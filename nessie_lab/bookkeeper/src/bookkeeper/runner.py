"""Batched investigation, code-assembled immutable reports, and ASI transport."""
import asyncio
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError

from . import __version__
from .batches import (WRITE_INSTRUCTION, Batch, assemble_draft, bank_ids_in, batch_tools, drop_findings, feedback_message,
                      opening_messages, plan_batches, problem_report)
from .contracts import (AgentInfo, BatchResult, Draft, Drafted, Report, ReportRequest, ReportResult, RevisionRequest, Sources,
                        Window)
from .tools import GROUP_OF, LEGACY_TOOL_GROUPS, TOOL_GROUPS, TOOLSET_VERSION, SnapshotTools, succeeded

LOG = logging.getLogger("bookkeeper")
MAX_CALLS_PER_RESPONSE = 20
TRACE_FORMAT = "batched/1"
TRANSPORT_ERRORS = {"ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "ReadError", "ConnectError"}
ASI_URL = "https://api.asi1.ai/v1/chat/completions"
PROGRESS_SECONDS = 30                 # debug-log a long streamed reply this often
SOURCE_KEYS = ("nessie_tag", "bank_snapshot", "bank_rows", "books_db")


class OutputTruncated(RuntimeError):
    """The model hit its output token limit; `partial` is the partial response."""

    def __init__(self, message, usage=None):
        super().__init__("ASI output exceeded its token limit")
        self.partial, self.usage = message, usage


def failure_message(exc: Exception) -> str:
    """Expose actionable diagnostics without forwarding raw provider responses or model text."""
    if isinstance(exc, RuntimeError) and str(exc).startswith((
        "ASI API returned HTTP ", "ASI output exceeded", "Investigation exceeded", "No investigation batch",
        "Model requested tools during final report generation", "Verification failed", "Revision failed",
    )):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "The ASI API call timed out. Retry the request."
    if isinstance(exc, httpx.HTTPError):
        return "Could not reach the ASI API. Check connectivity and retry."
    if isinstance(exc, ValidationError):
        return "The request or model report did not match the required JSON format."
    if isinstance(exc, ValueError) and str(exc).startswith(("Set ASI1_API_KEY", "Verification failed", "Revision failed")):
        return str(exc)
    return f"Report generation failed ({type(exc).__name__}); inspect the local trace."


class ASIClient:
    """Minimal streaming ASI client; a dropped or stalled call is retried, other errors fail the run without model repair loops.

    Replies stream, so the read timeouts bound the silence between chunks, not a whole call: asi1 reasons at length and a
    reply can take minutes."""

    def __init__(self, model="asi1", api_key=None, temperature=0):
        self.model, self.temperature = model, temperature
        self.key = api_key or os.getenv("ASI1_API_KEY")
        if not self.key:
            raise ValueError("Set ASI1_API_KEY to run Bookkeeper with ASI")
        self.read_timeout = float(os.getenv("ASI_READ_TIMEOUT_SECONDS", "120"))
        self.report_timeout = float(os.getenv("ASI_REPORT_TIMEOUT_SECONDS", "600"))
        if self.read_timeout <= 0 or self.report_timeout <= 0:
            raise ValueError("ASI timeouts must be positive seconds")
        self.max_tokens = int(os.getenv("ASI_MAX_TOKENS", "12000"))
        self.report_max_tokens = int(os.getenv("ASI_REPORT_MAX_TOKENS", "32000"))
        if self.max_tokens <= 0 or self.report_max_tokens <= 0:
            raise ValueError("ASI token limits must be positive")
        self.retries = int(os.getenv("ASI_RETRIES", "2"))
        self.backoff = float(os.getenv("ASI_RETRY_BACKOFF_SECONDS", "5"))
        if self.retries < 0 or self.backoff < 0:
            raise ValueError("ASI retries and retry backoff must not be negative")

    async def complete(self, messages, tools):
        """Request the next tool calls or a batch result without logging credentials."""
        payload = {"model": self.model, "messages": messages, "temperature": self.temperature,
                   "max_tokens": self.max_tokens if tools else self.report_max_tokens,
                   "stream": True, "stream_options": {"include_usage": True}}
        if tools:
            payload["tools"] = tools
        else:
            payload["tool_choice"] = "none"
            payload["response_format"] = {"type": "json_object"}
        # read = the longest silence allowed between streamed chunks (including the wait for the first one)
        timeout = httpx.Timeout(connect=15, read=self.read_timeout if tools else self.report_timeout,
                                write=60, pool=15)
        for attempt in range(1, self.retries + 2):
            try:
                message = await self._stream(payload, timeout)
                break
            except httpx.TransportError as exc:        # stalls and dropped connections, even mid-stream; HTTP errors are not retried
                if attempt > self.retries:
                    raise
                LOG.warning("ASI call failed (%s); retry %s/%s in %ss", type(exc).__name__, attempt, self.retries,
                            self.backoff * attempt)
                await asyncio.sleep(self.backoff * attempt)
        if attempt > 1:
            message["retries"] = attempt - 1
        return message

    async def _stream(self, payload, timeout):
        """One streamed call, assembled into the message shape a non-streamed reply has (plus usage per call)."""
        content, reasoning, calls, finish, usage = [], [], {}, None, None
        started = last_log = time.monotonic()
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", ASI_URL, headers={"Authorization": f"Bearer {self.key}"}, json=payload) as response:
                if response.status_code >= 400:
                    await response.aread()
                    raise RuntimeError(f"ASI API returned HTTP {response.status_code}")
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if data == "[DONE]":
                        break
                    chunk = json.loads(data)
                    usage = chunk.get("usage") or usage
                    for choice in chunk.get("choices") or []:
                        delta = choice.get("delta") or {}
                        content.append(delta.get("content") or "")
                        reasoning.append(delta.get("reasoning_content") or "")
                        for part in delta.get("tool_calls") or []:
                            call = calls.setdefault(part.get("index", len(calls)),
                                                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}})
                            call["id"] = call["id"] or part.get("id")
                            call["type"] = part.get("type") or call["type"]
                            function = part.get("function") or {}
                            call["function"]["name"] = call["function"]["name"] or function.get("name") or ""
                            call["function"]["arguments"] += function.get("arguments") or ""
                        finish = choice.get("finish_reason") or finish
                    if time.monotonic() - last_log >= PROGRESS_SECONDS:
                        last_log = time.monotonic()
                        LOG.debug("ASI reply still generating after %.0fs (%s chars)", last_log - started,
                                  sum(map(len, content)) + sum(map(len, reasoning)))
        message = {"role": "assistant", "content": "".join(content) or None}
        if calls:
            message["tool_calls"] = [calls[i] for i in sorted(calls)]
        if reasoning := "".join(reasoning):
            message["reasoning_content"] = reasoning
        if finish == "length":
            raise OutputTruncated(message, usage)
        return {**message, "usage": usage}                # usage per call: calls run in parallel


