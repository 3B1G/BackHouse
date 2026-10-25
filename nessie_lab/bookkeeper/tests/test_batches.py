import json

import pytest

from bookkeeper.batches import MEMO_INDEX_ROWS, Batch, assemble_draft, memo_index, opening_messages, plan_batches
from bookkeeper.bookkeeper_tools import ToolSession
from bookkeeper.contracts import BatchResult
from conftest import CUTOFF


def test_plan_areas_tools_and_windows(root):
    s = ToolSession(root, "boh-1loc", CUTOFF)
    plan = plan_batches(s)
    assert [b.id for b in plan] == ["card_settlements", "cash_deposits", "delivery_payouts", "supplier:Supplier",
                                    "recurring_obligations", "unrecorded_charges"]
    by = {b.id: b for b in plan}
    assert all((b.date_from, b.date_to) == ("2026-01-01", CUTOFF) for b in plan)          # every batch spans the window
    assert "get_card_batches" in by["card_settlements"].tools and "search_costs" not in by["card_settlements"].tools
    assert "get_account_balances" in by["recurring_obligations"].tools
    assert [b.max_turns for b in plan] == [6, 6, 6, 10, 12, 10]


def test_memo_index_is_area_scoped_and_capped(root):
    s = ToolSession(root, "boh-1loc", CUTOFF)
    by = {b.id: b for b in plan_batches(s)}
    card, _ = memo_index(s, by["card_settlements"])
    assert [g["memo_pattern"] for g in card] == ["TABLETAP PAYMENTS DEP ##"] and card[0]["count"] == 60
    supplier, _ = memo_index(s, by["supplier:Supplier"])
    assert {g["memo_pattern"] for g in supplier} == {"ACH SUPPLIER"} and supplier[0]["count"] == 3     # "pre" is before the window, n8 after the cutoff
    outflows, total = memo_index(s, by["unrecorded_charges"])
    assert all(g["nessie_type"] != "deposit" for g in outflows) and total == len(outflows)
    wide = Batch("x", "x", "2026-01-01", CUTOFF, "", (), 1, ("",))
    s.bank = s.bank.assign(memo=[f"MEMO {chr(65 + i % 26)}{chr(65 + i // 26)}" for i in range(len(s.bank))],
                           nessie_type="purchase")
    index, total = memo_index(s, wide)
    assert len(index) == MEMO_INDEX_ROWS and total > MEMO_INDEX_ROWS


def test_opening_messages_are_small_and_self_contained(root):
    s = ToolSession(root, "boh-1loc", CUTOFF)
    batch = next(b for b in plan_batches(s) if b.id == "supplier:Supplier")
    system, user = opening_messages(batch, s, {"total_cents": 1}, s.limitations)
    assert "\nBatch: supplier:Supplier\n" in system["content"] and "BatchResult schema" in system["content"]
    context = json.loads(user["content"])
    assert context["window"] == {"from": "2026-01-01", "to": CUTOFF} and "balances" not in context
    assert len(system["content"]) + len(user["content"]) < 15000


# ---------------------------------------------------------------- assembly
def finding(id, root_cause="card_fee_creep", counterparty="TableTap Payments", first="2026-08-01", last="2026-08-31",
            bank_ids=("a",), impact=100, kind="ongoing", confidence=0.9, verdict="error", recoverable=True, **treatment):
    t = {"ongoing": dict(kind="ongoing", since=first, monthly_cents=impact), "exclude": dict(kind="exclude", bank_ids=list(bank_ids)),
         "correct_to": dict(kind="correct_to", bank_id=bank_ids[0], cents=1), "owed": dict(kind="owed", cents=impact, due=last)}[kind]
    t.update(treatment)
    return dict(id=id, root_cause=root_cause, verdict=verdict, confidence=confidence, counterparty=counterparty,
                dates=dict(first=first, last=last), bank_ids=list(bank_ids), books_refs=[], amounts=dict(impact_cents=impact),
                explanation=f"{id}.", evidence=[dict(type="bank_row", ref=bank_ids[0])], treatment=t, recoverable=recoverable,
                suggested_action="Ask.", assumption_if_unconfirmed=None)


def result(*findings, cleared=(), reviewed=10, limitations=()):
    return BatchResult.model_validate(dict(coverage=dict(bank_rows_reviewed=reviewed, matched_to_documents=reviewed, investigated=1,
                                                         cleared_as_ok=len(cleared)),
                                           findings=list(findings), cleared=list(cleared), limitations=list(limitations)))


def b(id, title=None):
    return Batch(id, title or id, "2026-01-01", "2026-12-31", "", (), 1)


