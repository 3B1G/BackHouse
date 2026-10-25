"""The orchestrator's checks on the Bookkeeper's findings (replaces the direct use of checks_interim).

THE OWNER DECIDES EACH FINDING (ORCHESTRATOR.md, "owner review before forecasting"): accept, dismiss or revise. An LLM
never records that decision, and neither does the Bookkeeper's Verifier agreeing with it. So:

    check_findings(report, findings)              no decisions: every finding "downgrade" (awaiting the owner)
    check_findings(report, findings, decisions)   accept -> "pass", dismiss -> "reject", revise -> "reject" (it goes
                                                  back to the Bookkeeper as context for a new, verified report)
                                                  a finding without a decision -> ValueError("review incomplete")

Through contracts.passed_adjustments that means:
- awaiting the owner ("downgrade"): only the cautious adjustments (money still owed, an overcharge that keeps
  costing) reach the forecast, never as recoverable, and nothing that would make it rosier (exclude_row,
  correct_row). This is today's behaviour, the same as checks_interim's;
- accepted ("pass"): all of the finding's adjustments;
- dismissed or sent back for revision ("reject"): none.
Plain code turns (report, decisions) into adjustments; the LLM never writes an adjustment or a money figure.

What the Bookkeeper's Verifier loop said (bk schema 1.2: bk_adapter.verified_status, per published finding "agreed" |
"replaced" | "verifier_only") is INFORMATION for the owner, shown next to each finding by summary(); it never passes
a finding by itself. The stage gates and the owner-review flow that collects `decisions` are built on this hook."""
import contracts as K

from . import bk_adapter as A

DECISIONS = {"accept": "pass", "dismiss": "reject", "revise": "reject"}
AWAITING = "awaiting the owner's decision (accept, dismiss or revise); until then only cautious adjustments count"
REASONS = {"accept": "accepted by the owner", "dismiss": "dismissed by the owner: no adjustment",
           "revise": "sent back to the Bookkeeper by the owner for a new report: no adjustment until it is reviewed"}
VERIFIER_INFO = {
    A.AGREED: "the Bookkeeper's Verifier agreed with it (information only; the owner decides)",
    A.REPLACED: "the Bookkeeper's Verifier disagreed and published its own version (information only)",
    A.VERIFIER_ONLY: "found only by the Bookkeeper's Verifier (information only)",
}
NOT_VERIFIED = "not verified by the Bookkeeper's Verifier (schema 1.1 or unverified)"


class ReviewIncomplete(ValueError):
    """Decisions were supplied, but not for every finding (or not valid ones)."""


def _decision_map(decisions, report_id=None) -> dict[str, tuple[str, str | None]]:
    """{finding_id: (decision, note)} from a dict {id: decision | {decision, note}} or a list of FindingDecision-like
    objects / dicts {finding_id, decision, note?, report_id?}."""
    items = decisions.items() if isinstance(decisions, dict) else None
    out = {}
    if items is not None:
        for fid, d in items:
            value, note = (d.get("decision"), d.get("note")) if isinstance(d, dict) else (d, None)
            out[str(fid)] = (value, note)
    else:
        for d in decisions:
            get = d.get if isinstance(d, dict) else (lambda k, _d=d: getattr(_d, k, None))
            rid = get("report_id")
            if report_id and rid and rid != report_id:
                raise ReviewIncomplete("review incomplete: a decision is for another report")
            out[str(get("finding_id"))] = (get("decision"), get("note"))
    bad = sorted(fid for fid, (v, _) in out.items() if v not in DECISIONS)
    if bad:
        raise ReviewIncomplete(f"review incomplete: decisions must be accept, dismiss or revise ({', '.join(bad[:10])})")
    return out


def _verifier_reason(status) -> str:
    return VERIFIER_INFO.get(status, NOT_VERIFIED)


def check_findings(report, findings, decisions=None, verified: dict | None = None) -> list:
    """-> one CheckResult(subject="bookkeeper") per finding (contracts.Finding).

    report: the raw report dict the findings came from (its report_id is checked against decisions that name one).
    decisions: the OWNER's decisions, or None while the review is open. verified: for the orchestrator's merged
    report, {merged id: {"status", ...}} from bk_state.open_verification (information in the reasons only)."""
    report = report if isinstance(report, dict) else {}
    if verified is None:
        per = A.verified_status(report)["per_finding"]
        status_of = per.get
    else:
        status_of = lambda fid: (verified.get(fid) or {}).get("status")  # noqa: E731
    if decisions is None:
        return [K.CheckResult(subject="bookkeeper", ref=f.finding_id, status="downgrade",
                              reasons=[AWAITING, _verifier_reason(status_of(f.finding_id))]) for f in findings]
    made = _decision_map(decisions, report.get("report_id"))
    ids = [f.finding_id for f in findings]
    missing = [fid for fid in ids if fid not in made]
    if missing:
        raise ReviewIncomplete(f"review incomplete: no decision for {', '.join(missing[:10])}"
                               + (" ..." if len(missing) > 10 else ""))
    unknown = sorted(set(made) - set(ids))
    if unknown:
        raise ReviewIncomplete(f"review incomplete: decisions for findings not in this report ({', '.join(unknown[:10])})")
    out = []
    for f in findings:
        decision, note = made[f.finding_id]
        reasons = [REASONS[decision]] + ([f"owner's note: {note}"] if note else [])
        out.append(K.CheckResult(subject="bookkeeper", ref=f.finding_id, status=DECISIONS[decision],
                                 reasons=reasons + [_verifier_reason(status_of(f.finding_id))]))
    return out


def passed(report, adjustments, findings, decisions=None, verified: dict | None = None) -> list:
    """The adjustments the Forecaster may use (contracts.passed_adjustments on the checks above)."""
    return K.passed_adjustments(adjustments, check_findings(report, findings, decisions, verified))


def summary(report, verified: dict | None = None) -> dict:
    """What the Verifier loop said, for the owner: {"verified", "rounds", "label", "findings_checked",
    "findings_rejected", "per_finding", "left_out"}. For the merged report (the verified map given): verified when
    every open finding went through the loop (and there is at least one)."""
    if verified is not None:
        statuses = [v.get("status") for v in verified.values()]
        rounds = [v.get("rounds") for v in verified.values() if isinstance(v.get("rounds"), int)]
        done = [s for s in statuses if s not in (None, "unverified")]
        all_done = bool(statuses) and len(done) == len(statuses)
        r = max(rounds) if rounds else None
        label = (A.verification_label(True, r) if all_done else
                 "not verified" if not done else
                 f"partly verified: {len(done)} of {len(statuses)} open findings went through the Bookkeeper's Verifier")
        return {"verified": all_done, "rounds": r, "label": label, "findings_checked": None,
                "findings_rejected": None, "per_finding": {k: v.get("status") for k, v in verified.items()},
                "left_out": []}
    vs = A.verified_status(report if isinstance(report, dict) else {})
    return {**vs, "label": A.verification_label(vs["verified"], vs["rounds"])}
