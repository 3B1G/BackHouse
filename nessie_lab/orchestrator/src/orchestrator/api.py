"""The JSON API for the frontend, on the orchestrator's own server (port 8005), as uagents REST routes.

Every route is under /api and goes through GuardedServer (agent.py): registered only when the server is bound to a
loopback address, a loopback client and Host, the bearer token from data/orchestrator/rest_token on every request
(the CORS preflight aside), and an Origin header, when there is one, must be in ORCHESTRATOR_ALLOWED_ORIGINS
(the only origins that get CORS headers; never "*").

    GET  /api/health                 version, mode, offline, delegation, agents reachable, Bookkeeper coverage
    GET  /api/brief                  the deterministic brief as JSON (+ text); READ-ONLY: it never writes a suggestion
    POST /api/ask {question}         one owner turn (same guard as /ask): {text, suggestions, degraded, notes}
    GET  /api/status                 bookkeeper_status, cost_price verify() summary, pending suggestions count
    GET  /api/suggestions            pending suggestion files, each with its CH plan line and approve_command
    POST /api/suggestions/preview {id}   CH's dry-run plan and question line for one suggestion (read-only)
    GET  /api/market                 the latest saved market positions (no new scan)
    POST /api/forecast {scenario}    base and scenario results with vs_base
    GET  /api/menu                   the effective menu with food cost %
    GET  /api/bills                  pending bills, earliest first

There is NO approve or apply route. A suggestion carries `approve_command` (what the owner runs at their own
terminal; CH asks for the passphrase) and `owner_action_required: true`. Money is {cents, usd} (usd from fmt.usd,
"$1,234.56"), dates are ISO (YYYY-MM-DD). Every response has `ok` and, when ok is false, `error` (the HTTP status is
200; 400 for a body that doesn't match the request model, 401/403/413 from the guard).

`orchestrator api-schema --out docs/api_schema.json` writes the JSON Schema of every model plus this endpoint list."""
import asyncio
import json
import logging
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from uagents import Context, Model

LOG = logging.getLogger("orchestrator.api")
API_DEADLINE = 90                       # seconds for one brief / forecast request
SUGGESTION_LIMIT = 25
PROBE_TTL = 30
OWNER_NOTE = ("Nothing here approves or applies. To apply a suggestion, the owner runs its approve_command at their "
              "own terminal (CH asks for the owner passphrase).")
READ_ONLY_FIXES = ("Fixes: this read-only view writes no suggestion; see GET /api/suggestions, or POST /api/ask with "
                   "\"brief\" to have fixes proposed.")


# ---------------------------------------------------------------- models
class Money(Model):
    cents: int
    usd: str


class AgentStatus(Model):
    configured: bool
    address: str | None = None
    reachable: bool | None = None                     # None = not known (mailbox, or no local endpoint to probe)
    how: str = ""


class Coverage(Model):
    available: bool
    covered_through: str | None = None
    latest_bank_date: str | None = None
    days_behind: int | None = None
    up_to_date: bool | None = None
    open_findings: int | None = None
    verification: str | None = None                   # the last run: "verified by the Bookkeeper's Verifier (N rounds)"
    why: str | None = None


class HealthResponse(Model):
    ok: bool = True
    error: str | None = None
    version: str
    mode: str                                         # "local" | "mailbox"
    offline: bool
    delegation: bool                                  # scan_market / Bookkeeper runs go to the agents
    agents: dict[str, AgentStatus] = {}
    bookkeeper: Coverage | None = None
    time: str


class Account(Model):
    account: str
    nickname: str | None = None
    balance: Money


class Cash(Model):
    available: bool
    as_of: str | None = None
    total: Money | None = None
    accounts: list[Account] = []
    error: str | None = None


class Runway(Model):
    available: bool
    forecast_id: str | None = None
    as_of: str | None = None
    starting_cash: Money | None = None
    run_out_likely: str | None = None
    run_out_worst: str | None = None
    run_out_best: str | None = None
    below_floor_likely: str | None = None
    safety_floor: Money | None = None
    share_running_out: float | None = None
    running_out_pct: str | None = None
    trials: int | None = None
    horizon_months: int | None = None
    adjustments_note: str | None = None
    error: str | None = None


class Bill(Model):
    doc: str | None = None
    payee: str | None = None
    due: str | None = None
    account: str | None = None
    amount: Money


class Bills(Model):
    available: bool
    before: str | None = None
    count: int = 0
    total: Money | None = None
    items: list[Bill] = []
    error: str | None = None


