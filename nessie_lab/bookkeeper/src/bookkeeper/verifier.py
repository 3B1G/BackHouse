"""The Verifier: repeats the Bookkeeper's investigation independently, sends its disagreements back for revision, and
publishes the report.

The Verifier investigates once and never sees the Bookkeeper's findings until its own investigation is assembled. Each
round compares the Bookkeeper's latest draft with it (compare.py). While rounds remain, every disagreement goes back to
the Bookkeeper batch concerned as feedback (a RevisionRequest); otherwise the report is published and the Verifier wins
what is still disputed: its version of a disputed finding, its findings the Bookkeeper missed, and none of the
Bookkeeper's findings it could not find.
"""
import asyncio
import hashlib
import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .batches import _targets, summarize
from .compare import compare, feedback
from .contracts import (AgentInfo, Draft, Drafted, Finding, Report, ReportRequest, ReportResult, RevisionRequest, Verification,
                        VerifyRequest)
from .runner import Bookkeeper, draft_name, failure_message, immutable_json

LOG = logging.getLogger("bookkeeper.verifier")


def disputes(result):
    """A fingerprint of what is still disputed, ids aside: equal across two rounds when a revision changed nothing."""
    def plain(f):
        return f.model_dump(mode="json", exclude={"id"}) if f else None
    items = sorted(json.dumps([plain(a), reason, plain(b)], sort_keys=True) for a, reason, b in result.rejected)
    items += sorted(json.dumps(plain(b), sort_keys=True) for b in result.extra)
    return hashlib.sha256("\n".join(items).encode()).hexdigest()


