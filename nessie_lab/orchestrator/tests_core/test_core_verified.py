"""Part A: the Bookkeeper's verified report (bk schema 1.2). Fake 1.2 reports in the shape of bk's contracts.Report,
with the exact note strings bookkeeper/verifier.py writes; the Bookkeeper never runs. Also the served orchestrator's
Bookkeeper setup through the agent (delegated, degraded fallback, no rerun after a timeout). No ASI, no network."""
import asyncio
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from orchestrator import bk_adapter as A
from orchestrator import bk_runner as R
from orchestrator import bk_state as S
from orchestrator import checks_bk, tools
from test_core_bookkeeper import FEE, NEW, OWED, OWED2, DUP, FakeRunner, bk, finding, put, report  # noqa: F401

TRACE = "TR-" + "c" * 32
CORRECT = lambda fid: finding(fid, "commission_overcharge", "DoorDash", {"kind": "correct_to",  # noqa: E731
                                                                        "bank_id": "n-3", "cents": 90000}, 10000, ["n-3"])


def needs_owner(f):
    return {**f, "verdict": "needs_owner"}


def verified(report_id, as_of, findings, notes, rounds=2, **extra):
    """A published 1.2 report: verifier AgentInfo, verification.passed True, the Verifier's notes."""
    r = report(report_id, as_of, findings, **extra)
    r.update(schema_version="1.2", verifier={"version": "0.2.0", "model": "asi1", "trace_id": TRACE},
             verification={"passed": True, "checked_at": "2026-10-24T13:00:00+00:00",
                           "findings_checked": 5, "findings_rejected": 2, "rounds": rounds, "notes": notes})
    return r


NOTES = [
    "2 comparison round(s); disagreements resolved in the Verifier's favour.",
    "F2 (Mission Dry Goods, missed_payment): the Verifier's version replaces the Bookkeeper's F5 (owed 48000 vs "
    "47000 cents).",
    "F3 (TableTap, duplicate_charge): found by the Verifier; the Bookkeeper did not report it.",
    "Left out the Bookkeeper's F4 (Sonoma Dairy Supply, missed_payment): the Verifier's investigation found no "
    "matching issue.",
    # compare.py's verdict note uses the Bookkeeper's DRAFT id: F1 here is not the published F1
    "F1: the Bookkeeper said error and the Verifier needs_owner; the Verifier's verdict is used.",
    "Verifier: Not reviewed: delivery payouts (the batch result was cut off)",
]
V12 = verified("R-v12", "2026-10-21", [FEE("F1"), OWED("F2"), DUP("F3"), needs_owner(NEW("F4")), CORRECT("F5")], NOTES)


# ---------------------------------------------------------------- bk_adapter
def test_verified_status_parses_the_verifier_notes_by_published_id():
    vs = A.verified_status(V12)
    assert vs["verified"] is True and (vs["rounds"], vs["findings_checked"], vs["findings_rejected"]) == (2, 5, 2)
    assert vs["per_finding"] == {"F1": "agreed", "F2": "replaced", "F3": "verifier_only", "F4": "agreed",
                                 "F5": "agreed"}
    assert vs["left_out"] == [{"bookkeeper_id": "F4", "about": "Sonoma Dairy Supply, missed_payment",
                               "reason": "the Verifier's investigation found no matching issue"}]
    assert A.verification_label(True, 2) == "verified by the Bookkeeper's Verifier (2 rounds)"
    assert A.verification_label(True, 1) == "verified by the Bookkeeper's Verifier (1 round)"


def test_counterparty_with_brackets_still_parses():
    r = verified("R-x", "2026-10-21", [OWED("F1")],
                 ["F1 (Acme (West): Inc, missed_payment): the Verifier's version replaces the Bookkeeper's F2 "
                  "(it is owed on a different document)."])
    assert A.verified_status(r)["per_finding"] == {"F1": "replaced"}


@pytest.mark.parametrize("change", [
    {"schema_version": "1.1"}, {"verifier": None}, {"verifier": {"version": "x", "model": "y"}},
    {"verification": {"passed": False, "rounds": 0, "notes": []}},
])
def test_unverified_reports(change):
    vs = A.verified_status({**V12, **change})
    assert vs["verified"] is False and vs["per_finding"] == {} and vs["left_out"] == []


