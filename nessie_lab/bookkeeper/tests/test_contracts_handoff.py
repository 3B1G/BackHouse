"""Verified Bookkeeper reports must pass the team's contracts hand-off (nessie_lab/contracts.py, used as is) without losing findings."""
import importlib.util
import json
from pathlib import Path

import pytest

from bookkeeper.contracts import ReportRequest
from bookkeeper.runner import Bookkeeper
from bookkeeper.verifier import Verifier, verify_in_process
from conftest import ScriptedClient, bank_rows

CONTRACTS = Path(__file__).resolve().parents[2] / "contracts.py"


@pytest.fixture(scope="module")
def contracts():
    if not CONTRACTS.is_file():
        pytest.skip("nessie_lab/contracts.py is not in this checkout")
    spec = importlib.util.spec_from_file_location("team_contracts", CONTRACTS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def id_map():
    """What bookkeeper_tools.bank_id_map() returns, with grading ids in the contracts' T + 9 hex form."""
    return {row[0]: f"T{n:09X}" for n, row in enumerate(bank_rows())}


def finding(id, root_cause, bank_ids, treatment, books_refs=(), **kw):
    return dict(id=id, root_cause=root_cause, verdict="error", confidence=0.8, counterparty="Supplier",
                dates=dict(first="2026-01-03", last="2026-01-08"), bank_ids=list(bank_ids), books_refs=list(books_refs),
                amounts=dict(impact_cents=4000), explanation=f"{id} explained. More detail.",
                evidence=[dict(type="bank_row", ref=(list(bank_ids) or ["now"])[0])], treatment=treatment, recoverable=True,
                suggested_action="Ask.", **kw)


EVERY_KIND = dict(
    coverage=dict(bank_rows_reviewed=10, matched_to_documents=5, investigated=4, cleared_as_ok=0),
    findings=[finding("D", "duplicate_charge", ["n7"], {"kind": "exclude", "bank_ids": ["n7"]}),
              finding("O", "supplier_overcharge", ["n2"], {"kind": "correct_to", "bank_id": "n2", "cents": 3000}, ["now"]),
              finding("C", "card_fee_creep", ["n2"], {"kind": "ongoing", "since": "2026-01-01", "monthly_cents": 120}),
              finding("M", "missed_payment", [], {"kind": "owed", "cents": 4000, "due": "2026-01-20"}, ["now"]),
              finding("X", "duplicate_charge", ["made-up"], {"kind": "exclude", "bank_ids": ["made-up"]})],   # left out by Bookkeeper
    cleared=[], limitations=[])


async def test_every_treatment_kind_passes_the_hand_off(root, contracts):
    calls = ScriptedClient.CALLS + [("get_bank_transaction", {"bank_id": "n7"})]      # n7 is past the first page
    service = Bookkeeper(root, ScriptedClient(EVERY_KIND, calls))
    result = await verify_in_process(service, Verifier(root, ScriptedClient(EVERY_KIND, calls)),
                                     await service.run(ReportRequest(request_id="handoff")))
    report = json.loads((root / result.filename).read_text())
    assert result.verified and report["verification"]["findings_checked"] == 4
    findings, adjustments, rejected = contracts.from_bk_report(report, id_map())
    assert rejected == {}
    assert sorted(f.issue_type for f in findings) == ["duplicate_charge", "fee_rate_change", "missed_payment", "overcharge"]
    assert sorted(a.kind for a in adjustments) == ["correct_row", "exclude_row", "ongoing_overcharge", "unpaid_bill"]
    assert all(b.startswith("T") for f in findings for b in f.bank_ids)
    assert any(l.startswith("Left out a Charges with no supporting document finding") for l in report["limitations"])
    flags = contracts.findings_to_flags(findings)
    assert {"books_ref": "now", "finding_id": next(f.finding_id for f in findings if f.issue_type == "missed_payment")} in flags


async def test_scripted_report_converts_from_a_file_path(root, draft, contracts):
    service = Bookkeeper(root, ScriptedClient(draft))
    result = await verify_in_process(service, Verifier(root, ScriptedClient(draft)),
                                     await service.run(ReportRequest(request_id="path")))
    findings, adjustments, rejected = contracts.from_bk_report(root / result.filename, id_map())
    assert rejected == {} and [f.bank_ids for f in findings] == [[id_map()["n2"]]]
    assert [a.kind for a in adjustments] == ["exclude_row"]
