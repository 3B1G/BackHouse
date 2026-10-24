"""Batched investigation, code-assembled immutable reports, and ASI transport."""
import asyncio
import hashlib
import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
from pydantic import ValidationError

from . import __version__
from .batches import WRITE_INSTRUCTION, Batch, assemble_draft, batch_tools, opening_messages, plan_batches
from .contracts import AgentInfo, BatchResult, Draft, Report, ReportRequest, ReportResult, Sources, Window
from .tools import GROUP_OF, LEGACY_TOOL_GROUPS, TOOL_GROUPS, TOOLSET_VERSION, SnapshotTools, succeeded

LOG = logging.getLogger("bookkeeper")
MAX_CALLS_PER_RESPONSE = 20
TRACE_FORMAT = "batched/1"
TRANSPORT_ERRORS = {"ReadTimeout", "ConnectTimeout", "WriteTimeout", "PoolTimeout", "ReadError", "ConnectError"}
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
        "Model requested tools during final report generation",
    )):
        return str(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "The ASI API call timed out. Retry the request."
    if isinstance(exc, httpx.HTTPError):
        return "Could not reach the ASI API. Check connectivity and retry."
    if isinstance(exc, ValidationError):
        return "The request or model report did not match the required JSON format."
    if isinstance(exc, ValueError) and str(exc).startswith("Set ASI1_API_KEY"):
        return str(exc)
    return f"Report generation failed ({type(exc).__name__}); inspect the local trace."


class ASIClient:
    """Minimal ASI client; network errors fail the run without model repair loops."""

    def __init__(self, model="asi1", api_key=None):
        self.model = model
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

    async def complete(self, messages, tools):
        """Request the next tool calls or a batch result without logging credentials."""
        payload = {"model": self.model, "messages": messages, "temperature": 0,
                   "max_tokens": self.max_tokens if tools else self.report_max_tokens}
        if tools:
            payload["tools"] = tools
        else:
            payload["tool_choice"] = "none"
            payload["response_format"] = {"type": "json_object"}
        timeout = httpx.Timeout(connect=15, read=self.read_timeout if tools else self.report_timeout,
                                write=60, pool=15)
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.post("https://api.asi1.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {self.key}"},
                json=payload)
            if response.status_code >= 400:
                raise RuntimeError(f"ASI API returned HTTP {response.status_code}")
            body = response.json()
            choice = body["choices"][0]
            if choice.get("finish_reason") == "length":
                raise OutputTruncated(choice.get("message") or {}, body.get("usage"))
            return {**choice["message"], "usage": body.get("usage")}     # usage per call: calls run in parallel


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


def parse_result(content):
    """A BatchResult from a reply, allowing a Markdown code fence around the JSON (investigation turns are free text)."""
    text = (content or "").strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
    return BatchResult.model_validate_json(fenced.group(1) if fenced else text)


