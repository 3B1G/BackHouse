"""The owner's briefing in plain English, with no LLM at all. Built only from tool results (tools.call, so every one
is in session.log). Money is printed only from the `*_usd` display strings fmt.usd put in those results, so each
dollar figure in the text appears, character for character, in a tool result.

What it covers follows the stage (stages.py), which it reads first through review_status:
    Audit     cash, bills, the Bookkeeper's coverage, and what the owner runs next
    Review    + the open findings with their proposed decisions and the batch line; no forecast, no market
    Commit    + the `changes approve` command that commits the review
    Forecast  + the runway, with the review it is based on (running it opens Suggest)
    Suggest   everything: runway, market, and the fixes (propose_fixes runs only here)"""
from datetime import date, timedelta

from . import tools
from .controller import propose_fixes
from .session import Session
from .stages import AUDIT, COMMIT, FORECAST, ORDER, REVIEW, SUGGEST

BILLS_WINDOW_DAYS = 14


def _when(d) -> str:
    return d if d else "beyond the forecast horizon"


def _locked(stage: str, opens: str) -> dict:
    return {"error": f"opens in the {opens} stage (this is the {stage} stage)"}


def brief(session: Session, max_fixes: int = 3, propose: bool = True, scan: dict | None = None,
          read_only_note: str | None = None) -> dict:
    """-> {"text": str, "facts": dict, "suggestions": [paths]}. propose=False (a chat user who is not the owner, or a
    read-only GET /api/brief): facts only, no suggestion is written. scan: a scan_market result this turn already
    logged (the served orchestrator asks the Market scout agent first); else the in-process scan runs, in the
    Suggest stage only."""
    lines, facts = [], {}
    rs = tools.call(session, "review_status", {})
    view = (rs.get("stage") or {}) if "error" not in rs else {"stage": AUDIT, "why": rs["error"]}
    stage = view.get("stage") or AUDIT
    facts["stage"], facts["review"] = view, rs
    lines.append(f"Stage: {stage} ({' -> '.join(ORDER)}). {view.get('why')}.")

    cash = tools.call(session, "get_cash_position", {})
    facts["cash"] = cash
    if "error" in cash:
        lines.append(f"Cash: not available ({cash['error']}).")
        start = session.as_of or Session.today()
    else:
        start = date.fromisoformat(cash["as_of"])
        accounts = ", ".join(f"{a['account']} {a['balance_usd']}" for a in cash["accounts"])
        lines.append(f"Cash on {cash['as_of']}: {cash['total_usd']} ({accounts}).")

    before = (start + timedelta(days=BILLS_WINDOW_DAYS)).isoformat()
    bills = tools.call(session, "list_bills_due", {"before": before})
    facts["bills"] = bills
    if "error" in bills:
        lines.append(f"Bills: not available ({bills['error']}).")
    else:
        lines.append(f"Bills due by {before}: {bills['count']} pending, {bills['total_usd']} in all.")
        for b in sorted(bills["bills"], key=lambda b: -b["amount_cents"])[:3]:
            lines.append(f"  - {b['doc']} ({b['payee']}) {b['amount_usd']} due {b['due']}")

    if stage == AUDIT:
        bk = tools.call(session, "bookkeeper_status", {})
        facts["bookkeeper"] = bk
        if bk.get("available"):
            lines.append(f"Bookkeeper: covers through {bk['covered_through']} (latest bank date "
                         f"{bk['latest_bank_date']}, {bk['days_behind']} day(s) behind); "
                         f"{(bk.get('findings') or {}).get('open', 0)} open finding(s), last run "
                         f"{bk.get('last_run_verification')}.")
        else:
            lines.append(f"Bookkeeper: {bk.get('why') or 'no baseline yet'}.")
        lines.append("Findings, the forecast and suggestions open once a verified Bookkeeper report is in and "
                     "recorded: the owner runs `orchestrator bookkeeper-update` (or `bookkeeper-setup` the first time).")
        facts["findings"] = _locked(stage, REVIEW)
    else:
        _findings(session, rs, facts, lines)
        if stage == COMMIT and view.get("approve_command"):
            lines.append("The decisions are prepared. The owner commits the review at the terminal (it asks for the "
                         f"owner passphrase; WITHOUT --apply): {view['approve_command']}")
        elif stage == REVIEW:
            lines.append("Next: decide every open finding (`orchestrator review decide <id> accept|dismiss|revise`, "
                         "or ask me), then `orchestrator review prepare`.")

    fixes = []
    if stage in (FORECAST, SUGGEST):
        base = tools.call(session, "forecast_base", {})
        facts["forecast"] = base
        if "error" in base:
            lines.append(f"Runway: forecast not available ({base['error']}).")
        else:
            prov = base.get("provenance") or {}
            lines.append(f"Runway (forecast {base['forecast_id']}, {base['trials']} simulated futures, as of "
                         f"{base['as_of']}; based on the owner-reviewed Bookkeeper findings {prov.get('report')}, "
                         f"decisions {prov.get('decisions')}): cash likely reaches $0 on "
                         f"{_when(base['run_out_likely'])}; worst case {_when(base['run_out_worst'])}, best case "
                         f"{_when(base['run_out_best'])}. {base['running_out_pct_display']} of futures run out within "
                         f"{base['horizon_months']} months.")
            lines.append(f"Cash likely drops below one payroll plus rent ({base['safety_floor_usd']}) on "
                         f"{_when(base['below_floor_likely'])}.")
            lines.append("Not simulated by default: one-off shocks such as a walk-in fridge failure or a slow month. "
                         "Ask for a scenario (forecast_scenario with one_off_costs or cover_change) to test one.")
            if base.get("adjustments_note"):
                lines.append(f"Bookkeeping in the forecast: {base['adjustments_note']}.")
    else:
        facts["forecast"] = _locked(stage, FORECAST)

    if stage == SUGGEST:
        if not (isinstance(scan, dict) and "error" not in scan and "positions" in scan):
            scan = tools.call(session, "scan_market", {})
        facts["market"] = scan
        _market(scan, lines)
        fixes = propose_fixes(session, max_fixes=max_fixes) if propose else []
        _fixes(fixes, propose, read_only_note, lines)
    else:
        facts["market"] = _locked(stage, SUGGEST)
        if stage == FORECAST and "error" not in facts["forecast"]:
            lines.append("The base forecast is recorded, so suggestions (market scan, fixes) are open now: ask for "
                         "them.")
        else:
            lines.append("Market comparison and fixes: they open in the Suggest stage, after the forecast.")
    facts["fixes"] = fixes
    lines.append("Sources: every figure above comes from a tool result in this session's log.")
    return {"text": "\n".join(lines), "facts": facts, "suggestions": [fx["path"] for fx in fixes]}