def immutable_json(path: Path, value):
    """Publish a fully written file atomically, never replacing an existing artifact."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with tmp.open("x") as stream:
            json.dump(value, stream, indent=2, default=str)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def replay(initial_messages, events, group_of):
    """Rebuild a saved conversation and the evidence groups it reached, without running any tool."""
    messages, used = list(initial_messages), set()
    for event in events:
        if "instruction" in event:
            messages.append({"role": "user", "content": event["instruction"]})
        elif "message" in event:
            message = event["message"]
            entry = {"role": "assistant", "content": message.get("content")}
            if message.get("tool_calls"):
                entry["tool_calls"] = message["tool_calls"]
            messages.append(entry)
        elif "tool_call_id" in event:
            messages.append({"role": "tool", "tool_call_id": event["tool_call_id"], "content": json.dumps(event["result"])})
            if succeeded(event["result"]) and event["tool"] in group_of:
                used.add(group_of[event["tool"]])
    return messages, used


def seen_bank_ids(events):
    """The bank rows this batch's tools returned, from its recorded events (works on a saved trace too)."""
    return bank_ids_in([e["result"] for e in events if "result" in e])


def checked(result, problems, batch, record):
    """Drop the findings that would not survive the contracts hand-off; say so in the result and the trace."""
    record["dropped_findings"] = [{"id": fid, "problems": p} for fid, p in problems.items()]
    LOG.info("%s: left out %s finding(s) that break the hand-off rules", batch.id, len(problems))
    return drop_findings(result, problems, batch)


def draft_name(report_id, round=1):
    """A draft's filename under data/drafts: round 1 is the first draft, later rounds answer the Verifier's feedback."""
    return f"{report_id}.json" if round == 1 else f"{report_id}.r{round}.json"


