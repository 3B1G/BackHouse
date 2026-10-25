"""The Verifier's comparison: which of the Bookkeeper's findings its own independent investigation agrees with, and the
feedback that sends each disagreement back to the Bookkeeper batch that should look again.

Two model runs rarely word or number things identically, so findings are matched by key, not by equality: the same
root cause on the same bank rows, documents or counterparty, with the same treatment and amounts within a tolerance.
Cleared items, coverage and wording are not compared.
"""
from dataclasses import dataclass, field

from .batches import _targets
from .contracts import Finding

TOLERANCE_CENTS = 1000          # $10: the books' materiality threshold
TOLERANCE_PCT = 5               # or 5% of the larger amount, whichever is more
SINCE_DAYS = 7                  # an ongoing problem's start date


@dataclass
class Comparison:
    agreed: list = field(default_factory=list)         # the Bookkeeper's findings, with the Verifier's verdict
    rejected: list = field(default_factory=list)       # [(Bookkeeper finding, reason, paired Verifier finding or None)]
    extra: list = field(default_factory=list)          # findings only the Verifier made
    notes: list = field(default_factory=list)
    pairs: list = field(default_factory=list)          # [[Bookkeeper id, Verifier id or None]] for the trace


def close(a, b):
    """Two cent amounts agree within the tolerance."""
    return abs(a - b) <= max(TOLERANCE_CENTS, TOLERANCE_PCT * max(abs(a), abs(b)) / 100)


def _rows(f):
    return set(f.bank_ids) | _targets(f)


def _party(f):
    return " ".join(f.counterparty.lower().split())


def overlap(a, b):
    """How strongly two findings of the same root cause describe the same issue (0 = not at all)."""
    if a.root_cause != b.root_cause:
        return 0
    targets = len(_targets(a) & _targets(b))             # per-row corrections often share every row in bank_ids
    rows, refs = len(_rows(a) & _rows(b)), len(set(a.books_refs) & set(b.books_refs))
    if rows or refs:
        return 2 + 10 * targets + rows + refs
    return 1 if _party(a) == _party(b) else 0


def pair_findings(bk, mine):
    """Greedy pairing, strongest overlap first: [(Bookkeeper finding, Verifier finding or None)] in Bookkeeper order."""
    scores = sorted(((overlap(a, b), i, j) for i, a in enumerate(bk) for j, b in enumerate(mine)), key=lambda x: (-x[0], x[1], x[2]))
    paired, used = {}, set()
    for score, i, j in scores:
        if score and i not in paired and j not in used:
            paired[i] = j
            used.add(j)
    return [(a, mine[paired[i]] if i in paired else None) for i, a in enumerate(bk)]


def disagreements(a, b):
    """Why two paired findings disagree (empty when they agree)."""
    ta, tb = a.treatment, b.treatment
    if ta.kind != tb.kind:
        return [f"treatment {ta.kind} vs the Verifier's {tb.kind}"]
    problems = []
    if ta.kind == "exclude" and set(ta.bank_ids) != set(tb.bank_ids):
        problems.append("it excludes different bank rows")
    if ta.kind == "correct_to":
        if ta.bank_id != tb.bank_id:
            problems.append("it corrects a different bank row")
        elif not close(ta.cents, tb.cents):
            problems.append(f"correct_to {ta.cents} vs {tb.cents} cents")
    if ta.kind == "ongoing":
        if not close(ta.monthly_cents, tb.monthly_cents):
            problems.append(f"ongoing {ta.monthly_cents} vs {tb.monthly_cents} cents a month")
        if abs((ta.since - tb.since).days) > SINCE_DAYS:
            problems.append(f"ongoing since {ta.since} vs {tb.since}")
    if ta.kind == "owed":
        if a.books_refs[:1] != b.books_refs[:1]:
            problems.append("it is owed on a different document")
        elif not close(ta.cents, tb.cents):
            problems.append(f"owed {ta.cents} vs {tb.cents} cents")
    if not close(a.amounts.impact_cents, b.amounts.impact_cents):
        problems.append(f"impact {a.amounts.impact_cents} vs {b.amounts.impact_cents} cents")
    return problems


def compare(bk, mine, known_bank_ids):
    """Compare the Bookkeeper's findings with the Verifier's. On a verdict difference the Verifier's verdict is used."""
    out = Comparison()
    pairs = pair_findings(bk, mine)
    for a, b in pairs:
        out.pairs.append([a.id, b.id if b else None])
        unknown = sorted(_rows(a) - known_bank_ids)
        if unknown:
            out.rejected.append((a, f"it cites bank ids that are not in the snapshot: {', '.join(unknown[:5])}", b))
        elif b is None:
            out.rejected.append((a, "the Verifier's investigation found no matching issue", None))
        elif problems := disagreements(a, b):
            out.rejected.append((a, "; ".join(problems), b))
        elif a.verdict != b.verdict:
            out.agreed.append(Finding.model_validate(dict(
                a.model_dump(), verdict=b.verdict, confidence=min(a.confidence, b.confidence),
                assumption_if_unconfirmed=a.assumption_if_unconfirmed or b.assumption_if_unconfirmed)))
            out.notes.append(f"{a.id}: the Bookkeeper said {a.verdict} and the Verifier {b.verdict}; the Verifier's verdict is used.")
        else:
            out.agreed.append(a)
    matched = {id(b) for _, b in pairs if b is not None}
    out.extra = [b for b in mine if id(b) not in matched]
    return out


def feedback(comparison, bk_origin, verifier_origin):
    """{batch id: [items]}: each disagreement, for the Bookkeeper batch that should look again.

    A disputed Bookkeeper finding goes to the batch that produced it, with the Verifier's version when there is one; an
    issue only the Verifier found goes to the batch the Verifier found it in (both plans have the same batch ids)."""
    routed = {}
    for a, reason, b in comparison.rejected:
        item = {"kind": "disputed", "your_finding": a.model_dump(mode="json"), "reason": reason,
                "verifier_finding": b.model_dump(mode="json") if b else None}
        for batch in bk_origin.get(a.id) or (verifier_origin.get(b.id, []) if b else []):
            routed.setdefault(batch, []).append(item)
    for b in comparison.extra:
        for batch in verifier_origin.get(b.id, []):
            routed.setdefault(batch, []).append({"kind": "missed", "verifier_finding": b.model_dump(mode="json")})
    return routed