def _findings(session, rs, facts, lines):
    findings = tools.call(session, "get_findings", {})
    facts["findings"] = findings
    if "error" in findings:
        lines.append(f"Bookkeeper: report not readable ({findings['error']}).")
        return
    if not findings.get("available"):
        lines.append("Bookkeeper: no Bookkeeper report yet.")
        return
    per = (findings.get("verification") or {}).get("per_finding") or {}
    props = {f["finding_id"]: f.get("proposal") for f in rs.get("findings") or []}
    label = findings.get("verification_label") or "not verified"
    decided = sum(1 for p in props.values() if p)
    lines.append(f"Bookkeeper ({findings['report']}, {label}): {len(findings['findings'])} open finding(s), "
                 f"{decided} with a proposed decision. Proposals are not commitments: the owner commits the review "
                 "in CH.")
    info = {"agreed": "Verifier agreed", "replaced": "Verifier's version", "verifier_only": "found by the Verifier"}
    for f in findings["findings"]:
        p = props.get(f["finding_id"])
        state = "no decision yet" if not p else (f"proposed: {p['decision']}" + (" (forced)" if p.get("forced") else "")
                                                 + (f", note: {p['note']}" if p.get("note") else ""))
        tag = info.get(per.get(f["finding_id"]), "not verified")
        lines.append(f"  - {f['finding_id']}: {f['root_cause']} ({f['amount_at_issue_usd']} at issue; {tag}): "
                     f"{state}")
    batch = (rs.get("batch") or {}).get("line")
    if batch:
        lines.append(f"  {batch}")
    if findings.get("coverage_note"):
        lines.append(f"  Coverage: {findings['coverage_note']}.")
    if findings.get("rejected"):
        lines.append(f"  Not used: {', '.join(sorted(findings['rejected']))}.")


def _market(scan, lines):
    if "error" in scan:
        lines.append(f"Market: not available ({scan['error']}).")
        return
    lines.append(f"Market ({scan['filename']}, menu of {scan['menu_as_of']}"
                 f"{', saved pages only' if scan['offline'] else ''}):")
    for kind, label in (("below_market", "below market"), ("above_market", "above market"), ("at_par", "at par")):
        for p in (p for p in scan["positions"] if p["position"] == kind):
            lines.append(f"  - {p['our_item']}: ours {p['our_price_usd']}, competitor median "
                         f"{p['competitor_median_usd']} from {p['n_prices']} restaurants ({label})")
    thin = [p["our_item"] for p in scan["positions"] if p["position"] == "insufficient_data"]
    if thin:
        lines.append(f"  - Too few competitor prices to compare: {', '.join(thin)}.")


def _fixes(fixes, propose, read_only_note, lines):
    if not propose:
        lines.append(read_only_note or "Fixes: I propose changes only for the owner; ask the owner.")
    elif fixes:
        lines.append("Fixes for the owner (each checked with CH's dry run; nothing changes until you approve):")
        for i, fx in enumerate(fixes, 1):
            s, fc = fx["suggestion"], fx["forecast"]
            vs = fc.get("vs_base") or {}
            moved = (f", {vs['days_moved_average']} days later on average" if vs.get("days_moved_average") is not None
                     else "")
            money = (f"; about {s['monthly_cash_effect_usd']} a month" if s.get("monthly_cash_effect_cents") is not None
                     else "")
            lines.append(f"  {i}. {fx['question']}. Likely $0 date {_when(fc['before']['run_out_likely'])} -> "
                         f"{_when(fc['after']['run_out_likely'])}{moved}{money}.")
            lines.append(f"     Approve: {fx['approve_command']}")
    else:
        lines.append("Fixes: none the forecast supports right now.")