class Verification(Model):
    verified: bool
    label: str                                        # "verified by the Bookkeeper's Verifier (2 rounds)" | "not verified"
    rounds: int | None = None
    findings_checked: int | None = None
    findings_rejected: int | None = None
    left_out: list[dict] = []


class FindingItem(Model):
    finding_id: str
    issue_type: str | None = None
    verdict: str | None = None
    root_cause: str | None = None
    counterparty: str | None = None
    first_date: str | None = None
    last_date: str | None = None
    amount_at_issue: Money | None = None
    monthly_impact: Money | None = None
    suggested_action: str | None = None
    check: str | None = None                          # "downgrade" (awaiting the owner) | "pass" | "reject"
    check_reasons: list[str] = []
    verification: str | None = None                   # the Verifier's view, information only: "agreed" |
    #                                                   "replaced" | "verifier_only" | "unverified"


class Findings(Model):
    available: bool
    source: str | None = None
    report: str | None = None
    verification: Verification | None = None
    items: list[FindingItem] = []
    forecast_uses: int = 0
    not_used: list[str] = []
    coverage_note: str | None = None
    why: str | None = None
    error: str | None = None


class Position(Model):
    our_item: str
    service: str | None = None
    position: str | None = None                       # below_market | above_market | at_par | insufficient_data
    our_price: Money | None = None
    competitor_median: Money | None = None
    competitor_low: Money | None = None
    competitor_high: Money | None = None
    n_prices: int | None = None
    gap_pct: float | None = None
    upcoming_price: Money | None = None
    upcoming_from: str | None = None
    leaving_from: str | None = None


class Market(Model):
    available: bool
    filename: str | None = None
    report_id: str | None = None
    menu_as_of: str | None = None
    offline: bool | None = None
    via: str | None = None
    positions: list[Position] = []
    limitations: list[str] = []
    degraded: bool = False
    error: str | None = None


class Effect(Model):
    monthly_cash_effect: Money | None = None
    one_time_cash_effect: Money | None = None
    run_out_likely_before: str | None = None
    run_out_likely_after: str | None = None


class SuggestionItem(Model):
    id: str
    path: str
    status: str = "pending"
    plan_line: str | None = None                      # CH's question line, from a fresh plan (None: not planned)
    plan_error: str | None = None                     # CH would refuse it now
    title: str | None = None
    action: str | None = None
    target: str | None = None
    old_price: Money | None = None                    # the price in force today (reprice_item)
    new_price: Money | None = None
    effect: Effect = Effect()
    created: str | None = None
    approve_command: str
    owner_action_required: bool = True


class BriefResponse(Model):
    ok: bool = True
    error: str | None = None
    text: str = ""
    cash: Cash | None = None
    runway: Runway | None = None
    bills: Bills | None = None
    findings: Findings | None = None
    market: Market | None = None
    fixes: list[SuggestionItem] = []
    proposes: bool = False                            # GET /api/brief never writes a suggestion
    degraded: bool = False
    notes: list[str] = []


class AskApiRequest(Model):
    question: str
    session_id: str = "api"
    reset: bool = False


class AskApiResponse(Model):
    ok: bool = True
    error: str | None = None
    status: str = "ok"                                # ok | busy | error
    text: str = ""
    suggestions: list[str] = []
    approve_commands: list[str] = []
    owner_action_required: bool = False
    degraded: bool = False
    notes: list[str] = []


class CostPriceCheck(Model):
    ok: bool
    problems: list[str] = []
    notes: list[str] = []
    fingerprint: str | None = None


class StatusResponse(Model):
    ok: bool = True
    error: str | None = None
    bookkeeper_status: dict = {}
    cost_price: CostPriceCheck | None = None
    pending_suggestions: int = 0


class SuggestionsResponse(Model):
    ok: bool = True
    error: str | None = None
    suggestions: list[SuggestionItem] = []
    skipped: int = 0
    note: str = OWNER_NOTE


class PreviewRequest(Model):
    id: str


class PreviewResponse(Model):
    ok: bool = True
    error: str | None = None
    suggestion: SuggestionItem | None = None
    question: str | None = None
    planned_change: dict | None = None
    dry_run: dict | None = None
    approve_command: str | None = None
    owner_action_required: bool = True
    note: str = OWNER_NOTE


class MarketResponse(Model):
    ok: bool = True
    error: str | None = None
    market: Market | None = None


class ForecastRequest(Model):
    scenario: dict


