"""BackHouse's orchestrator (nessie_lab/orchestrator) for the dashboard: its brief closes every check.

The orchestrator has its own environment (uagents, the cost/price DB, Market scout and Changes), so it runs as a
subprocess through uv with orchestrator_driver.py, and every call it makes streams to the developer page as it happens.
Its brief needs no AI: cash, runway, bills, the Bookkeeper's findings, Market scout's price comparison, and the fixes
the forecast supports, each dry-run by Changes and saved as a pending suggestion. owner() runs the dashboard's owner
buttons (orchestrator_driver.py says what each one does): committing a review or approving a suggestion needs the owner
passphrase, which goes to the driver on stdin and never into the trace. A decline writes nothing to cost_price; it is
kept in state/declined.json so the same change isn't suggested again.
"""
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import tracelog
from core import LAB

DRIVER = Path(__file__).with_name("orchestrator_driver.py")
# The orchestrator's own packages, named directly: Python 3.13 skips .pth files that macOS has flagged hidden, which can
# quietly break the environment's editable installs.
SOURCES = [LAB / name / "src" for name in ("orchestrator", "cost_price", "market_scout", "changes")]
# Which agent answers each orchestrator tool, by the names the developer page uses.
AGENT = {"get_cash_position": "Nessie", "list_bills_due": "Nessie", "lookup": "Bookkeeper", "get_findings": "Bookkeeper",
         "get_menu": "Records", "scan_market": "Market scout", "forecast_base": "Forecaster", "forecast_scenario": "Forecaster",
         "list_upcoming_changes": "Changes", "propose_fix": "Changes", "preview_suggestion": "Changes"}
MAX_LOG_LINES = 20
DECLINED = Path(__file__).with_name("state") / "declined.json"
OWNER_SAYS = {"decide": "Owner decided a finding", "accept-verified": "Owner accepted the verified findings",
              "setup": "Owner set up the owner passphrase", "commit": "Owner signed the review of the findings",
              "approve": "Owner approved a change", "propose": "Owner tried their own price"}


class OrchestratorError(Exception):
    pass


