"""Stage 2, deterministic: "what do we do about it?" No model involved; every candidate goes through the same tools an
LLM would use (tools.call), so each step is in session.log.

1. Scan the market (reuses this session's latest full scan if there is one).
2. Candidates: dishes `below_market` with no approved upcoming change.
3. Score each with a forecast scenario at the price propose_fix would use (median rounded to the dollar, ±30% cap).
4. Rank by vs_base.days_moved_average, ties by the monthly cash effect.
5. Propose the top `max_fixes` with propose_fix (CH plans and dry-runs each one before it is saved).
6. Also: a remove_item for each dish whose food cost is at least 80% of its price (and that the market either
   compared or has no twin for), scored with remove_items. Lobster Ravioli runs about 92-95%.
A candidate the forecast says doesn't help (no days gained and no monthly cash) is not proposed."""
from . import tools
from .session import Session

REMOVE_AT_FOOD_COST = 0.80


def _latest_full_scan(session: Session) -> dict:
    for entry in reversed(session.log):
        if entry["name"] == "scan_market" and not entry["args"].get("items") and "error" not in entry["result"]:
            return entry["result"]
    return tools.call(session, "scan_market", {})


def _score(fc: dict) -> tuple[float, int]:
    avg = (fc.get("vs_base") or {}).get("days_moved_average")
    return (float("-inf") if avg is None else avg, fc.get("monthly_cash_effect_cents") or 0)


def _helps(fc: dict) -> bool:
    avg, monthly = _score(fc)
    return avg > 0 or monthly > 0


def candidates(session: Session) -> dict:
    """-> {"reprice": [(dish, scenario result)], "remove": [...], "skipped": {dish: why}}, best first."""
    scan = _latest_full_scan(session)
    if "error" in scan:
        return {"reprice": [], "remove": [], "skipped": {"market": scan["error"]}}
    upcoming = tools.call(session, "list_upcoming_changes", {})
    menu = tools.call(session, "get_menu", {})
    pending = {u["item"] for u in upcoming.get("changes", [])}
    eff = session.effective_date().isoformat()
    skipped, reprice, remove = {}, [], []
    for p in scan["positions"]:
        dish = p["our_item"]
        if p["position"] != "below_market":
            continue
        if dish in pending or p.get("upcoming_price_cents") or p.get("leaving_from"):
            skipped[dish] = "already has an approved upcoming change"
            continue
        old = p["our_price_cents"]
        new, _ = tools.target_price(old, tools._round_dollar(p["competitor_median_cents"]), whole_dollars=True)
        if new == old:
            skipped[dish] = "the median rounds to our price"
            continue
        fc = tools.call(session, "forecast_scenario", {"scenario": {
            "name": f"{dish} {tools.usd(old)} to {tools.usd(new)}", "price_changes_cents": {dish: new},
            "changes_start": eff}})
        if "error" in fc:
            skipped[dish] = fc["error"]
        elif not _helps(fc):
            skipped[dish] = "the forecast shows no gain"
        else:
            reprice.append((dish, fc))
    for d in menu.get("dishes", []):
        dish, share = d["item"], d.get("food_cost_pct")
        if share is None or share < REMOVE_AT_FOOD_COST or dish in pending:
            continue
        # either the scan compared it (any position, insufficient_data included) or it has no market twin: a dish
        # that loses money per plate is a removal candidate whatever competitors charge
        fc = tools.call(session, "forecast_scenario", {"scenario": {
            "name": f"take {dish} off the menu", "remove_items": [dish], "changes_start": eff}})
        if "error" in fc:
            skipped[dish] = fc["error"]
        elif not _helps(fc):
            skipped[dish] = "removing it shows no gain in the forecast"
        else:
            remove.append((dish, fc))
    reprice.sort(key=lambda x: _score(x[1]), reverse=True)
    remove.sort(key=lambda x: _score(x[1]), reverse=True)
    return {"reprice": reprice, "remove": remove, "skipped": skipped}


def propose_fixes(session: Session, max_fixes: int = 3) -> list[dict]:
    """The top `max_fixes` reprices plus the remove_item candidates, each a propose_fix result ({path, suggestion,
    question, approve_command, ...}) that CH's plan and dry run accepted. Rejected candidates are in session.log."""
    found = candidates(session)
    out = []
    for dish, _ in found["reprice"][:max(0, max_fixes)]:
        r = tools.call(session, "propose_fix", {"dish": dish})
        if "error" not in r:
            out.append(r)
    for dish, _ in found["remove"]:
        r = tools.call(session, "propose_fix", {"dish": dish, "action": "remove_item"})
        if "error" not in r:
            out.append(r)
    return out