class ForecastView(Model):
    forecast_id: str | None = None
    run_out_likely: str | None = None
    run_out_worst: str | None = None
    run_out_best: str | None = None
    below_floor_likely: str | None = None
    share_running_out: float | None = None
    running_out_pct: str | None = None


class ForecastResponse(Model):
    ok: bool = True
    error: str | None = None
    base: ForecastView | None = None
    scenario: ForecastView | None = None
    vs_base: dict = {}
    monthly_cash_effect: Money | None = None
    effects: list[dict] = []
    assumptions: list[str] = []


class Dish(Model):
    item: str
    price: Money
    plate_cost: Money | None = None
    food_cost_pct: float | None = None
    food_cost_display: str | None = None


class MenuResponse(Model):
    ok: bool = True
    error: str | None = None
    on: str | None = None
    prices_as_of: str | None = None
    dishes: list[Dish] = []
    food_cost_pct: float | None = None                # all plates' cost / all prices
    food_cost_display: str | None = None
    upcoming: list[dict] = []


class BillsResponse(Model):
    ok: bool = True
    error: str | None = None
    bills: Bills | None = None


# ---------------------------------------------------------------- helpers
def money(cents) -> dict | None:
    from .fmt import usd
    if not isinstance(cents, int) or isinstance(cents, bool):
        return None
    return {"cents": cents, "usd": usd(cents)}


def _session(service):
    from .session import Session
    from .turn import Deadline

    s = Session(service.root, offline=service.offline, as_of=None, tag=service.tag, log=[])
    s.stop = Deadline(API_DEADLINE)
    return s


async def _thread(fn, timeout=API_DEADLINE):
    return await asyncio.wait_for(asyncio.to_thread(fn), timeout)


def _err(exc) -> str:
    return "timed out" if isinstance(exc, asyncio.TimeoutError) else f"failed ({type(exc).__name__})"


def cash_view(r: dict) -> dict:
    if not isinstance(r, dict) or "error" in r:
        return {"available": False, "error": (r or {}).get("error") if isinstance(r, dict) else "not available"}
    return {"available": True, "as_of": r.get("as_of"), "total": money(r.get("total_cents")),
            "accounts": [{"account": str(a.get("account")), "nickname": a.get("nickname"),
                          "balance": money(a.get("balance_cents"))} for a in r.get("accounts") or []]}


def runway_view(r: dict) -> dict:
    if not isinstance(r, dict) or "error" in r:
        return {"available": False, "error": (r or {}).get("error") if isinstance(r, dict) else "not available"}
    keep = ("forecast_id", "as_of", "run_out_likely", "run_out_worst", "run_out_best", "below_floor_likely",
            "share_running_out", "trials", "horizon_months", "adjustments_note")
    return {"available": True, **{k: r.get(k) for k in keep}, "starting_cash": money(r.get("starting_cash_cents")),
            "safety_floor": money(r.get("safety_floor_cents")), "running_out_pct": r.get("running_out_pct_display")}


def bills_view(r: dict) -> dict:
    if not isinstance(r, dict) or "error" in r:
        return {"available": False, "error": (r or {}).get("error") if isinstance(r, dict) else "not available"}
    return {"available": True, "before": r.get("before"), "count": int(r.get("count") or 0),
            "total": money(r.get("total_cents")),
            "items": [{"doc": b.get("doc"), "payee": b.get("payee"), "due": b.get("due"), "account": b.get("account"),
                       "amount": money(b.get("amount_cents"))} for b in r.get("bills") or []]}


def findings_view(r: dict) -> dict:
    if not isinstance(r, dict) or "error" in r:
        return {"available": False, "error": (r or {}).get("error") if isinstance(r, dict) else "not available"}
    if not r.get("available"):
        return {"available": False, "why": r.get("why")}
    checks = {c["ref"]: c for c in r.get("checks") or []}
    ver = r.get("verification") or {}
    per = ver.get("per_finding") or {}
    items = []
    for f in r.get("findings") or []:
        c = checks.get(f["finding_id"]) or {}
        items.append({"finding_id": f["finding_id"], "issue_type": f.get("issue_type"), "verdict": f.get("verdict"),
                      "root_cause": f.get("root_cause"), "counterparty": f.get("counterparty"),
                      "first_date": f.get("first_date"), "last_date": f.get("last_date"),
                      "amount_at_issue": money(f.get("amount_at_issue_cents")),
                      "monthly_impact": money(f.get("monthly_impact_cents")),
                      "suggested_action": f.get("suggested_action"), "check": c.get("status"),
                      "check_reasons": list(c.get("reasons") or []),
                      "verification": per.get(f["finding_id"]) or ("unverified" if not ver.get("verified") else None)})
    return {"available": True, "source": r.get("source"), "report": r.get("report"),
            "verification": {"verified": bool(ver.get("verified")), "label": ver.get("label") or "not verified",
                             "rounds": ver.get("rounds"), "findings_checked": ver.get("findings_checked"),
                             "findings_rejected": ver.get("findings_rejected"), "left_out": ver.get("left_out") or []},
            "items": items, "forecast_uses": len(r.get("forecast_uses") or []),
            "not_used": sorted(r.get("rejected") or {}), "coverage_note": r.get("coverage_note")}