def run(mode, group, question=None, on_event=None, timeout=120):
    """Runs the driver. Each tool call and model request becomes a trace event in `group`, and goes to on_event as it
    arrives. Returns the driver's final line: the brief, or the answer to `question`."""
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}       # the API's own environment isn't this one
    env.update(PYTHONUNBUFFERED="1", PYTHONPATH=os.pathsep.join(map(str, SOURCES)))
    cmd = [shutil.which("uv") or "uv", "run", "--quiet", "--project", str(LAB / "orchestrator"), "python", str(DRIVER), mode]
    p = subprocess.Popen(cmd, cwd=LAB, env=env, text=True, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    p.stdin.write(question or "")
    p.stdin.close()
    timer = threading.Timer(timeout, p.kill)
    timer.start()
    final, failure, logged = None, None, 0
    try:
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                e = json.loads(line)
            except ValueError:                   # a warning from uv or a library, not one of ours
                logged += 1
                if logged <= MAX_LOG_LINES:
                    tracelog.emit("Orchestrator", "log", line, group)
                continue
            kind, tool = e.get("kind"), e.get("tool")
            if kind == "call":
                tracelog.emit(AGENT.get(tool, "Orchestrator"), "call", f"{tool}" + (f": {e['what']}" if e.get("what") else ""),
                              group, tool=tool, caller="Orchestrator")
            elif kind == "result":
                tracelog.emit(AGENT.get(tool, "Orchestrator"), "error" if e.get("error") else "result",
                              f"{tool}: {e.get('error') or e.get('note') or 'done'}", group, e.get("ms"), tool=tool)
            elif kind == "model":
                tracelog.emit("ASI:One", "error" if e.get("error") else "result",
                              e.get("error") or f"{e.get('model')} answered" + (f", asked for {e['tool_calls']} tools" if e.get("tool_calls") else ""),
                              group, e.get("ms"), tokens=e.get("tokens"), caller="Orchestrator")
            elif kind == "done":
                final = e
            elif kind == "error":
                failure = e.get("error")
            if on_event:
                on_event(e)
        code = p.wait(timeout=10)
    finally:
        timer.cancel()
    if final is None:
        raise OrchestratorError(failure or ("it took too long, so it was stopped" if code < 0 else f"it stopped with exit code {code}"))
    return final


def owner(mode, payload=None):
    """One owner button, in the orchestrator's environment. Returns the owner's review (or what an approval did)."""
    if mode not in OWNER_SAYS and mode != "owner":
        raise OrchestratorError(f"unknown owner action {mode!r}")
    span = tracelog.Span("Changes" if mode in ("setup", "commit", "approve") else "Orchestrator",
                         OWNER_SAYS.get(mode, "Owner opened the review"), "owner")
    try:
        out = run(mode, "owner", question=json.dumps(payload or {}))
    except OrchestratorError as e:
        span.fail(str(e)[:200])
        raise
    span.done((out.get("line") or out.get("signed") or "done")[:200])
    if mode == "propose":                        # the dashboard shows it like any other suggestion
        dishes = {d["item"]: d for d in (out.get("menu") or {}).get("dishes", [])}
        return {"fix": fix_view(out["fix"], dishes)}
    return out


def fix_view(fx, dishes):
    """One propose_fix result as the dashboard's suggestion card shows it."""
    s, vs = fx["suggestion"], (fx["forecast"].get("vs_base") or {})
    dish = dishes.get(s.get("target")) or {}
    return {
        "id": s["suggestion_id"], "action": s["action"], "title": s["title"], "dish": s.get("target"),
        "old_price_cents": fx.get("old_price_cents"), "new_price_cents": s.get("new_price_cents"),
        "market_median_cents": fx.get("competitor_median_cents"), "price_cents": dish.get("price_cents"),
        "food_cost_pct": dish.get("food_cost_pct"), "starts": (fx.get("planned_change") or {}).get("effective_from"),
        "runout_before": s.get("run_out_likely_before"), "runout_after": s.get("run_out_likely_after"),
        "days_later": vs.get("days_moved_average"), "monthly_cents": s.get("monthly_cash_effect_cents"),
        "approve": fx["approve_command"], "file": fx["path"],
    }


def _key(fx):
    return f"{fx.get('action')}|{fx.get('dish')}|{fx.get('new_price_cents')}"


def declined():
    try:
        return {_key(d) for d in json.loads(DECLINED.read_text())}
    except (OSError, ValueError):
        return set()


def decline(fx):
    """The owner said no: nothing is written to cost_price, and the same change (dish, action, price) stays hidden."""
    try:
        rows = json.loads(DECLINED.read_text())
    except (OSError, ValueError):
        rows = []
    rows.append({k: fx.get(k) for k in ("id", "action", "dish", "new_price_cents")})
    DECLINED.parent.mkdir(exist_ok=True)
    DECLINED.write_text(json.dumps(rows, indent=2))
    tracelog.emit("Orchestrator", "result", f"Owner declined: {fx.get('title') or fx.get('id')}; nothing was changed", "owner")


def plan(brief):
    """The brief, trimmed to what the dashboard shows: cash, bills due soon, the price comparison and each fix the owner
    hasn't declined."""
    facts = brief["facts"]
    cash, bills, market = facts.get("cash") or {}, facts.get("bills") or {}, facts.get("market") or {}
    dishes = {d["item"]: d for d in (brief.get("menu") or {}).get("dishes", [])}
    no = declined()
    fixes = [fix_view(fx, dishes) for fx in facts.get("fixes") or []
             if _key({"action": fx["suggestion"]["action"], "dish": fx["suggestion"].get("target"),
                      "new_price_cents": fx["suggestion"].get("new_price_cents")}) not in no]
    fields = ("our_item", "our_price_cents", "competitor_median_cents", "competitor_low_cents", "competitor_high_cents",
              "n_prices", "position", "gap_pct")
    stage = facts.get("stage") or {}              # Audit > Review > Commit > Forecast > Suggest: fixes only in Suggest
    return {
        "stage": {"stage": stage.get("stage"), "why": stage.get("why")} if stage else None,
        "as_of": cash.get("as_of"), "cash_cents": cash.get("total_cents"),
        "bills": {k: bills.get(k) for k in ("before", "count", "total_cents")} if "error" not in bills else None,
        "market": None if "error" in market else {
            "offline": market.get("offline"), "menu_as_of": market.get("menu_as_of"),
            "positions": [{k: p.get(k) for k in fields} for p in market.get("positions", [])],
        },
        "fixes": fixes,
        "brief": brief.get("text"),
    }