def test_cross_quarter_pattern_merges_into_one_finding():
    q3 = result(finding("Q3-1", first="2026-07-01", last="2026-07-24", bank_ids=("a", "b"), impact=300,
                        **{"monthly_cents": 300}))
    q4 = result(finding("Q4-1", first="2026-07-27", last="2026-10-22", bank_ids=("c",), impact=900, confidence=0.7,
                        **{"monthly_cents": 450}))
    draft, info = assemble_draft([(b("card:Q3"), q3), (b("card:Q4"), q4)], [], bank_rows=1000)
    [f] = draft.findings
    assert f.id == "F1" and f.bank_ids == ["a", "b", "c"] and f.amounts.impact_cents == 1200 and f.confidence == 0.7
    assert (str(f.dates.first), str(f.dates.last)) == ("2026-07-01", "2026-10-22")
    assert f.treatment.kind == "ongoing" and str(f.treatment.since) == "2026-07-01" and f.treatment.monthly_cents == 450
    assert draft.summary.ongoing_monthly_cents == 450 and info["merged"] == [["Q3-1", "Q4-1", "card:Q4"]]


def test_distant_or_different_findings_stay_separate():
    far = [finding("A", first="2026-01-01", last="2026-01-31"), finding("B", first="2026-06-01", last="2026-06-30", bank_ids=("z",))]
    other = finding("C", root_cause="commission_overcharge", counterparty="RapidPlate", bank_ids=("y",))
    corrections = [finding("D", root_cause="supplier_overcharge", counterparty="Supplier", bank_ids=("p",), kind="correct_to"),
                   finding("E", root_cause="supplier_overcharge", counterparty="Supplier", bank_ids=("q",), kind="correct_to")]
    draft, info = assemble_draft([(b("x"), result(*far, other, *corrections))], [], bank_rows=1000)
    assert [f.id for f in draft.findings] == ["F1", "F2", "F3", "F4", "F5"]
    assert info["origin"] == {f"F{n}": ["x"] for n in range(1, 6)}


def test_exclude_findings_union_their_rows():
    one = finding("A", root_cause="duplicate_charge", counterparty="Supplier", bank_ids=("d1",), kind="exclude", impact=50)
    two = finding("B", root_cause="duplicate_charge", counterparty="supplier ", first="2026-08-10", last="2026-08-10",
                  bank_ids=("d2",), kind="exclude", impact=70)
    draft, _ = assemble_draft([(b("s"), result(one)), (b("r"), result(two))], [], bank_rows=1000)
    [f] = draft.findings
    assert f.treatment.bank_ids == ["d1", "d2"] and f.amounts.impact_cents == 120


def test_same_rows_seen_by_two_batches_are_not_counted_twice():
    def charge(id, impact=34900, confidence=0.9):
        f = finding(id, root_cause="unrecorded_charge", counterparty="TableTap POS", first="2026-07-09", last="2026-07-09",
                    bank_ids=("h1",), kind="exclude", impact=impact, confidence=confidence)
        f["amounts"].update(expected_cents=0, actual_cents=impact)
        return f
    draft, info = assemble_draft([(b("recurring_obligations"), result(charge("RO-01"))),
                                  (b("unrecorded_charges"), result(charge("UNR-001", confidence=0.8)))], [], bank_rows=1000)
    [f] = draft.findings
    assert f.treatment.bank_ids == ["h1"] and f.amounts.impact_cents == 34900 and f.amounts.actual_cents == 34900
    assert draft.summary.total_impact_cents == 34900 and f.confidence == 0.8 and info["merged"] == [["RO-01", "UNR-001", "unrecorded_charges"]]
    assert info["origin"] == {"F1": ["recurring_obligations", "unrecorded_charges"]}
    partial = [result(charge("A", impact=500) | {"bank_ids": ["h1", "h2"], "treatment": {"kind": "exclude", "bank_ids": ["h1", "h2"]}}),
               result(charge("B", impact=300) | {"bank_ids": ["h2"], "treatment": {"kind": "exclude", "bank_ids": ["h2"]}})]
    draft, _ = assemble_draft([(b("one"), partial[0]), (b("two"), partial[1])], [], bank_rows=1000)
    assert [f.amounts.impact_cents for f in draft.findings] == [500]         # overlapping rows: the larger view, not 800


def test_conflicting_treatments_keep_the_more_confident_and_say_so():
    dup = finding("A", root_cause="duplicate_charge", counterparty="Supplier", bank_ids=("x",), kind="exclude", confidence=0.6)
    unrec = finding("B", root_cause="unrecorded_charge", counterparty="Unknown", bank_ids=("x",), kind="exclude", confidence=0.8,
                    verdict="needs_owner")
    draft, _ = assemble_draft([(b("s"), result(dup)), (b("u"), result(unrec))], [], bank_rows=1000)
    [f] = draft.findings
    assert f.root_cause == "unrecorded_charge" and any("treated the same bank rows differently" in l for l in draft.limitations)