def market_view(r: dict) -> dict:
    if not isinstance(r, dict) or "error" in r:
        return {"available": False, "error": (r or {}).get("error") if isinstance(r, dict) else "not available"}
    positions = r.get("positions")
    if not isinstance(positions, list):
        return {"available": False, "error": "no positions"}
    out = []
    for p in positions:
        if not isinstance(p, dict) or not p.get("our_item"):
            continue
        out.append({"our_item": str(p["our_item"]), "service": p.get("service"), "position": p.get("position"),
                    "our_price": money(p.get("our_price_cents")),
                    "competitor_median": money(p.get("competitor_median_cents")),
                    "competitor_low": money(p.get("competitor_low_cents")),
                    "competitor_high": money(p.get("competitor_high_cents")), "n_prices": p.get("n_prices"),
                    "gap_pct": p.get("gap_pct"), "upcoming_price": money(p.get("upcoming_price_cents")),
                    "upcoming_from": p.get("upcoming_from"), "leaving_from": p.get("leaving_from")})
    return {"available": True, "filename": r.get("filename"), "report_id": r.get("report_id"),
            "menu_as_of": r.get("menu_as_of"), "offline": r.get("offline"), "via": r.get("via"), "positions": out,
            "limitations": [str(x) for x in r.get("limitations") or []], "degraded": bool(r.get("degraded"))}


def _applied_ids(session) -> set[str]:
    """Suggestion ids CH has logged a change for (read on a quiet copy, role reader)."""
    from cost_price import api as cp

    from .tools import reader
    try:
        with reader(session) as con:
            return {str(r.get("suggestion_id")) for r in cp.change_log(con)}
    except Exception:
        return set()


def list_suggestions(session, preview: bool = True, limit: int = SUGGESTION_LIMIT) -> tuple[list[dict], int]:
    """Pending suggestion files (newest first): each with its CH plan line (preview=True: CH's plan and dry run,
    read-only) and the approve_command. A file CH has logged a change for is applied, not pending, and left out."""
    from changes.shared import Suggestion

    from . import tools
    from .session import ID_RE

    folder = session.data / "suggestions"
    if not folder.is_dir():
        return [], 0
    files, skipped = [], 0
    for p in folder.iterdir():
        try:
            if p.suffix == ".json" and p.is_file() and not p.is_symlink() and ID_RE.fullmatch(p.stem):
                files.append((p.stat().st_mtime, p))
        except OSError:
            continue
    files.sort(key=lambda t: t[0], reverse=True)
    applied = _applied_ids(session)
    today = session.today().isoformat()
    out = []
    for mtime, p in files:
        if len(out) >= limit:
            break
        try:
            s = Suggestion.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:
            skipped += 1
            continue
        if s.suggestion_id != p.stem:
            skipped += 1
            continue
        if s.suggestion_id in applied:
            continue
        item = suggestion_item(session, p, s, mtime, today)
        if preview:
            r = tools.call(session, "preview_suggestion", {"path": item["path"]})
            item["plan_line"] = r.get("question") if "error" not in r else None
            item["plan_error"] = r.get("error")
        out.append(item)
    return out, skipped


def suggestion_item(session, path: Path, s, mtime: float, today: str) -> dict:
    from . import tools

    old = None
    if s.action == "reprice_item":
        try:
            from cost_price import api as cp
            with tools.reader(session) as con:
                old = cp.effective_price(con, s.target, today)
        except Exception:
            old = None
    return {"id": s.suggestion_id, "path": f"data/suggestions/{path.name}", "status": "pending",
            "title": s.title, "action": s.action, "target": s.target, "old_price": money(old),
            "new_price": money(getattr(s, "new_price_cents", None)),
            "effect": {"monthly_cash_effect": money(getattr(s, "monthly_cash_effect_cents", None)),
                       "one_time_cash_effect": money(getattr(s, "one_time_cash_effect_cents", None)),
                       "run_out_likely_before": _iso(getattr(s, "run_out_likely_before", None)),
                       "run_out_likely_after": _iso(getattr(s, "run_out_likely_after", None))},
            "created": datetime.fromtimestamp(mtime, timezone.utc).isoformat(timespec="seconds"),
            "approve_command": tools._command(session, path), "owner_action_required": True}