def test_both_schema_versions_validate_and_requests_stay_plain(monkeypatch):
    assert A.validate_report(V12) == [] and A.validate_report(report("R-a", "2026-10-21", [])) == []
    assert A.validate_report({**V12, "schema_version": "2.0"}) == ["schema_version is not 1.1 or 1.2"]
    monkeypatch.delenv(A.INCREMENTAL_ENV, raising=False)
    from datetime import date
    payload = A.build_request(date(2026, 10, 22), date(2026, 10, 21), "R-x", request_id="r1")
    assert set(payload) == {"request_id", "nessie_tag", "as_of"}                    # bk's ReportRequest exactly
    assert "--since" not in A.cli_args(payload) and "--prior-report-id" not in A.cli_args(payload)


# ---------------------------------------------------------------- checks_bk
def _converted(rep, session):
    import bookkeeper_tools as bt
    import contracts as K

    from orchestrator import env
    env.bind(session.root, session.tag)
    return K.from_bk_report(rep, bt.bank_id_map())


def test_without_owner_decisions_every_finding_awaits_the_owner(session):
    """A Verifier agreement never passes a finding: with no decisions, all downgrade (cautious adjustments only)."""
    findings, adjustments, rejected = _converted(V12, session)
    assert not rejected
    checks = checks_bk.check_findings(V12, findings)
    assert {c.ref: c.status for c in checks} == dict.fromkeys(["F1", "F2", "F3", "F4", "F5"], "downgrade")
    reasons = {c.ref: c.reasons for c in checks}
    assert "awaiting the owner" in reasons["F1"][0] and "Verifier agreed" in reasons["F1"][1]   # information only
    assert "published its own version" in reasons["F2"][1] and "found only by" in reasons["F3"][1]
    used = sorted((a.kind, a.finding_id, a.recoverable) for a in checks_bk.passed(V12, adjustments, findings))
    assert used == [("ongoing_overcharge", "F1", False), ("ongoing_overcharge", "F4", False),
                    ("unpaid_bill", "F2", False)]                       # no exclude_row (F3), no correct_row (F5)


def test_owner_decisions_accept_dismiss_revise(session):
    findings, adjustments, _ = _converted(V12, session)
    decisions = {"F1": "accept", "F2": {"decision": "dismiss", "note": "paid by cheque"}, "F3": "accept",
                 "F4": "revise", "F5": "accept"}
    checks = {c.ref: c for c in checks_bk.check_findings(V12, findings, decisions)}
    assert {k: c.status for k, c in checks.items()} == {"F1": "pass", "F2": "reject", "F3": "pass",
                                                        "F4": "downgrade", "F5": "pass"}   # revise: cautious only
    assert "owner's note: paid by cheque" in checks["F2"].reasons
    used = sorted((a.kind, a.finding_id, a.recoverable) for a in checks_bk.passed(V12, adjustments, findings, decisions))
    assert used == [("correct_row", "F5", False), ("exclude_row", "F3", False), ("ongoing_overcharge", "F1", True),
                    ("ongoing_overcharge", "F4", False)]          # F4 revised: its ongoing overcharge still counts
    as_list = [{"report_id": "R-v12", "finding_id": k, "decision": v if isinstance(v, str) else v["decision"]}
               for k, v in decisions.items()]
    assert [c.status for c in checks_bk.check_findings(V12, findings, as_list)] == [c.status for c in checks.values()]
    obj = [SimpleNamespace(report_id="R-v12", finding_id=d["finding_id"], decision=d["decision"]) for d in as_list]
    assert len(checks_bk.check_findings(V12, findings, obj)) == 5


@pytest.mark.parametrize("decisions,match", [
    ({"F1": "accept"}, "no decision for F2"),
    ({}, "no decision for F1"),
    ({**dict.fromkeys(["F1", "F2", "F3", "F4", "F5"], "accept"), "F9": "accept"}, "not in this report"),
    ({**dict.fromkeys(["F1", "F2", "F3", "F4"], "accept"), "F5": "approve"}, "accept, dismiss or revise"),
    ([{"report_id": "R-other", "finding_id": "F1", "decision": "accept"}], "another report"),
])
def test_review_incomplete_raises(session, decisions, match):
    findings, _, _ = _converted(V12, session)
    with pytest.raises(ValueError, match=f"review incomplete.*{match}"):
        checks_bk.check_findings(V12, findings, decisions)


