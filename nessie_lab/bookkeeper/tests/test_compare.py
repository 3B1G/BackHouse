from bookkeeper.compare import close, compare, feedback, pair_findings
from bookkeeper.contracts import Finding

KNOWN = frozenset({"n1", "n2", "n3", "n7"})


def finding(id, root_cause="duplicate_charge", bank_ids=("n2",), treatment=None, books_refs=(), impact=4000, **kw):
    return Finding.model_validate(dict(
        dict(id=id, root_cause=root_cause, verdict="error", confidence=0.8, counterparty="Supplier",
             dates=dict(first="2026-01-03", last="2026-01-08"), bank_ids=list(bank_ids), books_refs=list(books_refs),
             amounts=dict(impact_cents=impact), explanation="x", evidence=[dict(type="bank_row", ref="n2")],
             treatment=treatment or {"kind": "exclude", "bank_ids": list(bank_ids)}, recoverable=True, suggested_action="Ask."),
        **kw))


def test_tolerance_is_ten_dollars_or_five_percent():
    assert close(4000, 5000) and not close(4000, 5001)
    assert close(100_000, 105_000) and not close(100_000, 105_300)


def test_pairs_by_rows_then_documents_then_counterparty():
    bk = [finding("A", bank_ids=["n2"]), finding("B", "missed_payment", [], {"kind": "owed", "cents": 4000, "due": "2026-01-20"}, ["now"]),
          finding("C", "card_fee_creep", [], {"kind": "ongoing", "since": "2026-01-01", "monthly_cents": 500})]
    mine = [finding("v3", "card_fee_creep", [], {"kind": "ongoing", "since": "2026-01-05", "monthly_cents": 520}),
            finding("v1", bank_ids=["n7"]), finding("v2", bank_ids=["n2", "n3"]),
            finding("v4", "missed_payment", [], {"kind": "owed", "cents": 4100, "due": "2026-01-20"}, ["now"])]
    assert [(a.id, b.id) for a, b in pair_findings(bk, mine)] == [("A", "v2"), ("B", "v4"), ("C", "v3")]
    assert pair_findings([finding("A", "unrecorded_charge")], mine) == [(finding("A", "unrecorded_charge"), None)]


def test_agreement_publishes_bookkeeper_findings_and_reports_extras():
    bk = [finding("F1"), finding("F2", "supplier_overcharge", ["n1"], {"kind": "correct_to", "bank_id": "n1", "cents": 3000})]
    mine = [finding("v1", impact=4500), finding("v2", "supplier_overcharge", ["n1"], {"kind": "correct_to", "bank_id": "n1", "cents": 3050}),
            finding("v3", "unrecorded_charge", ["n3"])]
    result = compare(bk, mine, KNOWN)
    assert result.agreed == bk and result.rejected == [] and [f.id for f in result.extra] == ["v3"]
    assert result.pairs == [["F1", "v1"], ["F2", "v2"]]


def test_disagreements_reject_the_finding():
    owed = {"kind": "owed", "cents": 4000, "due": "2026-01-20"}
    cases = [
        (finding("F1"), finding("v", treatment={"kind": "correct_to", "bank_id": "n2", "cents": 1}), "treatment exclude"),
        (finding("F1", bank_ids=["n2", "n3"]), finding("v", bank_ids=["n2"]), "excludes different bank rows"),
        (finding("F1", "supplier_overcharge", ["n1", "n2"], {"kind": "correct_to", "bank_id": "n1", "cents": 3000}),
         finding("v", "supplier_overcharge", ["n2"], {"kind": "correct_to", "bank_id": "n2", "cents": 3000}), "different bank row"),
        (finding("F1", "card_fee_creep", ["n2"], {"kind": "ongoing", "since": "2026-01-01", "monthly_cents": 500}),
         finding("v", "card_fee_creep", ["n2"], {"kind": "ongoing", "since": "2026-02-01", "monthly_cents": 500}), "ongoing since"),
        (finding("F1", "missed_payment", [], owed, ["now"]), finding("v", "missed_payment", [], owed, ["bulk", "now"]),
         "different document"),
        (finding("F1", impact=4000), finding("v", impact=9000), "impact 4000 vs 9000"),
    ]
    for bk, mine, reason in cases:
        result = compare([bk], [mine], KNOWN)
        assert result.agreed == [] and len(result.rejected) == 1 and reason in result.rejected[0][1], reason


def test_unmatched_and_unknown_rows_are_rejected():
    result = compare([finding("F1"), finding("F2", bank_ids=["ghost"])], [finding("v", bank_ids=["ghost"])], KNOWN)
    assert [(f.id, reason, b and b.id) for f, reason, b in result.rejected] == [
        ("F1", "the Verifier's investigation found no matching issue", None),
        ("F2", "it cites bank ids that are not in the snapshot: ghost", "v")]


def test_the_verifiers_verdict_is_used():
    bk = finding("F1", verdict="needs_owner", confidence=0.9, assumption_if_unconfirmed="One-off")
    [agreed] = compare([bk], [finding("v", verdict="error", confidence=0.6)], KNOWN).agreed
    assert agreed.verdict == "error" and agreed.confidence == 0.6 and agreed.assumption_if_unconfirmed == "One-off"


def test_feedback_goes_to_the_batch_that_should_look_again():
    bk = [finding("F1", impact=4000), finding("F2", "missed_payment", [], {"kind": "owed", "cents": 100, "due": "2026-01-20"}, ["x"])]
    mine = [finding("V1", impact=9000), finding("V2", "unrecorded_charge", ["n3"])]
    result = compare(bk, mine, KNOWN)
    routed = feedback(result, {"F1": ["supplier:A"], "F2": ["recurring"]}, {"V1": ["supplier:A"], "V2": ["unrecorded"]})
    assert set(routed) == {"supplier:A", "recurring", "unrecorded"}
    [disputed] = routed["supplier:A"]
    assert disputed["kind"] == "disputed" and disputed["your_finding"]["id"] == "F1" and "impact 4000 vs 9000" in disputed["reason"]
    assert disputed["verifier_finding"]["id"] == "V1"
    assert routed["recurring"][0]["verifier_finding"] is None and "found no matching issue" in routed["recurring"][0]["reason"]
    assert routed["unrecorded"] == [{"kind": "missed", "verifier_finding": mine[1].model_dump(mode="json")}]
    assert feedback(result, {}, {}) == {}                       # no origin: nothing can be routed


def test_per_row_corrections_pair_by_their_target_row():
    rows = ["n1", "n2", "n3"]

    def week(id, row, cents=3000):
        return finding(id, "commission_overcharge", rows, {"kind": "correct_to", "bank_id": row, "cents": cents})
    bk = [week("F1", "n1"), week("F2", "n2"), week("F3", "n3")]
    mine = [week("v3", "n3", 3050), week("v1", "n1"), week("v2", "n2", 2990)]
    assert [(a.id, b.id) for a, b in pair_findings(bk, mine)] == [("F1", "v1"), ("F2", "v2"), ("F3", "v3")]
    assert compare(bk, mine, KNOWN).agreed == bk