def called(turn, message):
    """A model call's trace event; usage and retries move from the message onto the event."""
    event = {"turn": turn, "message": message, "usage": message.pop("usage", None)}
    if retries := message.pop("retries", 0):
        event["retries"] = retries
    return event


def fix_instruction(problems):
    """Appended to the writing instruction when a result breaks the hand-off rules."""
    return " Fix these problems in your result: " + "; ".join(
        f"finding {fid}: {', '.join(p)}" for fid, p in problems.items()) + "."


def parse_result(content):
    """A BatchResult from a reply, allowing a Markdown code fence around the JSON (investigation turns are free text)."""
    text = (content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    return BatchResult.model_validate_json(fenced.group(1) if fenced else text)


@dataclass
class Investigation:
    """One completed batched investigation, assembled in code but not yet saved."""
    draft: Draft
    trace: dict
    sources: dict
    balances: dict
    limitations: list
    bank_ids: frozenset          # every bank row in the snapshot through the cutoff


class Bookkeeper:
    """Serialize jobs and return previously saved results for repeated requests.

    The Bookkeeper saves a draft; the Verifier (verifier.py) publishes the report. role="verifier" makes this the
    Verifier's own investigator: its prompts say so and its traces go to data/verifier_traces."""

    def __init__(self, root: Path, client=None, model="asi1", max_turns=None, snapshots=SnapshotTools, planner=plan_batches,
                 concurrency=None, role="bookkeeper", temperature=0):
        self.root = root.resolve()
        self.role, self.temperature = role, temperature
        self.traces = self.root / "data" / f"{role}_traces"
        self.concurrency = int(concurrency or os.getenv("BOOKKEEPER_CONCURRENCY", "4"))
        if self.concurrency < 1:
            raise ValueError("BOOKKEEPER_CONCURRENCY must be at least 1")
        self.client, self.model = client, model
        self.max_turns = max_turns                  # overrides every batch's turn cap (tests)
        self.snapshots, self.planner = snapshots, planner
        self.lock = asyncio.Lock()

    async def run(self, request: ReportRequest, sender="local") -> ReportResult | Drafted:
        """Draft one report for verification, or recover a saved draft or verified result after a duplicate delivery."""
        async with self.lock:
            return await self._run(request, sender)

    # ------------------------------------------------------------ one batch
    async def _investigate(self, batch, messages, client, snapshots, record, request_id, used):
        """Run one batch's conversation. Returns (BatchResult, None) or (None, reason it was not reviewed)."""
        tools, events = batch_tools(batch), record["events"]
        turns = self.max_turns or batch.max_turns
        ready, fix, last, turn = False, "", turns - 1, -1
        while turn < last:                         # `last` moves on once, to correct a final write that breaks the rules
            turn += 1
            final = ready or turn == last
            if final:
                instruction = WRITE_INSTRUCTION + fix
                messages.append({"role": "user", "content": instruction})
                events.append({"turn": turn, "instruction": instruction})
            elif turns > 3 and turn == turns - 2:
                note = "One data-gathering turn remains for this batch, then its result is written. Finish soon."
                messages.append({"role": "user", "content": note})
                events.append({"turn": turn, "instruction": note})
            LOG.info("Request %s: %s turn %s/%s (%s)", request_id, batch.id, turn + 1, last + 1,
                     "writing result" if final else "investigating")
            try:
                message = await client.complete(messages, [] if final else tools)
            except OutputTruncated as exc:
                events.append({"turn": turn, "truncated_message": exc.partial, "usage": exc.usage})
                if final:
                    return None, "its result was cut off at the output limit"
                LOG.info("Request %s: %s reply cut off at the output limit; writing its result", request_id, batch.id)
                ready = True
                continue
            events.append(called(turn, message))
            calls = message.get("tool_calls") or []
            if calls:
                if final:
                    return None, "the model asked for tools while writing its result"
                messages.append({"role": "assistant", "content": message.get("content"), "tool_calls": calls})
                for index, call in enumerate(calls):
                    name = call["function"]["name"]
                    try:
                        arguments = json.loads(call["function"].get("arguments") or "{}")
                    except (TypeError, ValueError):
                        arguments = call["function"].get("arguments")
                        result = {"error": "tool arguments must be a JSON object", "cutoff": snapshots.as_of}
                    else:
                        if index >= MAX_CALLS_PER_RESPONSE:        # every call still needs a reply; only the first 20 run
                            result = {"error": f"Not run: at most {MAX_CALLS_PER_RESPONSE} tool calls per response. Repeat "
                                      "this call in your next response if you still need it.", "cutoff": snapshots.as_of}
                        elif name not in batch.tools:
                            result = {"error": f"Not run: '{name}' is not available in this batch; available: "
                                      f"{', '.join(batch.tools)}", "cutoff": snapshots.as_of}
                        else:
                            result = snapshots.call(name, arguments)
                    if succeeded(result) and name in GROUP_OF:
                        used.add(GROUP_OF[name])
                    events.append({"tool_call_id": call["id"], "tool": name, "arguments": arguments, "result": result})
                    messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result, default=str)})
                continue
            try:
                result = parse_result(message.get("content"))
            except ValidationError:
                if final:
                    return None, "its result did not match the BatchResult format"
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                ready = True
                continue
            problems = problem_report(result, seen_bank_ids(events))
            if not problems:
                return result, None
            if not fix:                # a result that breaks the hand-off rules: one corrected write, tools off
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                fix, ready = fix_instruction(problems), True
                if final:
                    last += 1
                continue
            return checked(result, problems, batch, record), None
        return None, "its turn budget ran out"

    # ------------------------------------------------------------ publishing
    def _save_draft(self, draft, trace, *, report_id, request_id, sources, balances, limitations, model, round=1):
        """Save the assembled report as a draft for the Verifier; only the Verifier publishes reports."""
        draft_path = self.root / "data" / "drafts" / draft_name(report_id, round)
        report = Report(**draft.model_dump(), report_id=report_id, request_id=request_id,
            generated_at=datetime.now(timezone.utc), as_of=sources["as_of"],
            window=Window(start=sources["window_start"], end=sources["as_of"]),
            sources=Sources(**{k: sources[k] for k in SOURCE_KEYS}),
            bookkeeper=AgentInfo(version=__version__, model=model, trace_id=trace["trace_id"]), balances=balances)
        report.limitations = list(dict.fromkeys(limitations + report.limitations))
        trace["status"] = "drafted"
        immutable_json(self.traces / f"{trace['trace_id']}.json", trace)
        immutable_json(draft_path, report.model_dump(mode="json"))
        LOG.info("Request %s: saved draft %s for verification", request_id, draft_path.relative_to(self.root))
        return Drafted(request_id=request_id, report_id=report_id, draft=str(draft_path.relative_to(self.root)), as_of=report.as_of,
                       round=round)

    def latest_draft(self, report_id):
        """The highest-round saved draft for a report id (Drafted), or None."""
        drafts, round = self.root / "data" / "drafts", 1
        if not (drafts / draft_name(report_id)).exists():
            return None
        while (drafts / draft_name(report_id, round + 1)).exists():
            round += 1
        report = Report.model_validate_json((drafts / draft_name(report_id, round)).read_text())
        return Drafted(request_id=report.request_id, report_id=report_id, draft=f"data/drafts/{draft_name(report_id, round)}",
                       as_of=report.as_of, round=round)

    def _saved(self, report_id, receipt, request_data=None):
        """A published report (recorded in the receipt) or a saved draft for this report id, else None.

        The report recovers a crash between the Verifier's publication and the receipt; the latest draft is verified
        again, which resumes the revision rounds where they stopped."""
        for folder in ("reports", "drafts"):
            path = self.root / "data" / folder / f"{report_id}.json"
            if not path.exists():
                continue
            report = Report.model_validate_json(path.read_text())
            trace = json.loads((self.root / "data" / "bookkeeper_traces" / f"{report.bookkeeper.trace_id}.json").read_text())
            if request_data is not None and trace["request"] != request_data:
                raise ValueError("request_id was already used with different parameters")
            filename = str(path.relative_to(self.root))
            if folder == "drafts":
                return self.latest_draft(report_id)
            result = ReportResult(request_id=report.request_id, report_id=report_id, filename=filename, as_of=report.as_of,
                                  verified=report.verification.passed)
            immutable_json(receipt, {"request": trace["request"], "result": result.model_dump(mode="json")})
            return result
        return None

    def complete(self, report_id: str) -> ReportResult:
        """Record the report the Verifier published for this draft, so repeated requests return it without new runs."""
        if not re.fullmatch(r"R-[0-9a-f]{64}", report_id):
            raise ValueError("Invalid report ID")
        receipt = self.root / "data" / "reports" / "requests" / f"{report_id.removeprefix('R-')}.json"
        if receipt.exists():
            return ReportResult.model_validate(json.loads(receipt.read_text())["result"])
        if not (self.root / "data" / "reports" / f"{report_id}.json").is_file():
            raise ValueError("The Verifier has not published this report")
        result = self._saved(report_id, receipt)
        LOG.info("Request %s: verified report %s (passed: %s)", result.request_id, result.filename, result.verified)
        return result

    def _fail(self, trace, exc):
        trace.update(status="failed", error_type=type(exc).__name__, error_message=failure_message(exc))
        exc.bookkeeper_trace_id = trace["trace_id"]
        path = self.traces / f"{trace['trace_id']}.json"
        if not path.exists():
            immutable_json(path, trace)

    @staticmethod
    def _assemble(results, unreviewed, used, bank_rows):
        if used != set(TOOL_GROUPS):
            raise ValueError("The investigation did not retrieve evidence from both bank and books tools")
        if not results:
            raise RuntimeError("No investigation batch produced a result; see the trace for each batch")
        return assemble_draft(results, unreviewed, bank_rows)

    async def _run_batches(self, jobs, client, snapshots, request_id, used):
        """Run [(Batch, opening messages)] in parallel, CONCURRENCY at a time.

        Returns (records, outcomes, errors) in job order; a batch that never started has no record or outcome. A batch
        error does not stop the others, so their work is kept in the trace."""
        gate = asyncio.Semaphore(self.concurrency)
        records, outcomes = [None] * len(jobs), [None] * len(jobs)

        async def one(index, batch, messages):
            async with gate:
                record = {"id": batch.id, "title": batch.title, "window": [batch.date_from, batch.date_to],
                          "status": "running", "initial_messages": list(messages), "events": []}
                records[index] = record
                try:
                    result, reason = await self._investigate(batch, messages, client, snapshots, record, request_id, used)
                except Exception as exc:
                    record.update(status="failed", error_type=type(exc).__name__)
                    LOG.error("Request %s: %s failed: %s", request_id, batch.id, failure_message(exc))
                    raise
                if result is None:
                    record.update(status="unreviewed", reason=reason)
                    LOG.info("Request %s: %s not reviewed: %s", request_id, batch.id, reason)
                else:
                    record.update(status="done", result=result.model_dump(mode="json"))
                    LOG.info("Request %s: %s done (%s findings)", request_id, batch.id, len(result.findings))
                outcomes[index] = result, reason

        errors = [e for e in await asyncio.gather(*(one(i, b, m) for i, (b, m) in enumerate(jobs)), return_exceptions=True)
                  if isinstance(e, BaseException)]
        return records, outcomes, errors

    # ------------------------------------------------------------ a new run
    async def _run(self, request, sender):
        request_key = hashlib.sha256(f"{sender}\0{request.request_id}".encode()).hexdigest()
        receipt = self.root / "data" / "reports" / "requests" / f"{request_key}.json"
        request_data = request.model_dump(mode="json")
        if receipt.exists():
            saved = json.loads(receipt.read_text())
            if saved["request"] != request_data:
                raise ValueError("request_id was already used with different parameters")
            result = ReportResult.model_validate(saved["result"])
            if not (self.root / result.filename).is_file():
                raise ValueError("Previously saved report is missing")
            return result
        report_id = "R-" + request_key
        if (saved := self._saved(report_id, receipt, request_data)) is not None:
            return saved
        found = await self.investigate(request)
        try:
            return self._save_draft(found.draft, found.trace, report_id=report_id, request_id=request.request_id,
                                    sources=found.sources, balances=found.balances, limitations=found.limitations, model=self.model)
        except Exception as exc:
            LOG.error("Request %s failed: %s (trace %s)", request.request_id, failure_message(exc), found.trace["trace_id"])
            self._fail(found.trace, exc)
            raise

    async def investigate(self, request: ReportRequest) -> Investigation:
        """Run the batched investigation at the request's cutoff and assemble its draft; only a failed trace is saved."""
        snapshots = self.snapshots(self.root, request.nessie_tag, request.as_of)
        trace_id = "TR-" + uuid4().hex
        sources = {"nessie_tag": request.nessie_tag, "bank_snapshot": str(snapshots.bank_path.relative_to(self.root)),
                   "books_db": str(snapshots.books_path.relative_to(self.root)), "bank_rows": snapshots.bank_rows,
                   "as_of": snapshots.as_of, "window_start": snapshots.start}
        trace = {"trace_id": trace_id, "format": TRACE_FORMAT, "request": request.model_dump(mode="json"), "model": self.model,
                 "toolset": {"version": TOOLSET_VERSION, "groups": TOOL_GROUPS}, "sources": sources, "batches": []}
        try:
            balances = snapshots.balances().model_dump(mode="json")
            trace.update(balances=balances, limitations=snapshots.limitations)
            plan = self.planner(snapshots.session)
            trace["plan"] = [{"id": b.id, "title": b.title, "window": [b.date_from, b.date_to]} for b in plan]
            client = self.client or ASIClient(self.model, temperature=self.temperature)
            used = set()
            jobs = [(b, opening_messages(b, snapshots.session, balances, snapshots.limitations, self.role)) for b in plan]
            records, outcomes, errors = await self._run_batches(jobs, client, snapshots, request.request_id, used)
            trace["batches"] = [r for r in records if r]
            if errors:
                raise errors[0]
            results = [(b, outcomes[i][0]) for i, b in enumerate(plan) if outcomes[i][0] is not None]   # plan order
            unreviewed = [(b, outcomes[i][1]) for i, b in enumerate(plan) if outcomes[i][0] is None]
            draft, trace["assembly"] = self._assemble(results, unreviewed, used, snapshots.bank_rows)
            return Investigation(draft, trace, sources, balances, snapshots.limitations, snapshots.bank_ids)
        except Exception as exc:
            LOG.error("Request %s failed: %s (trace %s)", request.request_id, failure_message(exc), trace_id)
            self._fail(trace, exc)
            raise
        finally:
            snapshots.close()

    # ------------------------------------------------------------ revision rounds
    async def revise(self, request: RevisionRequest) -> Drafted:
        """Redo the batches the Verifier's feedback names, with that feedback, and save draft `round`.

        Every other batch result is reused from the previous round's trace, so a round costs a few batches."""
        async with self.lock:
            return await self._revise(request)

    async def _revise(self, request):
        drafts = self.root / "data" / "drafts"
        if (drafts / draft_name(request.report_id, request.round)).exists():
            return self.latest_draft(request.report_id)
        previous_path = drafts / draft_name(request.report_id, request.round - 1)
        if not previous_path.is_file():
            raise ValueError(f"Revision failed: there is no round {request.round - 1} draft to revise")
        previous = Report.model_validate_json(previous_path.read_text())
        if previous.request_id != request.request_id:
            raise ValueError("Revision failed: the draft belongs to another request")
        feedback_path = (self.root / request.feedback).resolve()
        if feedback_path.parent != (self.root / "data" / "verifications" / request.report_id).resolve():
            raise ValueError("Revision failed: the feedback must be under data/verifications/<report_id>/")
        feedback = json.loads(feedback_path.read_text())["feedback"]
        prior = json.loads((self.traces / f"{previous.bookkeeper.trace_id}.json").read_text())
        trace = {key: prior[key] for key in ("format", "request", "model", "toolset", "sources", "balances", "limitations", "plan")}
        trace.update(trace_id="TR-" + uuid4().hex, revision_of=prior["trace_id"], round=request.round, feedback=request.feedback,
                     batches=[dict(r) for r in prior["batches"]])
        snapshots = self.snapshots(self.root, previous.sources.nessie_tag, previous.as_of)
        try:
            if snapshots.bank_rows != previous.sources.bank_rows:
                raise RuntimeError("Revision failed: the snapshot changed since the first draft")
            plan = {b.id: b for b in self.planner(snapshots.session)}
            redo = [plan[batch_id] for batch_id in feedback if batch_id in plan]
            if not redo:
                raise ValueError("Revision failed: the feedback names no batch of this plan")
            last = {r["id"]: r.get("result") for r in prior["batches"]}
            jobs = [(b, opening_messages(b, snapshots.session, prior["balances"], snapshots.limitations, self.role)
                     + [{"role": "user", "content": feedback_message(request.round, last.get(b.id), feedback[b.id])}])
                    for b in redo]
            LOG.info("Request %s: revising %s with the Verifier's feedback (draft round %s)", request.request_id,
                     ", ".join(b.id for b in redo), request.round)
            client = self.client or ASIClient(self.model, temperature=self.temperature)
            used = set(TOOL_GROUPS)                   # the previous draft already had evidence from both groups
            records, _, errors = await self._run_batches(jobs, client, snapshots, request.request_id, used)
            trace["revised"] = [r for r in records if r]
            redone = {r["id"]: r for r in trace["revised"] if r["status"] == "done"}
            trace["batches"] = [redone.get(r["id"], r) for r in trace["batches"]]    # a batch that fails to redo keeps its result
            if errors:
                raise errors[0]
            stub = {p["id"]: Batch(p["id"], p["title"], p["window"][0], p["window"][1], "", (), 0) for p in prior["plan"]}
            results = [(plan.get(r["id"], stub.get(r["id"])), BatchResult.model_validate(r["result"]))
                       for r in trace["batches"] if r["status"] == "done"]
            unreviewed = [(plan.get(r["id"], stub.get(r["id"])), r.get("reason", "not reviewed"))
                          for r in trace["batches"] if r["status"] != "done"]
            draft, trace["assembly"] = self._assemble(results, unreviewed, used, snapshots.bank_rows)
            return self._save_draft(draft, trace, report_id=request.report_id, request_id=request.request_id,
                                    sources=prior["sources"], balances=prior["balances"], limitations=prior["limitations"],
                                    model=self.model, round=request.round)
        except Exception as exc:
            LOG.error("Request %s: revision failed: %s (trace %s)", request.request_id, failure_message(exc), trace["trace_id"])
            self._fail(trace, exc)
            raise
        finally:
            snapshots.close()

    # ------------------------------------------------------------ resume
    async def resume(self, trace_id: str) -> ReportResult | Drafted:
        """Retry only a result-writing call lost to transport failure, using frozen trace evidence; no tool runs.

        Like a run, it saves a draft for the Verifier, or returns the saved draft or verified report of an earlier resume."""
        if not re.fullmatch(r"TR-[a-f0-9]{32}", trace_id):
            raise ValueError("Invalid trace ID")
        async with self.lock:
            saved = json.loads((self.traces / f"{trace_id}.json").read_text())
            request_key = hashlib.sha256(f"resume\0{trace_id}".encode()).hexdigest()
            receipt = self.root / "data" / "reports" / "requests" / f"{request_key}.json"
            if receipt.exists():
                result = ReportResult.model_validate(json.loads(receipt.read_text())["result"])
                if not (self.root / result.filename).is_file():
                    raise ValueError("Previously saved report is missing")
                return result
            if (found := self._saved(f"R-{request_key}", receipt)) is not None:
                return found
            if saved.get("format") == TRACE_FORMAT:
                return await self._resume_batched(saved, trace_id, request_key)
            return await self._resume_conversation(saved, trace_id, request_key)

    async def _resume_batched(self, saved, trace_id, request_key):
        def writing(r):
            return (r.get("status") == "failed" and r.get("error_type") in TRANSPORT_ERRORS and r.get("events")
                    and "Tools are now disabled" in r["events"][-1].get("instruction", ""))
        records = saved.get("batches", [])
        if (saved.get("status") != "failed" or saved.get("error_type") not in TRANSPORT_ERRORS
                or not any(writing(r) for r in records)):
            raise ValueError("Only a result-writing transport failure can be resumed; completed results are not revised")
        groups = saved["toolset"]["groups"]
        group_of = {name: group for group, names in groups.items() for name in names}
        used = set()
        for record in records:
            used |= replay(record["initial_messages"], record["events"], group_of)[1]
        request_data = saved["request"]
        trace = {key: saved[key] for key in ("format", "request", "model", "toolset", "sources", "balances", "limitations", "plan")}
        trace.update(trace_id="TR-" + uuid4().hex, resumed_from=trace_id,
                     batches=[dict(r, events=list(r["events"])) for r in records])

        def batch_of(r):
            return Batch(r["id"], r["title"], r["window"][0], r["window"][1], "", (), 0)
        try:
            client = self.client or ASIClient(saved["model"], temperature=self.temperature)
            for record in trace["batches"]:          # one writing call per batch lost at that step; nothing else reruns
                if not writing(record):
                    if record["status"] in ("failed", "running"):
                        record.update(status="unreviewed", reason=f"failed during investigation ({record.get('error_type', 'interrupted')})")
                    continue
                LOG.info("Request %s: resuming %s result writing from %s; no tools will run", request_data["request_id"], record["id"], trace_id)
                messages, _ = replay(record["initial_messages"], record["events"], group_of)
                turn = record["events"][-1].get("turn")
                try:
                    message = await client.complete(messages, [])
                    record["events"].append(called(turn, message))
                    reason = "the model asked for tools while writing its result" if message.get("tool_calls") else None
                    result = None if reason else parse_result(message.get("content"))
                    if result is not None and (problems := problem_report(result, seen_bank_ids(record["events"]))):
                        result = checked(result, problems, batch_of(record), record)
                except OutputTruncated as exc:
                    record["events"].append({"turn": turn, "truncated_message": exc.partial, "usage": exc.usage})
                    result, reason = None, "its result was cut off at the output limit"
                except ValidationError:
                    result, reason = None, "its result did not match the BatchResult format"
                record.pop("error_type", None)
                if result is None:
                    record.update(status="unreviewed", reason=reason)
                else:
                    record.update(status="done", result=result.model_dump(mode="json"))
            results = [(batch_of(r), BatchResult.model_validate(r["result"])) for r in trace["batches"] if r["status"] == "done"]
            unreviewed = [(batch_of(r), r["reason"]) for r in trace["batches"] if r["status"] == "unreviewed"]
            started = {r["id"] for r in trace["batches"]}
            unreviewed += [(Batch(p["id"], p["title"], p["window"][0], p["window"][1], "", (), 0),
                            "not run: the original run failed before reaching it") for p in saved["plan"] if p["id"] not in started]
            order = {p["id"]: n for n, p in enumerate(saved["plan"])}
            unreviewed.sort(key=lambda x: order.get(x[0].id, len(order)))
            draft, trace["assembly"] = self._assemble(results, unreviewed, used, saved["sources"]["bank_rows"])
            return self._save_draft(draft, trace, report_id=f"R-{request_key}", request_id=request_data["request_id"],
                                    sources=saved["sources"], balances=saved["balances"], limitations=saved["limitations"],
                                    model=saved["model"])
        except Exception as exc:
            LOG.error("Result writing failed: %s (trace %s)", failure_message(exc), trace["trace_id"])
            self._fail(trace, exc)
            raise

    async def _resume_conversation(self, saved, trace_id, request_key):
        """Traces written before batching: one conversation, with the Draft written in its last turn."""
        events = saved.get("events", [])
        if (saved.get("status") != "failed" or saved.get("error_type") not in TRANSPORT_ERRORS
                or not events or "Tools are now disabled" not in events[-1].get("instruction", "")):
            raise ValueError("Only a final report-writing transport failure can be resumed; completed drafts are not revised")
        groups = (saved.get("toolset") or {}).get("groups")
        group_of = {name: group for group, names in groups.items() for name in names} if groups else LEGACY_TOOL_GROUPS
        messages, used = replay(saved["initial_messages"], events, group_of)
        if used != {"bank", "books"}:
            raise ValueError("The saved investigation did not access both tools")
        request_data = saved["request"]
        context = json.loads(saved["initial_messages"][1]["content"])
        trace = {"trace_id": "TR-" + uuid4().hex, "resumed_from": trace_id, "request": request_data,
                 "model": saved["model"], "initial_messages": saved["initial_messages"], "events": events.copy()}
        for key in ("toolset", "sources"):
            if key in saved:
                trace[key] = saved[key]
        try:
            LOG.info("Request %s: resuming report writing from %s; no tools will run", request_data["request_id"], trace_id)
            client = self.client or ASIClient(saved["model"], temperature=self.temperature)
            message = await client.complete(messages, [])
            trace["events"].append(called(events[-1].get("turn"), message))
            if message.get("tool_calls"):
                raise RuntimeError("Model requested tools during final report generation; no report was saved")
            draft = Draft.model_validate_json(message.get("content") or "")
            tag = request_data["nessie_tag"]
            source = dict(saved.get("sources") or {"nessie_tag": tag, "bank_snapshot": f"data/nessie_sync_{tag}.db",
                                                   "bank_rows": context["bank_rows"], "books_db": "data/books.db"})
            source.update(as_of=context["as_of"], window_start=context["window_start"])
            return self._save_draft(draft, trace, report_id=f"R-{request_key}", request_id=request_data["request_id"],
                                    sources=source, balances=context["balances"], limitations=context["limitations"],
                                    model=saved["model"])
        except Exception as exc:
            LOG.error("Report writing failed: %s (trace %s)", failure_message(exc), trace["trace_id"])
            self._fail(trace, exc)
            raise