def test_unverified_report_also_awaits_the_owner(session):
    rep = {**V12, "schema_version": "1.1", "verifier": None}
    findings, adjustments, _ = _converted(rep, session)
    checks = checks_bk.check_findings(rep, findings)
    assert {c.status for c in checks} == {"downgrade"} and "not verified" in checks[0].reasons[1]
    assert {a.kind for a in checks_bk.passed(rep, adjustments, findings)} <= {"unpaid_bill", "ongoing_overcharge"}


def test_get_findings_and_the_brief_show_verified_as_information(session):
    put(session.root, V12)
    r = tools.call(session, "get_findings", {})
    assert r["source"] == "latest Bookkeeper report" and r["verification"]["verified"] is True
    assert r["verification_label"] == "verified by the Bookkeeper's Verifier (2 rounds)"
    assert r["verification"]["per_finding"]["F5"] == "agreed"
    assert {c["status"] for c in r["checks"]} == {"downgrade"}
    base = tools.call(session, "forecast_base", {})
    assert {"finding_id": "F5", "kind": "correct_row"} not in base["adjustments_used"]
    assert ("verified by the Bookkeeper's Verifier (2 rounds); no committed review: each of the 5 finding(s) "
            "awaits the owner") in base["adjustments_note"]
    from orchestrator.brief import brief
    text = brief(session, propose=False)["text"]
    assert ("Bookkeeper (R-v12.json, verified by the Bookkeeper's Verifier (2 rounds)): 5 open finding(s), 0 with a "
            "proposed decision") in text
    assert "; Verifier agreed)" in text and "; Verifier's version)" in text and "; found by the Verifier)" in text


def test_brief_says_not_verified_for_a_1_1_report(session):
    put(session.root, report("R-old", "2026-10-21", [FEE("F1")]))
    from orchestrator.brief import brief
    text = brief(session, propose=False)["text"]
    assert "Bookkeeper (R-old.json, not verified): 1 open finding(s), 0 with a proposed decision" in text
    assert "; not verified)" in text


# ---------------------------------------------------------------- bk_state carries the status
def test_merged_state_carries_the_verifier_status_as_information(session):
    out = S.ingest(session.root, put(session.root, V12))
    assert out["verified"] is True and out["verification"] == "verified by the Bookkeeper's Verifier (2 rounds)"
    state = S.load(session.root)
    statuses = sorted(e["verification"]["status"] for e in state["findings"].values())
    assert statuses == ["agreed", "agreed", "agreed", "replaced", "verifier_only"]
    assert state["reports"][-1]["verified"] is True and state["reports"][-1]["rounds"] == 2
    merged = S.open_findings_report(state)
    assert merged["schema_version"] == "1.1" and merged["verification"]["passed"] is False   # unverified on its face
    r = tools.call(session, "get_findings", {})
    assert r["source"] == "merged Bookkeeper state" and r["verification"]["verified"] is True
    assert {c["status"] for c in r["checks"]} == {"downgrade"}
    cause = {f["finding_id"]: f["issue_type"] for f in r["findings"]}
    assert {cause[k]: v for k, v in r["verification"]["per_finding"].items()} == {
        "fee_rate_change": "agreed", "commission_overcharge": "agreed", "overcharge": "agreed",
        "missed_payment": "replaced", "duplicate_charge": "verifier_only"}
    reasons = {c["ref"]: c["reasons"] for c in r["checks"]}
    assert all("awaiting the owner" in v[0] for v in reasons.values())
    findings, adjustments, _ = _converted(merged, session)                  # decisions work on the merged ids too
    decisions = dict.fromkeys((f.finding_id for f in findings), "accept")
    assert {c.status for c in checks_bk.check_findings(merged, findings, decisions)} == {"pass"}
    view = tools.call(session, "bookkeeper_status", {})
    assert view["verification"] == {"agreed": 3, "replaced": 1, "verifier_only": 1, "unverified": 0}
    assert view["last_run_verification"] == "verified by the Bookkeeper's Verifier (2 rounds)"