def _iso(value):
    return value.isoformat() if hasattr(value, "isoformat") else value


def cost_price_check(session) -> dict:
    """cost_price.verify on a quiet copy (with its seal next to it): {ok, problems, notes, fingerprint}."""
    from changes.executor import quiet_path
    from cost_price.verify import seal_path, verify

    db = session.cost_db
    if not db.exists():
        return {"ok": False, "problems": ["data/cost_price.db not found: run `cost-price build`"], "notes": [],
                "fingerprint": None}
    with quiet_path(db) as path:
        path = Path(path)
        if path != db and seal_path(db).exists():
            shutil.copyfile(seal_path(db), seal_path(path))
        out = verify(path)

    def scrub(text):
        for form in {str(path), str(db), path.as_posix(), db.as_posix(), str(path.parent), str(db.parent)}:
            text = text.replace(form, "data/cost_price.db" if form.endswith(".db") else "data")
        return text[:300]
    problems = [scrub(str(x)) for x in out.get("problems") or []]
    return {"ok": not problems, "problems": problems[:20], "notes": [scrub(str(x)) for x in out.get("notes") or []][:20],
            "fingerprint": out.get("fingerprint")}


async def http_probe(endpoint: str) -> bool | None:
    """A HEAD /submit readiness probe of a loopback peer endpoint (uagents answers it). None for anything else."""
    import httpx

    try:
        host = (urlsplit(endpoint).hostname or "").lower()
    except ValueError:
        return None
    if host not in ("127.0.0.1", "localhost", "::1"):
        return None
    try:
        async with httpx.AsyncClient(trust_env=False, timeout=1.5) as http:
            r = await http.head(endpoint)
        return r.status_code < 500
    except Exception:
        return False


async def agent_status(service, address: str | None) -> dict:
    if not address:
        return {"configured": False, "how": "address not set"}
    endpoint = service.peer_endpoints.get(address)
    if not endpoint:
        how = ("mailbox mode: reached through Agentverse, not probed" if service.mailbox
               else "not in ORCHESTRATOR_PEERS: replies can't be routed")
        return {"configured": True, "address": address, "reachable": None, "how": how}
    cache = service.__dict__.setdefault("_probe_cache", {})
    hit = cache.get(address)
    if hit and time.monotonic() - hit[0] < PROBE_TTL:
        reachable = hit[1]
    else:
        probe = service.probe or http_probe
        try:
            reachable = await asyncio.wait_for(probe(endpoint), 3)
        except Exception:
            reachable = False
        cache[address] = (time.monotonic(), reachable)
    return {"configured": True, "address": address, "reachable": reachable, "how": "HEAD readiness probe"}


def coverage(session) -> dict:
    from . import bk_runner, bk_state

    try:
        state = bk_state.load(session.root, recover=False)
    except bk_state.StateError as exc:
        return {"available": False, "why": str(exc)}
    view = bk_state.status_view(state, bk_runner.latest_bank_date(session.root, session.tag))
    if not view.get("available"):
        return {"available": False, "latest_bank_date": view.get("latest_bank_date"), "why": view.get("why")}
    return {"available": True, "covered_through": view["covered_through"],
            "latest_bank_date": view["latest_bank_date"], "days_behind": view["days_behind"],
            "up_to_date": view["up_to_date"], "open_findings": (view.get("findings") or {}).get("open"),
            "verification": view.get("last_run_verification")}


def _busy(service):
    sem = service.__dict__.get("_api_slots")
    if sem is None:
        sem = service.__dict__["_api_slots"] = asyncio.Semaphore(2)
    return sem


