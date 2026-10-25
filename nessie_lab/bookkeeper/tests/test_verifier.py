import json
import sqlite3

import pytest

from bookkeeper.contracts import ReportRequest, RevisionRequest, VerifyRequest
from bookkeeper.runner import Bookkeeper
from bookkeeper.verifier import Verifier, verify_in_process
from conftest import ScriptedClient, batch_of

PLAN = ["card_settlements", "cash_deposits", "delivery_payouts", "supplier:Supplier", "recurring_obligations",
        "unrecorded_charges"]


async def drafted(root, draft, request_id="v-1"):
    return await Bookkeeper(root, ScriptedClient(draft)).run(ReportRequest(request_id=request_id))


def verify_request(d):
    return VerifyRequest(request_id=d.request_id, report_id=d.report_id, draft=d.draft)


class NeverCalled:
    async def complete(self, messages, tools):
        raise AssertionError("the Verifier must not investigate again")


def with_impact(draft, cents):
    out = json.loads(json.dumps(draft))
    out["findings"][0]["amounts"].update(actual_cents=cents, impact_cents=cents)
    return out


class Learns(ScriptedClient):
    """A Bookkeeper that writes `fixed` once it is given the Verifier's feedback; records which batches were revised."""

    def __init__(self, draft, fixed):
        super().__init__(draft)
        self.fixed, self.revised = fixed, []

    async def complete(self, messages, tools):
        if not any("Verifier feedback" in str(m.get("content")) for m in messages):
            return await super().complete(messages, tools)
        if not any(m["role"] == "assistant" for m in messages):
            self.revised.append(batch_of(messages))
        original, self.draft = self.draft, self.fixed
        try:
            return await super().complete(messages, tools)
        finally:
            self.draft = original


async def test_feedback_round_fixes_a_disputed_finding(root, draft):
    bookkeeper_client = Learns(draft, with_impact(draft, 9000))
    bookkeeper = Bookkeeper(root, bookkeeper_client)
    verifier_client = ScriptedClient(with_impact(draft, 9000))
    first = await bookkeeper.run(ReportRequest(request_id="rounds"))
    result = await verify_in_process(bookkeeper, Verifier(root, verifier_client), first)
    assert result.verified is True and bookkeeper_client.revised == ["unrecorded_charges"]   # only the disputed batch
    assert verifier_client.calls == len(PLAN) + 1                                          # the Verifier investigated once
    report = json.loads((root / result.filename).read_text())
    assert [f["amounts"]["impact_cents"] for f in report["findings"]] == [9000]
    v = report["verification"]
    assert (v["passed"], v["rounds"], v["findings_checked"], v["findings_rejected"]) == (True, 2, 1, 0)
    assert v["notes"][0].startswith("2 comparison round(s); the Bookkeeper and the Verifier agreed")
    record = json.loads((root / "data/verifications" / first.report_id / "round-1.json").read_text())
    assert record["next"]["round"] == 2 and list(record["feedback"]) == ["unrecorded_charges"]
    item = record["feedback"]["unrecorded_charges"][0]
    assert item["kind"] == "disputed" and "impact 4000 vs 9000" in item["reason"]
    revised = json.loads((root / "data/drafts" / f"{first.report_id}.r2.json").read_text())
    trace = json.loads((root / "data/bookkeeper_traces" / f"{revised['bookkeeper']['trace_id']}.json").read_text())
    assert trace["round"] == 2 and [r["id"] for r in trace["revised"]] == ["unrecorded_charges"]
    assert [b["id"] for b in trace["batches"]] == PLAN and "Verifier feedback" in trace["revised"][0]["initial_messages"][-1]["content"]


async def test_verifier_wins_when_rounds_run_out(root, draft):
    d = await drafted(root, draft)

    class Skips(ScriptedClient):                      # finds the charge at 9000, and cannot review cash deposits
        async def complete(self, messages, tools):
            if batch_of(messages) == "cash_deposits":
                self.calls += 1
                return {"role": "assistant", "content": "not json"}
            return await super().complete(messages, tools)
    result = await Verifier(root, Skips(with_impact(draft, 9000)), max_rounds=1).verify(verify_request(d))
    report = json.loads((root / result.filename).read_text())
    assert result.verified is True and [f["amounts"]["impact_cents"] for f in report["findings"]] == [9000]
    assert report["summary"]["total_impact_cents"] == 9000
    v = report["verification"]
    assert (v["passed"], v["rounds"], v["findings_checked"], v["findings_rejected"]) == (True, 1, 1, 1)
    assert any(n.startswith("F1 (Supplier, unrecorded_charge): the Verifier's version replaces the Bookkeeper's F1 (impact 4000 vs 9000")
               for n in v["notes"])
    assert any(n.startswith("Verifier: Not reviewed: Cash deposits") for n in v["notes"])
    trace = json.loads((root / "data/verifier_traces" / f"{report['verifier']['trace_id']}.json").read_text())
    assert trace["status"] == "investigated" and trace["bookkeeper_trace_id"] == report["bookkeeper"]["trace_id"]
    assert "You are the Verifier" in trace["batches"][0]["initial_messages"][0]["content"]
    assert await Verifier(root, NeverCalled()).verify(verify_request(d)) == result          # idempotent by report id