def test_an_unverified_rerun_resets_the_status(session):
    S.ingest(session.root, put(session.root, V12))
    S.ingest(session.root, put(session.root, report("R-11", "2026-10-22", [FEE("F1"), CORRECT("F2")])))
    state = S.load(session.root)
    assert {e["verification"]["status"] for e in S.open_entries(state) for e in [e[1]]} == {"unverified"}
    r = tools.call(session, "get_findings", {})
    assert set(r["verification"]["per_finding"].values()) == {"unverified"} and r["verification_label"] == "not verified"


def test_a_report_cannot_claim_the_merged_map(session):
    """A raw report carrying an orchestrator-style map is still judged on its own verification block."""
    rep = report("R-forged", "2026-10-21", [CORRECT("F1")], open_verification={"F1": {"status": "agreed"}})
    put(session.root, rep)
    r = tools.call(session, "get_findings", {})
    assert {c["status"] for c in r["checks"]} == {"downgrade"}


def test_old_state_entries_without_verification_are_unverified(session):
    S.ingest(session.root, put(session.root, V12))
    state = S.load(session.root)
    for e in state["findings"].values():
        e.pop("verification")
    with S.lock(session.root):
        S.save(session.root, state)
    assert set(v["status"] for v in S.open_verification(S.load(session.root)).values()) == {"unverified"}


# ---------------------------------------------------------------- bk_runner
def test_timeouts_are_doubled_and_cli_defaults_follow():
    import inspect

    assert (R.SETUP_TIMEOUT, R.UPDATE_TIMEOUT) == (3600, 1200)
    assert inspect.signature(R.setup).parameters["timeout"].default == 3600
    assert inspect.signature(R.update).parameters["timeout"].default == 1200


def test_setup_reads_verified_from_the_cli_reply(session, bk, monkeypatch):
    def make(as_of, argv):
        return verified("x", as_of, [CORRECT("F1")], ["1 comparison round(s); the Bookkeeper and the Verifier "
                                                       "agreed on every finding."], rounds=1)

    class Verified(FakeRunner):
        def __call__(self, argv, **kw):
            out = super().__call__(argv, **kw)
            body = json.loads(out.stdout.split("\n", 1)[1])
            assert "status" not in body
            body["verified"] = True
            return subprocess.CompletedProcess(argv, 0, "INFO\n" + json.dumps(body), "")

    fake = Verified(session.root, make)
    said = []
    out = R.setup(session.root, runner=fake, progress=said.append)
    assert fake.calls[0][1]["timeout"] == 3600 and out["reply_verified"] is True and out["verified"] is True
    assert any("verified by its Verifier" in s for s in said) and "two investigations" in said[0]


def test_timeout_suggests_verify(session, bk):
    with pytest.raises(R.BookkeeperError, match=r"bookkeeper verify --report-id R-[0-9a-f]{64}`"):
        R.run_bookkeeper(session.root, timeout=5, runner=FakeRunner(session.root, raise_timeout=True))
    with pytest.raises(R.BookkeeperError, match="bookkeeper verify --report-id"):
        R.run_bookkeeper(session.root, runner=FakeRunner(
            session.root, returncode=1, stderr="Bookkeeper failed (RuntimeError): Verification failed: the snapshot "
                                               "changed since the Bookkeeper's run\n"))


def test_agent_replies(session):
    put(session.root, V12, "R-" + "a" * 64 + ".json")
    good = {"request_id": "orch-1", "report_id": "R-" + "a" * 64, "filename": f"data/reports/R-{'a' * 64}.json",
            "as_of": "2026-10-21", "verified": True}
    path, ver = R.adopt_reply(session.root, good, "orch-1")                  # no "status": "ok" needed
    assert path.name == f"R-{'a' * 64}.json" and ver is True
    err = {"request_id": "orch-1", "status": "error", "error": "Verification failed: cash balances differ",
           "error_type": "RuntimeError", "trace_id": TRACE}
    with pytest.raises(R.BookkeeperError, match=rf"failed: Verification failed.*resume --trace-id {TRACE}.*verify"):
        R.adopt_reply(session.root, err, "orch-1")
    for bad in [{**good, "filename": "../../etc/passwd"}, {**good, "request_id": "other"}, {"request_id": "orch-1"},
                ["not", "a", "dict"]]:
        with pytest.raises(R.BookkeeperError):
            R.adopt_reply(session.root, bad, "orch-1")