# ---------------------------------------------------------------- handlers
def build_handlers(service) -> dict:
    """{(method, path): handler}. Each never raises: a problem comes back as ok=false with an error."""
    from . import __version__
    from . import tools

    async def health(ctx: Context) -> HealthResponse:
        session = _session(service)
        try:
            cov = await _thread(lambda: coverage(session), 15)
        except Exception as exc:
            cov = {"available": False, "why": _err(exc)}
        agents = {"market_scout": await agent_status(service, service.scout),
                  "bookkeeper": await agent_status(service, service.bookkeeper)}
        return HealthResponse(version=__version__, mode="mailbox" if service.mailbox else "local",
                              offline=bool(service.offline), delegation=service.delegating, agents=agents,
                              bookkeeper=cov, time=datetime.now(timezone.utc).isoformat(timespec="seconds"))

    async def brief(ctx: Context) -> BriefResponse:
        from .brief import brief as make_brief
        from .turn import prefetch_scan

        sem = _busy(service)
        if sem.locked():
            return BriefResponse(ok=False, error="busy: two API requests are already running")
        async with sem:
            session = _session(service)
            try:
                scan, notes, degraded = await prefetch_scan(session, service.dispatch(ctx, owner=False), session.stop)
                out = await _thread(lambda: make_brief(session, propose=False, scan=scan,
                                                       read_only_note=READ_ONLY_FIXES))
                fixes, _ = await _thread(lambda: list_suggestions(session, preview=False, limit=10), 30)
            except Exception as exc:
                LOG.warning("api brief failed", exc_info=True)
                return BriefResponse(ok=False, error=f"brief {_err(exc)}")
            finally:
                session.stop.cancel()
            f = out["facts"]
            return BriefResponse(text=out["text"], cash=cash_view(f.get("cash")), runway=runway_view(f.get("forecast")),
                                 bills=bills_view(f.get("bills")), findings=findings_view(f.get("findings")),
                                 market=market_view(f.get("market")), fixes=fixes, proposes=False,
                                 degraded=degraded, notes=notes + [OWNER_NOTE])

    async def ask(ctx: Context, request: AskApiRequest) -> AskApiResponse:
        from .agent import REQUEST_ID, Busy
        from .turn import MAX_INPUT

        if len(request.question) > MAX_INPUT:
            return AskApiResponse(ok=False, status="error", error=f"question over {MAX_INPUT} characters")
        key = "api:" + (REQUEST_ID.sub("-", request.session_id)[:64] or "api")
        try:
            result, new = await service.run(ctx, key, request.question, reset=request.reset, owner=True)
        except Busy:
            return AskApiResponse(ok=False, status="busy", error="a turn is already running for this session")
        except Exception as exc:
            LOG.warning("api ask failed (%s)", type(exc).__name__)
            return AskApiResponse(ok=False, status="error", error="something went wrong; nothing was changed")
        from .turn import proposals
        commands = [p["approve_command"] for p in proposals(service.session(key))
                    if p["path"] in result["suggestions"] and isinstance(p.get("approve_command"), str)]
        return AskApiResponse(text=result["text"], suggestions=list(result["suggestions"]), approve_commands=commands,
                              owner_action_required=bool(result["suggestions"]), degraded=bool(result["degraded"]),
                              notes=list(result["notes"]))

    async def status(ctx: Context) -> StatusResponse:
        session = _session(service)
        try:
            bk = await _thread(lambda: tools.call(session, "bookkeeper_status", {}), 15)
            cp = await _thread(lambda: cost_price_check(session), 30)
            pending, _ = await _thread(lambda: list_suggestions(session, preview=False, limit=1000), 30)
        except Exception as exc:
            return StatusResponse(ok=False, error=f"status {_err(exc)}")
        return StatusResponse(bookkeeper_status=bk, cost_price=cp, pending_suggestions=len(pending))

    async def suggestions(ctx: Context) -> SuggestionsResponse:
        session = _session(service)
        try:
            items, skipped = await _thread(lambda: list_suggestions(session, preview=True), 60)
        except Exception as exc:
            return SuggestionsResponse(ok=False, error=f"suggestions {_err(exc)}")
        return SuggestionsResponse(suggestions=items, skipped=skipped)

    async def preview(ctx: Context, request: PreviewRequest) -> PreviewResponse:
        from changes.shared import Suggestion

        from .session import ID_RE

        sid = request.id.strip().removeprefix("data/suggestions/").removesuffix(".json")
        if not ID_RE.fullmatch(sid):
            return PreviewResponse(ok=False, error="id must be a suggestion id like orch-20261024-213512-3f9a01bc")
        session = _session(service)
        path = session.data / "suggestions" / f"{sid}.json"
        if not path.is_file() or path.is_symlink():
            return PreviewResponse(ok=False, error="no such suggestion in data/suggestions")

        def run():
            r = tools.call(session, "preview_suggestion", {"path": f"data/suggestions/{sid}.json"})
            if "error" in r:
                return r, None
            s = Suggestion.model_validate_json(path.read_text(encoding="utf-8"))
            item = suggestion_item(session, path, s, path.stat().st_mtime, session.today().isoformat())
            item["plan_line"] = r.get("question")
            return r, item
        try:
            r, item = await _thread(run, 60)
        except Exception as exc:
            return PreviewResponse(ok=False, error=f"preview {_err(exc)}")
        if item is None:
            return PreviewResponse(ok=False, error=r["error"])
        return PreviewResponse(suggestion=item, question=r.get("question"), planned_change=r.get("planned_change"),
                               dry_run=r.get("dry_run"), approve_command=r.get("approve_command"))

    async def market(ctx: Context) -> MarketResponse:
        session = _session(service)

        def latest():
            from market_scout.contracts import MarketReport

            folder = session.data / "market_reports"
            files = sorted((p for p in folder.glob("M-*.json") if tools.MARKET_NAME.fullmatch(p.name)
                            and p.is_file() and not p.is_symlink()), key=lambda p: p.stat().st_mtime_ns) \
                if folder.is_dir() else []
            for p in reversed(files):
                try:
                    report = MarketReport.model_validate_json(p.read_text(encoding="utf-8"))
                except Exception:
                    continue
                from .fmt import displayed
                return displayed(tools._market_view(session, report, f"data/market_reports/{p.name}",
                                                    report.service))
            return None
        try:
            view = await _thread(latest, 30)
        except Exception as exc:
            return MarketResponse(ok=False, error=f"market {_err(exc)}")
        if view is None:
            return MarketResponse(market={"available": False, "error": "no market report yet: ask for a brief or "
                                                                       "a market scan first"})
        return MarketResponse(market=market_view(view))

    async def forecast(ctx: Context, request: ForecastRequest) -> ForecastResponse:
        if not isinstance(request.scenario, dict) or not request.scenario:
            return ForecastResponse(ok=False, error="scenario must be a non-empty JSON object (contracts.Scenario)")
        sem = _busy(service)
        if sem.locked():
            return ForecastResponse(ok=False, error="busy: two API requests are already running")
        async with sem:
            session = _session(service)
            try:
                r = await _thread(lambda: tools.call(session, "forecast_scenario", {"scenario": request.scenario}))
            except Exception as exc:
                return ForecastResponse(ok=False, error=f"forecast {_err(exc)}")
            finally:
                session.stop.cancel()
        if "error" in r:
            return ForecastResponse(ok=False, error=r["error"])
        before, after = r.get("before") or {}, r.get("after") or {}
        return ForecastResponse(
            base={"forecast_id": r.get("base_forecast_id"), **before,
                  "running_out_pct": _pct(before.get("share_running_out"))},
            scenario={"forecast_id": r.get("forecast_id"), **after,
                      "running_out_pct": _pct(after.get("share_running_out"))},
            vs_base=r.get("vs_base") or {}, monthly_cash_effect=money(r.get("monthly_cash_effect_cents")),
            effects=list(r.get("effects") or []), assumptions=[str(a) for a in r.get("assumptions") or []])

    async def menu(ctx: Context) -> MenuResponse:
        session = _session(service)
        try:
            m = await _thread(lambda: tools.call(session, "get_menu", {}), 30)
            up = await _thread(lambda: tools.call(session, "list_upcoming_changes", {}), 30)
        except Exception as exc:
            return MenuResponse(ok=False, error=f"menu {_err(exc)}")
        if "error" in m:
            return MenuResponse(ok=False, error=m["error"])
        dishes = [{"item": d["item"], "price": money(d["price_cents"]), "plate_cost": money(d.get("plate_cost_cents")),
                   "food_cost_pct": d.get("food_cost_pct"), "food_cost_display": d.get("food_cost_pct_display")}
                  for d in m.get("dishes") or []]
        costed = [d for d in m.get("dishes") or [] if isinstance(d.get("plate_cost_cents"), int)]
        price = sum(d["price_cents"] for d in costed)
        share = round(sum(d["plate_cost_cents"] for d in costed) / price, 4) if price else None
        return MenuResponse(on=m.get("on"), prices_as_of=m.get("prices_as_of"), dishes=dishes, food_cost_pct=share,
                            food_cost_display=_pct(share), upcoming=list((up or {}).get("changes") or []))

    async def bills(ctx: Context) -> BillsResponse:
        session = _session(service)
        try:
            r = await _thread(lambda: tools.call(session, "list_bills_due", {}), 30)
        except Exception as exc:
            return BillsResponse(ok=False, error=f"bills {_err(exc)}")
        if "error" in r:
            return BillsResponse(ok=False, error=r["error"])
        return BillsResponse(bills=bills_view(r))

    return {("GET", "/api/health"): health, ("GET", "/api/brief"): brief, ("POST", "/api/ask"): ask,
            ("GET", "/api/status"): status, ("GET", "/api/suggestions"): suggestions,
            ("POST", "/api/suggestions/preview"): preview, ("GET", "/api/market"): market,
            ("POST", "/api/forecast"): forecast, ("GET", "/api/menu"): menu, ("GET", "/api/bills"): bills}


