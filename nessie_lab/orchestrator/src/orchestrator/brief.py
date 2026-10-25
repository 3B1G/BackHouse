"""The owner's briefing in plain English, with no LLM at all. Built only from tool results (tools.call, so every one
is in session.log). Money is printed only from the `*_usd` display strings fmt.usd put in those results, so each
dollar figure in the text appears, character for character, in a tool result."""
from datetime import date, timedelta

from . import tools
from .controller import propose_fixes
from .session import Session

BILLS_WINDOW_DAYS = 14


def _when(d) -> str:
    return d if d else "beyond the forecast horizon"


def brief(session: Session, max_fixes: int = 3, propose: bool = True) -> dict:
    """-> {"text": str, "facts": dict, "suggestions": [paths]}. propose=False (a chat user who is not the owner):
    facts only, no suggestion is written."""
    lines, facts = [], {}
    cash = tools.call(session, "get_cash_position", {})
    facts["cash"] = cash
    if "error" in cash:
        lines.append(f"Cash: not available ({cash['error']}).")
        start = session.as_of or Session.today()
    else:
        start = date.fromisoformat(cash["as_of"])
        accounts = ", ".join(f"{a['account']} {a['balance_usd']}" for a in cash["accounts"])
        lines.append(f"Cash on {cash['as_of']}: {cash['total_usd']} ({accounts}).")

    base = tools.call(session, "forecast_base", {})
    facts["forecast"] = base
    if "error" in base:
        lines.append(f"Runway: forecast not available ({base['error']}).")
    else:
        lines.append(f"Runway (forecast {base['forecast_id']}, {base['trials']} simulated futures): cash likely reaches "
                     f"$0 on {_when(base['run_out_likely'])}; worst case {_when(base['run_out_worst'])}, best case "
                     f"{_when(base['run_out_best'])}. {base['running_out_pct_display']} of futures run out within "
                     f"{base['horizon_months']} months.")
        lines.append(f"Cash likely drops below one payroll plus rent ({base['safety_floor_usd']}) on "
                     f"{_when(base['below_floor_likely'])}.")
        lines.append("Not simulated by default: one-off shocks such as a walk-in fridge failure or a slow month. "
                     "Ask for a scenario (forecast_scenario with one_off_costs or cover_change) to test one.")
        if base.get("adjustments_note"):
            lines.append(f"Bookkeeping in the forecast: {base['adjustments_note']}.")

    before = (start + timedelta(days=BILLS_WINDOW_DAYS)).isoformat()
    bills = tools.call(session, "list_bills_due", {"before": before})
    facts["bills"] = bills
    if "error" in bills:
        lines.append(f"Bills: not available ({bills['error']}).")
    else:
        lines.append(f"Bills due by {before}: {bills['count']} pending, {bills['total_usd']} in all.")
        for b in sorted(bills["bills"], key=lambda b: -b["amount_cents"])[:3]:
            lines.append(f"  - {b['doc']} ({b['payee']}) {b['amount_usd']} due {b['due']}")

    findings = tools.call(session, "get_findings", {})
    facts["findings"] = findings
    if "error" in findings:
        lines.append(f"Bookkeeper: report not readable ({findings['error']}).")
    elif not findings.get("available"):
        lines.append("Bookkeeper: no Bookkeeper report yet, so the books are not reviewed; the forecast uses the bank "
                     "history as it stands.")
    else:
        lines.append(f"Bookkeeper ({findings['report']}): {len(findings['findings'])} finding(s), each waiting for the "
                     "owner to confirm (interim checks until the verifier exists):")
        for f in findings["findings"]:
            lines.append(f"  - {f['finding_id']}: {f['root_cause']} ({f['amount_at_issue_usd']} at issue)")
        if findings.get("rejected"):
            lines.append(f"  Not used: {', '.join(sorted(findings['rejected']))}.")

    scan = tools.call(session, "scan_market", {})
    facts["market"] = scan
    if "error" in scan:
        lines.append(f"Market: not available ({scan['error']}).")
    else:
        lines.append(f"Market ({scan['filename']}, menu of {scan['menu_as_of']}"
                     f"{', saved pages only' if scan['offline'] else ''}):")
        for kind, label in (("below_market", "below market"), ("above_market", "above market"), ("at_par", "at par")):
            for p in (p for p in scan["positions"] if p["position"] == kind):
                lines.append(f"  - {p['our_item']}: ours {p['our_price_usd']}, competitor median "
                             f"{p['competitor_median_usd']} from {p['n_prices']} restaurants ({label})")
        thin = [p["our_item"] for p in scan["positions"] if p["position"] == "insufficient_data"]
        if thin:
            lines.append(f"  - Too few competitor prices to compare: {', '.join(thin)}.")

    fixes = propose_fixes(session, max_fixes=max_fixes) if propose else []
    facts["fixes"] = fixes
    if not propose:
        lines.append("Fixes: I propose changes only for the owner; ask the owner.")
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
    lines.append("Sources: every figure above comes from a tool result in this session's log.")
    return {"text": "\n".join(lines), "facts": facts, "suggestions": [fx["path"] for fx in fixes]}