async def test_arun_agent_timeout_names_the_report_and_verify(session):
    async def never(payload):
        await asyncio.sleep(5)

    with pytest.raises(R.AgentTimeout, match=r"R-[0-9a-f]{64}\.json.*bookkeeper verify"):
        await R.arun_agent(session.root, never, {"request_id": "orch-bk-x"}, 0.05, "agent1qme")


# ---------------------------------------------------------------- the served orchestrator's Bookkeeper setup
def _service(session, **kw):
    from orchestrator.agent import Orchestrator
    return Orchestrator(session.root, set(), use_agents=True, bookkeeper="agent1qbk", address="agent1qme", **kw)


async def test_setup_is_delegated_to_the_bookkeeper_agent(session, bk, monkeypatch):
    from orchestrator.agent import chat_message
    from uagents_core.contrib.protocols.chat import TextContent

    fake = FakeRunner(session.root)
    monkeypatch.setattr(R.subprocess, "run", fake)
    svc = _service(session)
    sent = []

    async def send(dest, msg):
        body = json.loads(msg.content[0].text)
        sent.append(body)
        name = R.expected_report(body["request_id"], "agent1qme")
        put(session.root, verified(name[:-5], body["as_of"], [CORRECT("F1")], []), name)
        reply = {"request_id": body["request_id"], "report_id": name[:-5], "filename": f"data/reports/{name}",
                 "as_of": body["as_of"], "verified": True}
        asyncio.get_running_loop().call_soon(
            svc.replies.deliver, "agent1qbk", chat_message([TextContent(type="text", text=json.dumps(reply))]))

    out = await svc.bookkeeper_op(SimpleNamespace(send=send, logger=Mock()), "setup")
    assert out["status"] == "done" and out["via"] == "bookkeeper agent" and out["degraded"] is False
    assert set(sent[0]) == {"request_id", "nessie_tag", "as_of"} and fake.calls == []
    assert S.load(session.root)["baseline"]["filename"] == out["report"] and out["verified"] is True


async def test_agent_fails_fast_then_in_process_fallback_is_degraded(session, bk, monkeypatch):
    fake = FakeRunner(session.root)
    monkeypatch.setattr(R.subprocess, "run", fake)

    async def boom(dest, msg):
        raise ConnectionError("unreachable")

    out = await _service(session).bookkeeper_op(SimpleNamespace(send=boom, logger=Mock()), "setup")
    assert out["status"] == "done" and out["degraded"] is True and len(fake.calls) == 1
    assert "degraded: the Bookkeeper agent send failed" in out["notes"][0]


async def test_agent_that_ran_and_failed_is_not_rerun_in_process(session, bk, monkeypatch):
    from orchestrator.agent import chat_message
    from uagents_core.contrib.protocols.chat import TextContent

    fake = FakeRunner(session.root)
    monkeypatch.setattr(R.subprocess, "run", fake)
    svc = _service(session)

    async def send(dest, msg):
        body = json.loads(msg.content[0].text)
        reply = {"request_id": body["request_id"], "status": "error", "error": "ASI API returned HTTP 500",
                 "error_type": "RuntimeError", "trace_id": None}
        asyncio.get_running_loop().call_soon(
            svc.replies.deliver, "agent1qbk", chat_message([TextContent(type="text", text=json.dumps(reply))]))

    out = await svc.bookkeeper_op(SimpleNamespace(send=send, logger=Mock()), "setup")
    assert out["status"] == "error" and "HTTP 500" in out["error"] and fake.calls == []
    assert out["notes"] == ["Bookkeeper agent failed; not rerun in-process"]


async def test_agent_timeout_is_not_rerun_in_process(session, bk, monkeypatch):
    fake = FakeRunner(session.root)
    monkeypatch.setattr(R.subprocess, "run", fake)

    async def silent(dest, msg):
        return None

    svc = _service(session)
    out = await svc.bookkeeper_op(SimpleNamespace(send=silent, logger=Mock()), "setup", timeout=0.1)
    assert out["status"] == "error" and out["degraded"] is True and fake.calls == []
    assert "did not reply" in out["error"] and svc.bk_running is False
    svc.bk_running = True
    assert (await svc.bookkeeper_op(SimpleNamespace(send=silent), "update"))["status"] == "busy"