def _pct(share):
    from .fmt import pct
    return pct(share) if isinstance(share, (int, float)) and not isinstance(share, bool) else None


# ---------------------------------------------------------------- the endpoint list
ENDPOINTS = [
    ("GET", "/api/health", None, HealthResponse, "Version, mode (local/mailbox), offline, delegation, whether the "
     "Market scout and Bookkeeper agents are reachable, and the Bookkeeper's coverage."),
    ("GET", "/api/brief", None, BriefResponse, "The deterministic brief as JSON plus its text. Read-only: it never "
     "writes a suggestion (fixes lists the pending ones)."),
    ("POST", "/api/ask", AskApiRequest, AskApiResponse, "One owner turn through the figure guard: text, suggestion "
     "paths and their approve commands, degraded, notes. May write a pending suggestion; never approves."),
    ("GET", "/api/status", None, StatusResponse, "bookkeeper_status, the cost_price verify() summary and the number "
     "of pending suggestions."),
    ("GET", "/api/suggestions", None, SuggestionsResponse, "Pending suggestion files with CH's plan line, title, "
     "action, target, old/new price, effect, created time and approve_command (owner_action_required: true)."),
    ("POST", "/api/suggestions/preview", PreviewRequest, PreviewResponse, "CH's dry-run plan and question line for "
     "one suggestion. Read-only."),
    ("GET", "/api/market", None, MarketResponse, "The latest saved market positions (no new scan)."),
    ("POST", "/api/forecast", ForecastRequest, ForecastResponse, "A what-if forecast (contracts.Scenario): base and "
     "scenario run-out dates, vs_base, the monthly cash effect."),
    ("GET", "/api/menu", None, MenuResponse, "The dine-in dinner menu in force, each dish's food cost, and the "
     "menu's overall food cost %."),
    ("GET", "/api/bills", None, BillsResponse, "Pending bills from the Nessie sync, earliest first, with the total."),
]
FORBIDDEN_WORDS = ("approve", "apply", "revert", "unlock")        # no route path may name one