class Bookkeeper:
    """Serialize jobs and return previously saved results for repeated requests."""

    def __init__(self, root: Path, client=None, model="asi1", max_turns=None, snapshots=SnapshotTools, planner=plan_batches,
                 concurrency=None):
        self.root = root.resolve()
        self.concurrency = int(concurrency or os.getenv("BOOKKEEPER_CONCURRENCY", "4"))
        if self.concurrency < 1:
            raise ValueError("BOOKKEEPER_CONCURRENCY must be at least 1")
        self.client, self.model = client, model
        self.max_turns = max_turns                  # overrides every batch's turn cap (tests)
        self.snapshots, self.planner = snapshots, planner
        self.lock = asyncio.Lock()

    async def run(self, request: ReportRequest, sender="local") -> ReportResult:
        """Generate one report, or recover a completed result after a duplicate delivery."""
        async with self.lock:
            return await self._run(request, sender)

    # ------------------------------------------------------------ one batch
    async def _investigate(self, batch, messages, client, snapshots, record, request_id, used):
        """Run one batch's conversation. Returns (BatchResult, None) or (None, reason it was not reviewed)."""
        tools, events = batch_tools(batch), record["events"]
        turns = self.max_turns or batch.max_turns
        ready = False
        for turn in range(turns):
            final = ready or turn == turns - 1
            if final:
                messages.append({"role": "user", "content": WRITE_INSTRUCTION})
                events.append({"turn": turn, "instruction": WRITE_INSTRUCTION})
            elif turns > 3 and turn == turns - 2:
                note = "One data-gathering turn remains for this batch, then its result is written. Finish soon."
                messages.append({"role": "user", "content": note})
                events.append({"turn": turn, "instruction": note})
            LOG.info("Request %s: %s turn %s/%s (%s)", request_id, batch.id, turn + 1, turns,
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
            usage = message.pop("usage", None)
            events.append({"turn": turn, "message": message, "usage": usage})
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
                return parse_result(message.get("content")), None
            except ValidationError:
                if final:
                    return None, "its result did not match the BatchResult format"
                messages.append({"role": "assistant", "content": message.get("content") or ""})
                ready = True
        return None, "its turn budget ran out"

    # ------------------------------------------------------------ publishing
    def _publish(self, draft, trace, *, report_id, request_id, request_data, receipt, sources, balances, limitations, model):
        report_path = self.root / "data" / "reports" / f"{report_id}.json"
        report = Report(**draft.model_dump(), report_id=report_id, request_id=request_id,
            generated_at=datetime.now(timezone.utc), as_of=sources["as_of"],
            window=Window(start=sources["window_start"], end=sources["as_of"]),
            sources=Sources(**{k: sources[k] for k in SOURCE_KEYS}),
            bookkeeper=AgentInfo(version=__version__, model=model, trace_id=trace["trace_id"]), balances=balances)
        report.limitations = list(dict.fromkeys(limitations + report.limitations))
        trace["status"] = "generated_unverified"
        immutable_json(self.root / "data" / "bookkeeper_traces" / f"{trace['trace_id']}.json", trace)
        immutable_json(report_path, report.model_dump(mode="json"))
        result = ReportResult(request_id=request_id, report_id=report_id,
                              filename=str(report_path.relative_to(self.root)), as_of=report.as_of)
        immutable_json(receipt, {"request": request_data, "result": result.model_dump(mode="json")})
        LOG.info("Request %s: saved %s", request_id, result.filename)
        return result

    def _fail(self, trace, exc):
        trace.update(status="failed", error_type=type(exc).__name__, error_message=failure_message(exc))
        exc.bookkeeper_trace_id = trace["trace_id"]
        path = self.root / "data" / "bookkeeper_traces" / f"{trace['trace_id']}.json"
        if not path.exists():
            immutable_json(path, trace)

    @staticmethod
    def _assemble(results, unreviewed, used, bank_rows):
        if used != set(TOOL_GROUPS):
            raise ValueError("The investigation did not retrieve evidence from both bank and books tools")
        if not results:
            raise RuntimeError("No investigation batch produced a result; see the trace for each batch")
        return assemble_draft(results, unreviewed, bank_rows)

    # ------------------------------------------------------------ a new run
    async def _run(self, request, sender):
        request_key = hashlib.sha256(f"{sender}\0{request.request_id}".encode()).hexdigest()
        reports = self.root / "data" / "reports"
        receipt = reports / "requests" / f"{request_key}.json"
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
        report_path = reports / f"{report_id}.json"
        # The report itself recovers a crash between report publication and receipt publication.
        if report_path.exists():
            report = Report.model_validate_json(report_path.read_text())
            trace_path = self.root / "data" / "bookkeeper_traces" / f"{report.bookkeeper.trace_id}.json"
            if json.loads(trace_path.read_text())["request"] != request_data:
                raise ValueError("request_id was already used with different parameters")
            result = ReportResult(request_id=request.request_id, report_id=report_id,
                                  filename=str(report_path.relative_to(self.root)), as_of=report.as_of)
            immutable_json(receipt, {"request": request_data, "result": result.model_dump(mode="json")})
            return result
        snapshots = self.snapshots(self.root, request.nessie_tag, request.as_of)
        trace_id = "TR-" + uuid4().hex
        sources = {"nessie_tag": request.nessie_tag, "bank_snapshot": str(snapshots.bank_path.relative_to(self.root)),
                   "books_db": str(snapshots.books_path.relative_to(self.root)), "bank_rows": snapshots.bank_rows,
                   "as_of": snapshots.as_of, "window_start": snapshots.start}
        trace = {"trace_id": trace_id, "format": TRACE_FORMAT, "request": request_data, "model": self.model,
                 "toolset": {"version": TOOLSET_VERSION, "groups": TOOL_GROUPS}, "sources": sources, "batches": []}
        try:
            balances = snapshots.balances().model_dump(mode="json")
            trace.update(balances=balances, limitations=snapshots.limitations)
            plan = self.planner(snapshots.session)
            trace["plan"] = [{"id": b.id, "title": b.title, "window": [b.date_from, b.date_to]} for b in plan]
            client = self.client or ASIClient(self.model)
            used, records, outcomes = set(), {}, {}
            gate = asyncio.Semaphore(self.concurrency)

            async def investigate(index, batch):
                async with gate:
                    messages = opening_messages(batch, snapshots.session, balances, snapshots.limitations)
                    record = {"id": batch.id, "title": batch.title, "window": [batch.date_from, batch.date_to],
                              "status": "running", "initial_messages": list(messages), "events": []}
                    records[index] = record
                    try:
                        result, reason = await self._investigate(batch, messages, client, snapshots, record,
                                                                 request.request_id, used)
                    except Exception as exc:      # the other batches carry on, so their work is kept for resume
                        record.update(status="failed", error_type=type(exc).__name__)
                        LOG.error("Request %s: %s failed: %s", request.request_id, batch.id, failure_message(exc))
                        raise
                    if result is None:
                        record.update(status="unreviewed", reason=reason)
                        LOG.info("Request %s: %s not reviewed: %s", request.request_id, batch.id, reason)
                    else:
                        record.update(status="done", result=result.model_dump(mode="json"))
                        LOG.info("Request %s: %s done (%s findings)", request.request_id, batch.id, len(result.findings))
                    outcomes[index] = result, reason

            errors = [e for e in await asyncio.gather(*(investigate(i, b) for i, b in enumerate(plan)), return_exceptions=True)
                      if isinstance(e, BaseException)]
            trace["batches"] = [records[i] for i in sorted(records)]
            if errors:
                raise errors[0]
            results = [(b, outcomes[i][0]) for i, b in enumerate(plan) if outcomes[i][0] is not None]   # plan order
            unreviewed = [(b, outcomes[i][1]) for i, b in enumerate(plan) if outcomes[i][0] is None]
            draft, trace["assembly"] = self._assemble(results, unreviewed, used, snapshots.bank_rows)
            return self._publish(draft, trace, report_id=report_id, request_id=request.request_id, request_data=request_data,
                                 receipt=receipt, sources=sources, balances=balances, limitations=snapshots.limitations,
                                 model=self.model)
        except Exception as exc:
            LOG.error("Request %s failed: %s (trace %s)", request.request_id, failure_message(exc), trace_id)
            self._fail(trace, exc)
            raise
        finally:
            snapshots.close()

    # ------------------------------------------------------------ resume
    async def resume(self, trace_id: str) -> ReportResult:
        """Retry only a result-writing call lost to transport failure, using frozen trace evidence; no tool runs."""
        if not re.fullmatch(r"TR-[a-f0-9]{32}", trace_id):
            raise ValueError("Invalid trace ID")
        async with self.lock:
            saved = json.loads((self.root / "data" / "bookkeeper_traces" / f"{trace_id}.json").read_text())
            request_key = hashlib.sha256(f"resume\0{trace_id}".encode()).hexdigest()
            receipt = self.root / "data" / "reports" / "requests" / f"{request_key}.json"
            if receipt.exists():
                result = ReportResult.model_validate(json.loads(receipt.read_text())["result"])
                if not (self.root / result.filename).is_file():
                    raise ValueError("Previously saved report is missing")
                return result
            report_path = self.root / "data" / "reports" / f"R-{request_key}.json"
            if report_path.exists():
                report = Report.model_validate_json(report_path.read_text())
                result = ReportResult(request_id=report.request_id, report_id=report.report_id,
                    filename=str(report_path.relative_to(self.root)), as_of=report.as_of)
                immutable_json(receipt, {"request": saved["request"], "result": result.model_dump(mode="json")})
                return result
            if saved.get("format") == TRACE_FORMAT:
                return await self._resume_batched(saved, trace_id, request_key, receipt)
            return await self._resume_conversation(saved, trace_id, request_key, receipt)

    async def _resume_batched(self, saved, trace_id, request_key, receipt):
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
            client = self.client or ASIClient(saved["model"])
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
                    usage = message.pop("usage", None)
                    record["events"].append({"turn": turn, "message": message, "usage": usage})
                    reason = "the model asked for tools while writing its result" if message.get("tool_calls") else None
                    result = None if reason else parse_result(message.get("content"))
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
            return self._publish(draft, trace, report_id=f"R-{request_key}", request_id=request_data["request_id"],
                                 request_data=request_data, receipt=receipt, sources=saved["sources"], balances=saved["balances"],
                                 limitations=saved["limitations"], model=saved["model"])
        except Exception as exc:
            LOG.error("Result writing failed: %s (trace %s)", failure_message(exc), trace["trace_id"])
            self._fail(trace, exc)
            raise

    async def _resume_conversation(self, saved, trace_id, request_key, receipt):
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
            client = self.client or ASIClient(saved["model"])
            message = await client.complete(messages, [])
            trace["events"].append({"turn": events[-1].get("turn"), "message": message})
            if message.get("tool_calls"):
                raise RuntimeError("Model requested tools during final report generation; no report was saved")
            draft = Draft.model_validate_json(message.get("content") or "")
            tag = request_data["nessie_tag"]
            source = dict(saved.get("sources") or {"nessie_tag": tag, "bank_snapshot": f"data/nessie_sync_{tag}.db",
                                                   "bank_rows": context["bank_rows"], "books_db": "data/books.db"})
            source.update(as_of=context["as_of"], window_start=context["window_start"])
            return self._publish(draft, trace, report_id=f"R-{request_key}", request_id=request_data["request_id"],
                                 request_data=request_data, receipt=receipt, sources=source, balances=context["balances"],
                                 limitations=context["limitations"], model=saved["model"])
        except Exception as exc:
            LOG.error("Report writing failed: %s (trace %s)", failure_message(exc), trace["trace_id"])
            self._fail(trace, exc)
            raise