async def test_finding_only_the_verifier_made_is_fed_back_then_published(root, draft):
    bookkeeper_client = Learns(dict(draft, findings=[]), dict(draft, findings=[]))         # never finds it
    bookkeeper = Bookkeeper(root, bookkeeper_client)
    first = await bookkeeper.run(ReportRequest(request_id="missed"))
    result = await verify_in_process(bookkeeper, Verifier(root, ScriptedClient(draft), max_rounds=2), first)
    assert bookkeeper_client.revised == ["unrecorded_charges"]
    report = json.loads((root / result.filename).read_text())
    assert [(f["id"], f["bank_ids"]) for f in report["findings"]] == [("F1", ["n2"])]
    v = report["verification"]
    assert (v["passed"], v["rounds"], v["findings_checked"], v["findings_rejected"]) == (True, 2, 0, 0)
    assert "F1 (Supplier, unrecorded_charge): found by the Verifier; the Bookkeeper did not report it." in v["notes"]
    item = json.loads((root / "data/verifications" / first.report_id / "round-1.json").read_text())["feedback"]["unrecorded_charges"][0]
    assert item["kind"] == "missed" and item["verifier_finding"]["bank_ids"] == ["n2"]


async def test_rounds_stop_when_a_revision_changes_nothing(root, draft):
    bookkeeper_client = Learns(draft, draft)                        # looks again, writes the same result
    bookkeeper = Bookkeeper(root, bookkeeper_client)
    first = await bookkeeper.run(ReportRequest(request_id="stalled"))
    result = await verify_in_process(bookkeeper, Verifier(root, ScriptedClient(with_impact(draft, 9000)), max_rounds=5), first)
    assert bookkeeper_client.revised == ["unrecorded_charges"]       # one revision, not four
    v = json.loads((root / result.filename).read_text())["verification"]
    assert v["rounds"] == 2 and "The last revision changed nothing, so the rounds stopped early." in v["notes"][0]
    records = root / "data/verifications" / first.report_id
    second = json.loads((records / "round-2.json").read_text())
    assert second["stalled"] is True and second["next"] is None
    assert second["disputes"] == json.loads((records / "round-1.json").read_text())["disputes"]


async def test_a_round_is_compared_once(root, draft):
    d = await drafted(root, draft)
    step = await Verifier(root, ScriptedClient(with_impact(draft, 9000))).verify(verify_request(d))
    assert isinstance(step, RevisionRequest) and step.round == 2
    assert step.feedback == f"data/verifications/{d.report_id}/round-1.json"
    assert await Verifier(root, NeverCalled()).verify(verify_request(d)) == step        # re-sent round: same next step, no calls


async def test_rejects_drafts_outside_the_drafts_folder(root, draft):
    d = await drafted(root, draft)
    verifier = Verifier(root, NeverCalled())
    for path in ["data/reports/requests/../../drafts/../bookkeeper_traces/x.json", f"data/drafts/../{d.report_id}.json",
                 "data/drafts/R-" + "0" * 64 + ".json"]:
        with pytest.raises(ValueError, match="Verification failed"):
            await verifier.verify(VerifyRequest(request_id=d.request_id, report_id=d.report_id, draft=path))
    with pytest.raises(ValueError, match="another request"):
        await verifier.verify(VerifyRequest(request_id="other", report_id=d.report_id, draft=d.draft))
    assert not list((root / "data/reports").glob("R-*.json"))


async def test_changed_snapshot_is_not_verified(root, draft):
    d = await drafted(root, draft)
    with sqlite3.connect(root / "data/nessie_sync_boh-1loc.db") as con:
        con.execute("INSERT INTO bank_tx (nessie_id, account, nessie_type, date, amount, status, counterparty, memo, bank_id) "
                    "VALUES ('late', 'CHS', 'deposit', '2026-01-09', 100, 'completed', NULL, 'BRANCH DEPOSIT', 'Tlate')")
    with pytest.raises(RuntimeError, match="snapshot changed") as failure:
        await Verifier(root, ScriptedClient(draft)).verify(verify_request(d))
    assert not list((root / "data/reports").glob("R-*.json"))
    trace = json.loads((root / "data/verifier_traces" / f"{failure.value.bookkeeper_trace_id}.json").read_text())
    bank_rows = json.loads((root / d.draft).read_text())["sources"]["bank_rows"]
    assert trace["status"] == "investigated" and trace["sources"]["bank_rows"] == bank_rows + 1       # the row added after the draft


async def test_verifier_api_failure_publishes_nothing(root, draft):
    d = await drafted(root, draft)

    class Down:
        async def complete(self, messages, tools):
            raise RuntimeError("ASI API returned HTTP 500")
    with pytest.raises(RuntimeError, match="HTTP 500") as failure:
        await Verifier(root, Down()).verify(verify_request(d))
    assert not list((root / "data/reports").glob("R-*.json"))
    assert (root / "data/verifier_traces" / f"{failure.value.bookkeeper_trace_id}.json").exists()
    result = await Verifier(root, ScriptedClient(draft)).verify(verify_request(d))          # a retry verifies the same draft
    assert result.verified is True


def test_verifier_model_settings(root, monkeypatch):
    monkeypatch.setenv("ASI_MODEL", "asi1-bk")
    assert Verifier(root).model == "asi1-bk" and Verifier(root).investigator.temperature == 0
    monkeypatch.setenv("VERIFIER_MODEL", "asi1-other")
    monkeypatch.setenv("VERIFIER_TEMPERATURE", "0.3")
    verifier = Verifier(root)
    assert verifier.model == "asi1-other" and verifier.investigator.temperature == 0.3
    assert verifier.investigator.traces == root.resolve() / "data/verifier_traces"