def register(agent, service):
    """Add every /api route to the agent (make_agent calls this only on a loopback bind)."""
    handlers = build_handlers(service)
    service.probe = service.probe or http_probe
    for method, path, request, response, _ in ENDPOINTS:
        assert not any(w in path for w in FORBIDDEN_WORDS), path
        if method == "GET":
            agent.on_rest_get(path, response)(handlers[(method, path)])
        else:
            agent.on_rest_post(path, request, response)(handlers[(method, path)])


def schema() -> dict:
    """The JSON Schema of every request and response model, and the endpoint list."""
    from . import __version__

    models = {}
    for _, _, request, response, _ in ENDPOINTS:
        for m in (request, response):
            if m is not None:
                models[m.__name__] = json.loads(m.schema_json())
    return {"title": "BackHouse orchestrator HTTP API", "version": __version__,
            "base_url": "http://127.0.0.1:8005",
            "auth": "Authorization: Bearer <contents of nessie_lab/data/orchestrator/rest_token> on every request "
                    "(a fresh token each time `orchestrator serve` starts)",
            "cors": "only for the origins in ORCHESTRATOR_ALLOWED_ORIGINS (comma-separated, loopback only); never *",
            "money": "{cents: int, usd: '$1,234.56'}", "dates": "ISO YYYY-MM-DD",
            "errors": "ok=false with error (HTTP 200); 400 body does not match the request model; 401 no/wrong "
                      "token; 403 origin, client or host refused; 413 body over 64 KiB",
            "no_approve_or_apply": OWNER_NOTE,
            "endpoints": [{"method": m, "path": p, "request": req.__name__ if req else None,
                           "response": resp.__name__, "description": d} for m, p, req, resp, d in ENDPOINTS],
            "models": models}