def test_summary_coverage_cleared_and_unreviewed():
    findings = [finding("A", verdict="error", recoverable=True, impact=100, bank_ids=("a",), kind="exclude", root_cause="duplicate_charge"),
                finding("B", verdict="needs_owner", recoverable="unknown", impact=40, bank_ids=("b",), kind="exclude",
                        root_cause="unrecorded_charge", counterparty="X"),
                finding("C", verdict="error", recoverable=False, impact=25, bank_ids=("c",), kind="ongoing", root_cause="card_fee_creep",
                        counterparty="Y", **{"monthly_cents": 7})]
    cleared = [dict(bank_ids=["a"], reason="looked fine"), dict(bank_ids=["k"], reason="matches invoice")]
    draft, info = assemble_draft([(b("one"), result(*findings[:2], cleared=cleared, reviewed=700, limitations=["L1"])),
                                  (b("two"), result(findings[2], reviewed=700, limitations=["L1", "L2"]))],
                                 [(b("cash:Q2", "Cash deposits Q2"), "its result was cut off at the output limit")], bank_rows=1000)
    assert draft.summary.model_dump() == dict(errors=2, needs_owner=1, total_impact_cents=165, recoverable_cents=100,
                                              ongoing_monthly_cents=7)
    assert draft.coverage.bank_rows_reviewed == 1000 and draft.coverage.investigated == 2      # capped at bank_rows; summed
    assert [c.bank_ids for c in draft.cleared] == [["k"]] and info["dropped_cleared"] == 1
    assert draft.limitations[0] == "Not reviewed: Cash deposits Q2 (its result was cut off at the output limit)."
    assert draft.limitations.count("L1") == 1 and "L2" in draft.limitations
    assert info["unreviewed"] == [["cash:Q2", "its result was cut off at the output limit"]]


def test_empty_results_assemble_to_an_empty_draft():
    draft, _ = assemble_draft([(b("a"), result())], [], bank_rows=5)
    assert draft.findings == [] and draft.summary.total_impact_cents == 0 and draft.coverage.bank_rows_reviewed == 5


# ---------------------------------------------------------------- hand-off rules
def test_bank_ids_in_collects_nested_ids():
    from bookkeeper.batches import bank_ids_in
    results = [{"rows": [{"bank_id": "a", "bank_ids": ["b", "c"]}], "unmatched_deposits": [{"bank_id": "d"}]},
               {"transaction": {"bank_id": "e"}, "same_amount_within_14_days": [{"bank_id": "f", "memo": "x"}]}, {"error": "no"}]
    assert bank_ids_in(results) == set("abcdef")


def _no_rows(raw):
    raw.update(bank_ids=[], books_refs=[], treatment=dict(kind="ongoing", since="2026-08-01", monthly_cents=5))


@pytest.mark.parametrize("edit,problem", [
    (lambda raw: raw.update(evidence=[]), "cites no evidence"),
    (_no_rows, "names no bank row or document"),
    (lambda raw: raw.update(treatment=dict(kind="ongoing", since="2026-08-01", monthly_cents=0)), "monthly_cents is not above zero"),
    (lambda raw: raw.update(treatment=dict(kind="correct_to", bank_id="a", cents=0)), "correct_to cents is not above zero"),
    (lambda raw: raw.update(treatment=dict(kind="owed", cents=100, due="2026-10-24")), "names no document in books_refs"),
    (lambda raw: raw.update(books_refs=["DOC-1"], treatment=dict(kind="owed", cents=0, due="2026-10-24")), "owed cents is not above zero"),
    (lambda raw: raw.update(bank_ids=["zzz"]), "no tool returned in this batch: zzz"),
    (lambda raw: raw.update(treatment=dict(kind="exclude", bank_ids=["a", "made-up"])), "no tool returned in this batch: made-up"),
    (lambda raw: raw.update(treatment=dict(kind="correct_to", bank_id="a", cents=100)),
     "duplicate_charge findings take the exclude treatment, not correct_to"),
])
def test_finding_problems_mirror_the_contracts_hand_off(edit, problem):
    from bookkeeper.batches import finding_problems
    from bookkeeper.contracts import Finding
    raw = finding("F", root_cause="duplicate_charge", kind="exclude")
    edit(raw)
    problems = finding_problems(Finding.model_validate(raw), seen={"a"})
    assert any(problem in p for p in problems), problems


def test_valid_findings_have_no_problems():
    from bookkeeper.batches import finding_problems
    from bookkeeper.contracts import Finding
    for raw in [finding("A", root_cause="duplicate_charge", kind="exclude"), finding("B", kind="ongoing"),
                finding("C", root_cause="supplier_overcharge", kind="correct_to"),
                dict(finding("D", root_cause="missed_payment", kind="owed", bank_ids=("a",)), books_refs=["DOC-1"])]:
        assert finding_problems(Finding.model_validate(raw), seen={"a"}) == []


def test_every_root_cause_has_one_treatment_and_the_prompt_says_which():
    from typing import get_args

    from bookkeeper.batches import CORE, TREATMENT
    from bookkeeper.contracts import Finding
    assert set(TREATMENT) == set(get_args(Finding.model_fields["root_cause"].annotation))
    for cause, kind in TREATMENT.items():
        assert f"- {cause}: {kind} (" in CORE
    assert "one finding per row" in CORE


def test_invoiced_price_increases_are_not_findings(root):
    from bookkeeper.batches import CORE
    session = ToolSession(root, "boh-1loc", CUTOFF, mode="historical")
    try:
        supplier = next(b for b in plan_batches(session) if b.id.startswith("supplier:"))
    finally:
        session.close()
    assert "price change, not an error" in supplier.brief and "never as a finding" in supplier.brief
    assert "supplier_overcharge means a payment above its invoice" in CORE