class Verifier:
    """Serialize verifications; a report already published for a draft is returned as-is."""

    def __init__(self, root: Path, client=None, model=None, temperature=None, max_rounds=None, **investigator):
        self.root = Path(root).resolve()
        self.model = model or os.getenv("VERIFIER_MODEL") or os.getenv("ASI_MODEL", "asi1")
        if temperature is None:
            temperature = float(os.getenv("VERIFIER_TEMPERATURE", "0"))
        self.max_rounds = int(max_rounds or os.getenv("VERIFIER_MAX_ROUNDS", "3"))
        if self.max_rounds < 1:
            raise ValueError("VERIFIER_MAX_ROUNDS must be at least 1")
        self.investigator = Bookkeeper(self.root, client, model=self.model, role="verifier", temperature=temperature,
                                       **investigator)
        self.lock = asyncio.Lock()

    async def verify(self, request: VerifyRequest) -> ReportResult | RevisionRequest:
        """Compare a draft with the Verifier's own investigation: ask for a revision, or publish the report."""
        async with self.lock:
            return await self._verify(request)

    def _result(self, report, path):
        return ReportResult(request_id=report.request_id, report_id=report.report_id, filename=str(path.relative_to(self.root)),
                            as_of=report.as_of, verified=report.verification.passed)

    def _draft(self, request):
        path = (self.root / request.draft).resolve()
        name = draft_name(request.report_id, request.round)
        if path.parent != (self.root / "data" / "drafts").resolve() or path.name != name:
            raise ValueError(f"Verification failed: round {request.round}'s draft must be data/drafts/{name}")
        if not path.is_file():
            raise ValueError("Verification failed: the draft does not exist")
        draft = Report.model_validate_json(path.read_text())
        if draft.report_id != request.report_id or draft.request_id != request.request_id:
            raise ValueError("Verification failed: the draft belongs to another request")
        return draft

    async def _mine(self, request, draft):
        """The Verifier's own investigation of this request: run once, then kept in data/verifier_drafts for every round."""
        path = self.root / "data" / "verifier_drafts" / f"{request.report_id}.json"
        if not path.exists():
            LOG.info("Request %s: verifying %s with an independent investigation", request.request_id, request.draft)
            found = await self.investigator.investigate(ReportRequest(request_id=draft.request_id, as_of=draft.as_of,
                                                                      nessie_tag=draft.sources.nessie_tag))
            trace = found.trace
            trace.update(status="investigated", report_id=request.report_id, bookkeeper_trace_id=draft.bookkeeper.trace_id)
            immutable_json(self.investigator.traces / f"{trace['trace_id']}.json", trace)
            immutable_json(path, {"trace_id": trace["trace_id"], "draft": found.draft.model_dump(mode="json"),
                                  "origin": trace["assembly"]["origin"], "sources": found.sources, "balances": found.balances,
                                  "bank_ids": sorted(found.bank_ids)})
        mine = json.loads(path.read_text())
        mine["draft"] = Draft.model_validate(mine["draft"])
        return mine

    async def _verify(self, request):
        report_path = self.root / "data" / "reports" / f"{request.report_id}.json"
        if report_path.exists():
            return self._result(Report.model_validate_json(report_path.read_text()), report_path)
        draft = self._draft(request)
        record_path = self.root / "data" / "verifications" / request.report_id / f"round-{request.round}.json"
        if record_path.exists() and (step := json.loads(record_path.read_text()).get("next")):
            return RevisionRequest.model_validate(step)           # this round was already compared: same next step
        mine = await self._mine(request, draft)
        try:
            self._same_snapshot(draft, mine)
            result = compare(draft.findings, mine["draft"].findings, frozenset(mine["bank_ids"]))
            bk_trace = json.loads((self.root / "data" / "bookkeeper_traces" / f"{draft.bookkeeper.trace_id}.json").read_text())
            bk_origin = (bk_trace.get("assembly") or {}).get("origin")   # drafts saved before revision rounds have none
            routed = feedback(result, bk_origin, mine["origin"]) if bk_origin is not None else {}
            fingerprint = disputes(result)
            before = record_path.with_name(f"round-{request.round - 1}.json")
            stalled = bool(routed) and before.exists() and json.loads(before.read_text()).get("disputes") == fingerprint
            if stalled:
                LOG.info("Request %s: round %s: the revision changed nothing; publishing", request.request_id, request.round)
            step = None
            if routed and request.round < self.max_rounds and not stalled:
                step = RevisionRequest(request_id=request.request_id, report_id=request.report_id, round=request.round + 1,
                                       feedback=str(record_path.relative_to(self.root)))
            if not record_path.exists():
                immutable_json(record_path, {
                    "round": request.round, "draft": request.draft, "pairs": result.pairs,
                    "rejected": [[a.id, reason, b.id if b else None] for a, reason, b in result.rejected],
                    "extra": [b.id for b in result.extra], "notes": result.notes, "feedback": routed,
                    "disputes": fingerprint, "stalled": stalled, "next": step.model_dump(mode="json") if step else None})
            if step:
                LOG.info("Request %s: round %s: %s disagreement(s); asking the Bookkeeper to revise %s", request.request_id,
                         request.round, len(result.rejected) + len(result.extra), ", ".join(routed))
                return step
            report = self._published(draft, mine, result, request.round, stalled)
            immutable_json(report_path, report.model_dump(mode="json"))
        except Exception as exc:
            exc.bookkeeper_trace_id = mine["trace_id"]
            LOG.error("Request %s: verification failed: %s (trace %s)", request.request_id, failure_message(exc), mine["trace_id"])
            raise
        LOG.info("Request %s: published %s after %s round(s) (%s findings)", request.request_id,
                 report_path.relative_to(self.root), request.round, len(report.findings))
        return self._result(report, report_path)

    @staticmethod
    def _same_snapshot(draft, mine):
        sources = mine["sources"]
        if (str(draft.as_of), str(draft.window.start), draft.sources.bank_rows) != (
                str(sources["as_of"]), str(sources["window_start"]), sources["bank_rows"]):
            raise RuntimeError("Verification failed: the snapshot changed since the Bookkeeper's run")
        if draft.balances.model_dump(mode="json") != mine["balances"]:
            raise RuntimeError("Verification failed: cash balances differ from the Bookkeeper's")

    def _published(self, draft, mine, result, rounds, stalled=False):
        """The report: agreed findings, and the Verifier's view of whatever is still disputed."""
        chosen = [(f, None) for f in result.agreed]
        chosen += [(b, f"the Verifier's version replaces the Bookkeeper's {a.id} ({reason})") for a, reason, b in result.rejected if b]
        chosen += [(b, "found by the Verifier; the Bookkeeper did not report it") for b in result.extra]
        findings, notes = [], []
        for n, (f, why) in enumerate(chosen, 1):
            findings.append(Finding.model_validate(dict(f.model_dump(), id=f"F{n}")))
            if why:
                notes.append(f"F{n} ({f.counterparty}, {f.root_cause}): {why}.")
        notes += [f"Left out the Bookkeeper's {a.id} ({a.counterparty}, {a.root_cause}): {reason}."
                  for a, reason, b in result.rejected if b is None]
        unreviewed = [f"Verifier: {x}" for x in mine["draft"].limitations if x.startswith("Not reviewed")]
        flagged = set().union(*(set(f.bank_ids) | _targets(f) for f in findings))
        summary = [f"{rounds} comparison round(s); " + ("the Bookkeeper and the Verifier agreed on every finding."
                                                       if not notes else "disagreements resolved in the Verifier's favour.")
                   + (" The last revision changed nothing, so the rounds stopped early." if stalled else "")]
        return Report.model_validate(dict(
            draft.model_dump(), schema_version="1.2", findings=[f.model_dump() for f in findings],
            summary=summarize(findings).model_dump(), cleared=[c.model_dump() for c in draft.cleared if not set(c.bank_ids) & flagged],
            limitations=list(dict.fromkeys(draft.limitations + notes)),
            verifier=AgentInfo(version=__version__, model=self.model, trace_id=mine["trace_id"]).model_dump(),
            verification=Verification(passed=True, checked_at=datetime.now(timezone.utc), findings_checked=len(draft.findings),
                                      findings_rejected=len(result.rejected), rounds=rounds,
                                      notes=summary + notes + result.notes + unreviewed).model_dump()))


async def verify_in_process(bookkeeper: Bookkeeper, verifier: Verifier, outcome):
    """Run the verification rounds in this process and record the published report; a finished result passes through."""
    if not isinstance(outcome, Drafted):
        return outcome
    step = VerifyRequest(request_id=outcome.request_id, report_id=outcome.report_id, draft=outcome.draft, round=outcome.round)
    while not isinstance(verdict := await verifier.verify(step), ReportResult):
        drafted = await bookkeeper.revise(verdict)
        step = VerifyRequest(request_id=drafted.request_id, report_id=drafted.report_id, draft=drafted.draft, round=drafted.round)
    return bookkeeper.complete(outcome.report_id)
