"""The orchestrator's tools, in OpenAI function-calling format (`TOOLS`), and `call(session, name, args)`.

Stage 1, "what is going on?": get_cash_position, list_bills_due, lookup (the Bookkeeper's 11 read-only tools),
get_findings, scan_market, get_menu, list_upcoming_changes, forecast_base, forecast_scenario, bookkeeper_status.
(Running the Bookkeeper, `orchestrator bookkeeper-setup` / `bookkeeper-update`, is the owner's; no tool does it.)
Stage 2, "what do we do about it?": propose_fix (the Controller for one dish) and preview_suggestion.
Owner review (review.py): review_status, propose_decision, propose_accept_verified_errors (proposals only).

Stage gates (stages.py, STAGE_TOOLS below): Audit -> Review -> Commit -> Forecast -> Suggest, derived from disk on
every call. `tools_for(session, owner)` is the list an LLM is offered right now, and `call` refuses any tool outside
the current stage with a fixed message (nothing the caller sent is echoed). The forecast re-checks the stage itself
(_inputs), always gets an explicit list of adjustments built from the owner's committed decisions and the review's
as_of, and its result is dropped if the data moved while it ran. No tool records a commit: only the owner's
passphrase-signed `changes approve` of the review suggestion does.

Rules every tool keeps:
- `call` never raises. A problem comes back as {"error": "..."}; arguments are validated against the tool's schema.
- Every call, good or bad, is appended to session.log as {"name", "args", "result"}.
- Results are JSON-serialisable. Money is integer cents under `*_cents`, each with a `*_usd` display string from the
  one formatter (fmt.usd).
- cost_price.db is opened only with role "reader", and only on a copy (changes.executor.quiet_path), so reading
  leaves the data directory as it was. The Market scout runs in-process with `MarketScout.scan`, which only reads;
  its results go to a report file, never to cost_price.db (storing them needs the scout's write role, which is the
  scout agent's job, not ours).
- CH is used only to plan and to dry-run (`Executor(root).plan`, `.apply(s, live=False)`). Nothing here approves,
  applies live, unlocks the owner key, reads a passphrase or calls Nessie. The owner approves at a terminal with the
  `approve_command` a suggestion carries.
- `call` is plain synchronous code (no event loop of its own), so it is safe from sync code and from inside a running
  loop; `acall` runs it in a worker thread so an async caller (the uAgent) isn't blocked.
- Error messages never carry what the caller sent (a dish name, a field value, a path) and never an absolute path:
  the model could otherwise launder a figure through an error, and chat users must not see this machine's layout.
  Every path in a result is root-relative (data/suggestions/<id>.json); only the approve command for a scratch
  root keeps `--root <absolute path>`, because the owner runs it at their own terminal.
- A turn's deadline (session.stop) is checked when a call starts and again right before anything is written
  (suggestion files, forecast and market report files), so a turn that has timed out writes nothing more.
- propose_fix never writes a twin: a pending suggestion file for the same (action, dish, new price) less than 24
  hours old is returned instead of a new one."""
import asyncio
import contextvars
import json
import logging
import math
import re
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from . import env
from .fmt import displayed, usd
from .session import ID_RE, Session

LOG = logging.getLogger("orchestrator.tools")
BK_TOOL_NAMES = ["get_bank_transaction", "search_bank_transactions", "get_payee_history", "get_bank_bills",
                 "get_account_balances", "search_costs", "get_cost", "get_agreements", "get_card_batches",
                 "get_cash_deposit_slips", "get_delivery_payouts"]
MAX_PRICE_MOVE = 0.30                    # CH refuses a reprice beyond ±30% (changes.executor.MAX_PRICE_MOVE)
DATE = {"type": "string", "format": "date", "description": "YYYY-MM-DD"}
REPORT_NAME = re.compile(r"^R-[A-Za-z0-9._-]+\.json$")       # Bookkeeper reports in data/reports
MARKET_NAME = re.compile(r"^M-[A-Za-z0-9._-]+\.json$")       # Market scout reports in data/market_reports
TWIN_SECONDS = 24 * 3600                                     # propose_fix reuses a pending twin younger than this


def _fn(name, description, properties=None, required=()):
    return {"type": "function", "function": {"name": name, "description": description, "parameters": {
        "type": "object", "properties": properties or {}, "required": list(required), "additionalProperties": False}}}


ONE_OFF = {"type": "object", "properties": {"cents": {"type": "integer", "minimum": 1}, "day": DATE,
                                            "label": {"type": "string"}},
           "required": ["cents", "label"], "additionalProperties": False}
SCENARIO = {
    "type": "object", "additionalProperties": False,
    "description": "contracts.Scenario. Scenario() alone is the base case; give at least one change.",
    "properties": {
        "name": {"type": "string", "description": "a short label, e.g. 'fridge + slow January'"},
        "one_off_costs": {"type": "array", "items": ONE_OFF, "description": "e.g. a $4,800 fridge repair"},
        "one_off_income": {"type": "array", "items": ONE_OFF},
        "cover_change": {"type": "object", "description": "{'2027-01': -0.15} = 15% fewer covers that month"},
        "price_changes_cents": {"type": "object", "description": "{'Margherita Pizza': 2000}: new dine-in dinner price"},
        "remove_items": {"type": "array", "items": {"type": "string"}},
        "changes_start": DATE,
        "price_response": {"description": "share of orders lost on a price rise: 0.05, or {dish: share}"},
        "substitution": {"type": "number", "minimum": 0, "maximum": 1},
        "fix_findings": {"type": "array", "items": {"type": "string"}},
        "supplier_stretch_days": {"type": "object", "description": "{'Mission Dry Goods': 15}"},
    }}

TOOLS: list[dict] = [
    _fn("get_cash_position", "Cash in the bank now: the total and each account, from the Nessie sync."),
    _fn("list_bills_due", "Pending bills from the Nessie sync, earliest first, with their total. `before`: only bills "
        "due on or before that date.", {"before": DATE}),
    _fn("lookup", "Run one of the Bookkeeper's read-only tools (bank rows, payee history, costs, agreements, card "
        "batches, deposit slips, delivery payouts). `args` are that tool's own arguments.",
        {"tool": {"type": "string", "enum": BK_TOOL_NAMES}, "args": {"type": "object"}}, ["tool"]),
    _fn("get_findings", "The Bookkeeper's open findings (merged from every report ingested since the one-time "
        "backfill, else the latest data/reports/R-*.json): findings, the adjustments they imply, what was rejected, "
        "whether each was verified by the Bookkeeper's own Verifier (verification_label), which adjustments the "
        "forecast uses, and how far the Bookkeeper's coverage runs. {'available': false} when there is none."),
    _fn("scan_market", "Market scout: our dish prices against relevant nearby competitors (offline: saved menu pages). "
        "Positions, competitor medians, approved upcoming changes, and the report file.",
        {"items": {"type": "array", "items": {"type": "string", "minLength": 1, "maxLength": 80}, "maxItems": 50,
                   "description": "dishes; empty = the whole menu"},
         "service": {"type": "string", "enum": ["dinner", "lunch"]}}),
    _fn("get_menu", "Our dine-in dinner menu in force on a date, with each dish's food cost per plate and food cost "
        "as a share of the price.", {"on": DATE}),
    _fn("list_upcoming_changes", "Owner-approved menu changes (prices, removals) that take effect after a date.",
        {"on": DATE}),
    _fn("forecast_base", "Monte Carlo cash runway with no changes: when cash likely, at worst and at best reaches $0, "
        "the share of futures that run out, and when cash likely drops below one payroll plus rent."),
    _fn("forecast_scenario", "A what-if forecast against the base (same random draws): run-out dates before and after, "
        "days moved, and the monthly cash effect of menu changes.", {"scenario": SCENARIO}, ["scenario"]),
    _fn("propose_fix", "The Controller for one dish: build a Suggestion (reprice to the competitor median or the given "
        "price, capped at ±30%; or take the dish off the menu), score it with the Forecaster, check it with CH's dry "
        "run, and save it for the owner to approve. Returns the file, the question CH will ask, and the approve command.",
        {"dish": {"type": "string", "minLength": 1, "maxLength": 80},
         "new_price_cents": {"type": "integer", "minimum": 1},
         "action": {"type": "string", "enum": ["reprice_item", "remove_item"], "description": "default reprice_item"}},
        ["dish"]),
    _fn("preview_suggestion", "Re-run CH's dry-run plan for a saved suggestion and show the question CH will ask.",
        {"path": {"type": "string", "minLength": 1}}, ["path"]),
    _fn("bookkeeper_status", "Read-only: how far the Bookkeeper's review runs (baseline, covered through, days behind "
        "the bank, open / not re-reported / resolved finding counts, last run). Running the Bookkeeper is the "
        "owner's job at the terminal (orchestrator bookkeeper-update); no tool starts it."),
    _fn("review_status", "Read-only: the current stage (Audit, Review, Commit, Forecast, Suggest) and why, every open "
        "finding with its proposed decision, the batch of verified errors, and the owner's next command."),
    _fn("propose_decision", "Review stage: PROPOSE the owner's decision on ONE open finding (accept: its adjustment "
        "counts; dismiss: none; revise: back to the Bookkeeper). Only a proposal: the owner commits the review at the "
        "terminal. A note is kept only if the owner wrote it in this chat; a new figure in it forces revise. At most "
        "one per turn.", {"finding": {"type": "string", "minLength": 1, "maxLength": 80, "description": "finding_id"},
                          "decision": {"type": "string", "enum": ["accept", "dismiss", "revise"]},
                          "note": {"type": "string", "maxLength": 500}}, ["finding", "decision"]),
    _fn("propose_accept_verified_errors", "Review stage: PROPOSE accept for every open finding the Bookkeeper's "
        "Verifier agreed is an error (code picks them; no arguments). needs_owner findings are left for the owner."),
]
SPECS = {t["function"]["name"]: t["function"] for t in TOOLS}
ALWAYS = ("bookkeeper_status", "get_cash_position", "list_bills_due", "review_status")
READING = ("get_findings", "lookup")
FORECASTS = ("forecast_base", "forecast_scenario", "get_menu", "list_upcoming_changes")
STAGE_TOOLS = {"Audit": frozenset(ALWAYS),
               "Review": frozenset(ALWAYS + READING + ("propose_decision", "propose_accept_verified_errors")),
               "Commit": frozenset(ALWAYS + READING),
               "Forecast": frozenset(ALWAYS + READING + FORECASTS),
               "Suggest": frozenset(ALWAYS + READING + FORECASTS + ("scan_market", "propose_fix", "preview_suggestion"))}
OWNER_ONLY = frozenset(("propose_fix", "preview_suggestion", "propose_decision", "propose_accept_verified_errors"))


class Refusal(ValueError):
    """The tool can't do this as asked; the message says why."""


# ---------------------------------------------------------------- argument validation (the schemas above)
def _check(schema: dict, value, where: str):
    kind = schema.get("type")
    if kind is None:
        return value
    if kind == "object":
        if not isinstance(value, dict):
            raise Refusal(f"{where} must be a JSON object")
        props = schema.get("properties")
        if props is not None:
            for k in schema.get("required", []):
                if value.get(k) is None:
                    raise Refusal(f"missing required field '{k}' in {where}")
            if schema.get("additionalProperties") is False:
                if set(value) - set(props):                   # the unknown names are caller input: not echoed
                    raise Refusal(f"unknown field(s) in {where}; allowed: {sorted(props)}")
            return {k: (v if v is None or k not in props else _check(props[k], v, f"{where}.{k}"))
                    for k, v in value.items()}
        return value
    if kind == "array":
        if not isinstance(value, list):
            raise Refusal(f"{where} must be a list")
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            raise Refusal(f"{where} has more than {schema['maxItems']} entries")
        return [_check(schema.get("items", {}), v, f"{where}[{i}]") for i, v in enumerate(value)]
    if kind == "integer":
        if isinstance(value, float) and value.is_integer():
            value = int(value)
        if not isinstance(value, int) or isinstance(value, bool):
            raise Refusal(f"{where} must be an integer")
    elif kind == "number":
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(value):
            raise Refusal(f"{where} must be a number")
    elif kind == "string":
        if not isinstance(value, str):
            raise Refusal(f"{where} must be a string")
        if len(value) < schema.get("minLength", 0) or len(value) > schema.get("maxLength", 10_000):
            raise Refusal(f"{where} must be {schema.get('minLength', 0)}-{schema.get('maxLength', 10_000)} characters")
        if "enum" in schema and value not in schema["enum"]:
            raise Refusal(f"{where} must be one of: {', '.join(schema['enum'])}")
        if schema.get("format") == "date":
            try:
                date.fromisoformat(value)
            except ValueError:
                raise Refusal(f"{where} must be a date like 2026-10-24") from None
    if "minimum" in schema and value < schema["minimum"]:
        raise Refusal(f"{where} must be at least {schema['minimum']}")
    if "maximum" in schema and value > schema["maximum"]:
        raise Refusal(f"{where} must be at most {schema['maximum']}")
    return value


# ---------------------------------------------------------------- stages
def stage(session):
    from . import stages
    return stages.current(session)


def allowed(session, name: str) -> bool:
    """May `name` run in the current stage?"""
    return name in STAGE_TOOLS.get(stage(session).stage, ())


def tools_for(session, owner: bool = True) -> list[dict]:
    """The tools an LLM is offered right now: this stage's, minus the owner-only ones for a non-owner."""
    names = STAGE_TOOLS.get(stage(session).stage, frozenset())
    return [t for t in TOOLS if t["function"]["name"] in names and (owner or t["function"]["name"] not in OWNER_ONLY)]


def stage_line(session) -> str:
    from . import stages
    return stages.line(session)


def _gate(session, name: str):
    view = stage(session)
    if name not in STAGE_TOOLS.get(view.stage, ()):
        raise Refusal(f"{name} is not available in the {view.stage} stage: {view.why}")


def _locked():
    from . import review, stages
    return (stages.StageLocked, review.ReviewError)


# ---------------------------------------------------------------- the entry points
_STOP = contextvars.ContextVar("orchestrator_tools_stop", default=None)


def _expired(stop) -> bool:
    try:
        return bool(stop is not None and stop.expired())
    except Exception:
        return False


def may_write(session: Session):
    """Raise Refusal when the turn that started this call has run out of time or was cancelled. The deadline is the
    one captured when the outermost call started, so a worker thread left over from a timed-out turn stays stopped
    even after the next turn begins."""
    stop = _STOP.get() or getattr(session, "stop", None)
    if _expired(stop):
        raise Refusal("the turn ran out of time: nothing was written")


def call(session: Session, name: str, args: dict) -> dict:
    """Run one tool. Never raises: problems come back as {"error": ...}. Every call is appended to session.log."""
    try:
        logged_args = json.loads(json.dumps(args, default=str)) if args is not None else {}
    except (TypeError, ValueError):
        logged_args = {"unserialisable": repr(args)[:200]}
    token = _STOP.set(_STOP.get() or getattr(session, "stop", None))
    try:
        spec = SPECS.get(name)
        if spec is None:
            raise Refusal(f"unknown tool; tools: {', '.join(SPECS)}")
        if _expired(_STOP.get()):
            raise Refusal("the turn ran out of time: nothing was run")
        _gate(session, name)
        clean = _check(spec["parameters"], {} if args is None else args, "args")
        with env.LOCK:
            env.bind(session.root, session.tag)
            result = displayed(HANDLERS[name](session, **{k: v for k, v in clean.items() if v is not None}))
        json.dumps(result)
    except (Refusal, *_locked()) as exc:
        result = {"error": str(exc)}
    except Exception as exc:                              # report to the caller (an LLM loop) instead of crashing it
        LOG.warning("tool %s failed", name, exc_info=True)  # the message may carry paths or input: log, don't return
        result = {"error": f"{name} failed ({type(exc).__name__})"}
    finally:
        _STOP.reset(token)
    session.log.append({"name": name, "args": logged_args, "result": result})
    return result


async def acall(session: Session, name: str, args: dict) -> dict:
    """`call` for async code: runs in a worker thread so the event loop keeps going during a scan or a forecast."""
    return await asyncio.to_thread(call, session, name, args)


# ---------------------------------------------------------------- helpers
@contextmanager
def reader(session: Session):
    """cost_price.db, role "reader", opened on a quiet copy (no -wal/-shm left next to the real file)."""
    from changes.executor import quiet_path
    from cost_price.db import connect

    if not session.cost_db.exists():
        raise Refusal("data/cost_price.db not found: run `cost-price build`")
    with quiet_path(session.cost_db) as path, connect(path, "reader") as con:
        yield con


def _day(value, default: date) -> str:
    return (date.fromisoformat(value) if value else default).isoformat()


def _menu_day(session: Session) -> date:
    return session.as_of or Session.today()


def rel(session: Session, path) -> str:
    """A root-relative POSIX path for messages ("data/suggestions/x.json"); just the name if it's outside the root."""
    p = Path(path)
    try:
        return (p if p.is_absolute() else session.root / p).resolve().relative_to(session.root).as_posix()
    except (ValueError, OSError):
        return p.name


def _peer_name(value, folder: str, pattern) -> str | None:
    """A file name a peer agent sent ("X.json" or "data/<folder>/X.json") -> "X.json" if it matches `pattern`."""
    if not isinstance(value, str) or len(value) > 200:
        return None
    parts = value.replace("\\", "/").split("/")
    if len(parts) not in (1, 3) or (len(parts) == 3 and parts[:2] != ["data", folder]):
        return None
    return parts[-1] if pattern.fullmatch(parts[-1]) else None


def report_name(value) -> str | None:
    """A Bookkeeper report name ("R-x.json" or "data/reports/R-x.json") -> "R-x.json", else None."""
    return _peer_name(value, "reports", REPORT_NAME)


def _report_files(session: Session) -> list[Path]:
    """data/reports/R-*.json whose names match REPORT_NAME, regular files only, oldest first."""
    folder = session.data / "reports"
    if not folder.is_dir():
        return []
    files = []
    for p in folder.iterdir():
        try:
            if REPORT_NAME.fullmatch(p.name) and p.is_file() and not p.is_symlink():
                files.append((p.stat().st_mtime_ns, p.name, p))
        except OSError:
            continue
    return [p for *_, p in sorted(files)]


def latest_report(session: Session) -> Path | None:
    reports = _report_files(session)
    return reports[-1] if reports else None


def _load_report(path, source=None, verified=None):
    """A report file (or `source`, a report dict from bk_state.open_findings_report, labelled `path`, with
    `verified` = bk_state.open_verification) -> (path, findings, adjustments, rejected, checks, passed, verification).
    The checks are checks_bk's: with no owner decisions every finding awaits the owner (cautious adjustments only);
    `verification` is what the Bookkeeper's Verifier loop said, as information."""
    import bookkeeper_tools as bt
    import contracts as K

    from . import checks_bk

    if source is None:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
        if not isinstance(raw, dict):
            raise ValueError("report is not a JSON object")
    else:
        raw = source
    findings, adjustments, rejected = K.from_bk_report(raw, bt.bank_id_map())
    if not isinstance(findings, list) or not isinstance(adjustments, list) or not isinstance(rejected, dict):
        raise ValueError("report did not convert to findings")
    checks = checks_bk.check_findings(raw, findings, verified=verified)      # no owner decisions here yet
    passed = K.passed_adjustments(adjustments, checks)
    verification = checks_bk.summary(raw, verified)
    json.dumps(displayed({"f": findings, "a": adjustments, "r": rejected, "c": checks, "p": passed, "v": verification}))
    return (path, findings, adjustments, rejected, checks, passed, verification)


class _Label(str):
    """Stands in for a report path where only `.name` is read: the merged state's label."""
    @property
    def name(self):
        return str(self)


def _state(session: Session):
    """(state, latest bank date, error) for data/orchestrator/bookkeeper_state.json; state is None when there is
    none (or it is unreadable: then `error` says why)."""
    from . import bk_runner, bk_state

    try:
        state = bk_state.load(session.root)
    except bk_state.StateError as exc:
        return None, None, str(exc)
    if state is None or not state.get("covered_through"):
        return None, None, None
    return state, bk_runner.latest_bank_date(session.root, session.tag), None


def _merged(session: Session, state):
    """The findings tuple from the merged state's OPEN findings (bk_state.open_findings_report)."""
    from . import bk_state

    st = bk_state.state_path(session.root).stat()
    key = ("findings-state", st.st_mtime_ns, st.st_size)
    if key not in session.cache:
        label = _Label(f"{bk_state.STATE_NAME}, merged from {len(state['reports'])} report(s)")
        session.cache[key] = _load_report(label, bk_state.open_findings_report(state),
                                          bk_state.open_verification(state))
    return session.cache[key]


def _coverage(state, latest) -> dict:
    from . import bk_state

    behind = bk_state.days_behind(state, latest)
    view = {"baseline_as_of": (state.get("baseline") or {}).get("as_of"), "covered_through": state["covered_through"],
            "latest_bank_date": latest.isoformat() if latest else None, "days_behind": behind,
            "reports_ingested": len(state["reports"]), "counts": bk_state.counts(state)}
    view["note"] = (f"Bookkeeper covers through {state['covered_through']}, {behind} day(s) behind the bank; the "
                    "owner runs `orchestrator bookkeeper-update`" if behind else
                    f"Bookkeeper covers through {state['covered_through']}, up to date with the bank")
    return view


def _findings(session: Session):
    """((report path, findings, adjustments, rejected, checks, passed adjustments, verification) or None,
    [bad reports]).

    The merged Bookkeeper state (bk_state) wins when it exists: its open findings only. Otherwise the newest valid
    report wins; a report a peer named (session.preferred_report) is tried first. A report that can't be read or
    converted never breaks a tool: it is listed as rejected and the previous valid one is used."""
    state, _, _ = _state(session)
    if state is not None:
        try:
            return _merged(session, state), []
        except Exception as exc:                          # fall back to the reports themselves
            LOG.warning("merged Bookkeeper state not usable (%s); using the latest report", type(exc).__name__)
    preferred = report_name(getattr(session, "preferred_report", None))
    order = list(reversed(_report_files(session)))
    order.sort(key=lambda p: p.name != preferred)        # stable: the named one first, then newest first
    bad = []
    for path in order:
        try:
            key = ("findings", path.name, path.stat().st_mtime_ns)
        except OSError:
            continue
        if key not in session.cache:
            try:
                session.cache[key] = _load_report(path)
            except Exception as exc:                      # anything: a malformed report must never break a tool
                session.cache[key] = ("bad", f"not a readable Bookkeeper report ({type(exc).__name__})")
        got = session.cache[key]
        if got[0] == "bad":
            bad.append({"report": path.name, "why": got[1]})
            continue
        return got, bad
    return None, bad


def _round_dollar(cents: int) -> int:
    return (int(cents) + 50) // 100 * 100


def target_price(old: int, target: int, whole_dollars: bool) -> tuple[int, bool]:
    """-> (new price, capped?). Capped at ±30% of the old price, as CH requires; when `whole_dollars`, a capped price
    is rounded inward to a whole dollar so it stays inside the cap."""
    lo, hi = -(-old * 7 // 10), old * 13 // 10               # ceil(0.7 old), floor(1.3 old), integer cents
    new = min(max(int(target), lo), hi)
    capped = new != int(target)
    if capped and whole_dollars:
        new = new // 100 * 100 if new == hi else -(-new // 100) * 100
    return new, capped


def _command(session: Session, path: Path) -> str:
    """The owner's terminal command. For the real nessie_lab it is root-relative (run from nessie_lab/, as the
    README says); only a scratch root needs --root and the file's absolute path."""
    def q(p):
        return f'"{p}"' if " " in str(p) else str(p)

    if session.root == env.DEFAULT_ROOT.resolve():
        opts, target = "", rel(session, path)
    else:
        opts, target = f" --root {q(session.root)}", q(Path(path).resolve())
    opts += "" if session.tag == "boh-1loc" else f" --tag {session.tag}"
    return f"python -m uv run --project changes changes{opts} approve {target} --apply --live"


# ---------------------------------------------------------------- stage 1: what is going on?
def get_cash_position(session: Session) -> dict:
    import bookkeeper_tools as bt

    r = bt.get_account_balances(session.as_of.isoformat() if session.as_of else None)
    if "error" in r:                                      # e.g. as_of outside the opening balances / cutoff
        raise ValueError(r["error"])
    total = r["total_cents"] if "total_cents" in r else r["total_balance_cents"]   # shared impl / pre-#14 wrapper
    return {"as_of": r["as_of"], "total_cents": int(total), "source": bt.BANK_DB.name,
            "accounts": [{"account": a["account"], "nickname": a.get("nickname") or a["account"],
                          "balance_cents": int(a["balance_cents"])} for a in r["accounts"]]}


def list_bills_due(session: Session, before: str | None = None) -> dict:
    import bookkeeper_tools as bt

    bills = []
    for b in bt.get_bank_bills(status="pending")["bills"]:
        due = str(b.get("payment_date") or "")[:10]
        if before and (not due or due > before):
            continue
        nick = b.get("nickname") or ""
        bills.append({"doc": nick.split(" due ")[0], "payee": (b.get("payee") or "").removesuffix(" [SIM]"),
                      "due": due or None, "account": b.get("account"), "amount_cents": int(b["amount_cents"])})
    bills.sort(key=lambda b: (b["due"] or "9999", -b["amount_cents"], b["doc"]))
    return {"before": before, "count": len(bills), "total_cents": sum(b["amount_cents"] for b in bills), "bills": bills}


def lookup(session: Session, tool: str, args: dict | None = None) -> dict:
    import bookkeeper_tools as bt

    r = bt.call_tool(tool, args or {})
    if isinstance(r, dict) and "error" in r:              # the Bookkeeper's message can quote the arguments back
        spec = (getattr(bt, "SPECS", None) or {}).get(tool) or {}
        params = (spec.get("input_schema") or spec.get("parameters") or {}) if isinstance(spec, dict) else {}
        allowed = sorted(params.get("properties", {})) if isinstance(params, dict) else []
        raise Refusal(f"{tool} refused these arguments" + (f"; its arguments are: {', '.join(allowed)}" if allowed else ""))
    return {"tool": tool, "result": r}


def get_findings(session: Session) -> dict:
    got, bad = _findings(session)
    state, latest, state_error = _state(session)
    extra = {"state_error": state_error} if state_error else {}
    if got is None:
        return {"available": False, "rejected_reports": bad, **extra,
                "why": ("no readable Bookkeeper report" if bad else "no Bookkeeper report yet") +
                       " (data/reports/R-*.json is written by the Bookkeeper on the bk branch)"}
    path, findings, adjustments, rejected, checks, passed, ver = got
    if state is not None and isinstance(path, _Label):
        coverage = _coverage(state, latest)
        extra.update(source="merged Bookkeeper state", coverage=coverage, coverage_note=coverage["note"])
    else:
        extra["source"] = "latest Bookkeeper report"
    return {"available": True, "report": path.name, "findings": findings, "adjustments": adjustments,
            "rejected": rejected, "rejected_reports": bad, "checks": checks, "forecast_uses": passed, **extra,
            "verification": ver, "verification_label": ver["label"],
            "checks_note": ("checks_bk: the owner decides every finding (accept, dismiss or revise); until then "
                            "each awaits the owner, so only cautious adjustments (money owed, an ongoing overcharge, "
                            "never as recoverable) reach the forecast. `verification` says what the Bookkeeper's "
                            "Verifier found, as information for the owner; it never accepts a finding")}


def bookkeeper_status(session: Session) -> dict:
    """Read-only: never runs the Bookkeeper, never repairs the state file."""
    from . import bk_runner, bk_state

    try:
        state = bk_state.load(session.root, recover=False)
    except bk_state.StateError as exc:
        return {"available": False, "why": str(exc)}
    return bk_state.status_view(state, bk_runner.latest_bank_date(session.root, session.tag))


def _market_view(session: Session, report, filename: str, service: str) -> dict:
    """A MarketReport -> the scan_market result (the same shape whether the scan ran here or in the scout agent)."""
    from market_scout.analyze import upcoming_summary

    positions = [{k: getattr(p, k) for k in ("our_item", "service", "our_price_cents", "n_prices",
                                             "competitor_median_cents", "competitor_low_cents", "competitor_high_cents",
                                             "price_gap_cents", "position", "upcoming_price_cents", "upcoming_from",
                                             "upcoming_position", "leaving_from")}
                 | {"gap_pct": None if p.gap_pct is None else round(p.gap_pct, 4), "restaurants": p.restaurants}
                 for p in report.positions]
    return {"request_id": report.request_id, "report_id": report.report_id, "filename": filename,
            "menu_as_of": report.menu_as_of, "service": service, "offline": session.offline,
            "positions": positions,
            "medians": {p["our_item"]: {"competitor_median_cents": p["competitor_median_cents"], "n_prices": p["n_prices"]}
                        for p in positions if p["competitor_median_cents"] is not None},
            "upcoming": upcoming_summary(report.positions), "limitations": report.limitations,
            "stored_in_cost_price": False}


def scan_market(session: Session, items: list[str] | None = None, service: str = "dinner") -> dict:
    from market_scout.contracts import MarketRequest
    from market_scout.runner import MarketScout, immutable_json

    rid = session.new_id("orch-scan")
    req = MarketRequest(request_id=rid, items=items or [], service=service, as_of=session.as_of,
                        offline=session.offline)
    scout = MarketScout(session.root, offline=session.offline, use_apify=False if session.offline else None)
    report_id = f"M-{rid}"
    try:
        report, _, _ = scout.scan(req, report_id)         # read-only: write_scan (role "scout") is never called
    except ValueError:                                     # e.g. a dish that isn't on the menu (named in the message)
        raise Refusal("the market scan refused the request: a dish isn't on the menu, or an argument is invalid") \
            from None
    path = session.data / "market_reports" / f"{report_id}.json"
    may_write(session)
    immutable_json(path, report.model_dump(mode="json"))
    return _market_view(session, report, rel(session, path), service)


def market_name(value) -> str | None:
    """A Market scout report name ("M-x.json" or "data/market_reports/M-x.json") -> "M-x.json", else None."""
    return _peer_name(value, "market_reports", MARKET_NAME)


def adopt_market_report(session: Session, filename, request_id: str | None = None, args: dict | None = None) -> dict:
    """Agent mode: the scout agent replies with a MarketResult (positions as {dish: status} plus `filename`). Load
    the report file it names (only data/market_reports/M-*.json) and return exactly what scan_market returns, logged
    as scan_market so the controller, propose_fix and the guard see the usual shape. Never raises: a problem comes
    back as {"error": ...} (and nothing is logged, so the caller can fall back to the in-process scan)."""
    from market_scout.contracts import MarketReport

    try:
        _gate(session, "scan_market")
        name = market_name(filename)
        if name is None:
            raise Refusal("the scout named a report file outside data/market_reports")
        path = session.data / "market_reports" / name
        if not path.is_file() or path.is_symlink():
            raise Refusal("the scout's report file is missing")
        report = MarketReport.model_validate_json(path.read_text(encoding="utf-8"))
        if request_id is not None and report.request_id != request_id:
            raise Refusal("the scout's report is for another request")
        result = displayed(_market_view(session, report, rel(session, path), report.service))
        result["via"] = "market_scout agent"
        json.dumps(result)
    except Refusal as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"the scout's report could not be read ({type(exc).__name__})"}
    session.log.append({"name": "scan_market", "args": dict(args or {}), "result": result})
    return result


def get_menu(session: Session, on: str | None = None) -> dict:
    from cost_price import api

    day = _day(on, _menu_day(session))
    with reader(session) as con:
        menu, latest = api.menu(con, day)
        dishes = []
        for item, price in menu.items():
            plate = api.plate_cost(con, item, day[:7])
            dishes.append({"item": item, "price_cents": price, "plate_cost_cents": plate,
                           "food_cost_pct": None if plate is None else round(plate / price, 4)})
    return {"on": day, "channel": "dine_in", "service": "dinner", "prices_as_of": latest, "dishes": dishes}


def list_upcoming_changes(session: Session, on: str | None = None) -> dict:
    from cost_price import api

    day = _day(on, _menu_day(session))
    with reader(session) as con:
        return {"on": day, "changes": api.upcoming_changes(con, day)}


# ---------------------------------------------------------------- forecasts
def _inputs(session: Session):
    """(view, adjustments, note, as_of) for a forecast: the stage re-checked here (a second layer behind call()),
    adjustments ALWAYS an explicit list from the owner's committed decisions, as_of the review's."""
    from . import review, stages

    view = stages.require(session, stages.FORECAST)
    adjustments, note = review.adjustments(session, view)
    as_of = date.fromisoformat(view.as_of) if view.as_of else session.as_of
    if view.as_of and session.as_of and session.as_of != as_of:
        raise Refusal(f"--as-of conflicts with the review's as_of {view.as_of}: forecasts use the review's date")
    return view, adjustments, note, as_of


def _unchanged(session: Session, view):
    """Recompute the data fingerprint fully; Refusal if it moved since the audit (the forecast is not used)."""
    from . import stages

    if view.review_id is None or view.fingerprint is None:
        return
    fp = stages.fingerprint(session.root, session.tag, view.as_of, fresh=True)
    if any(fp.get(k) != view.fingerprint.get(k) for k in stages.FP_PARTS):
        raise Refusal("the data changed since the audit: the forecast was not used; the owner runs "
                      "`orchestrator bookkeeper-update`")


def _run(session: Session, scenario, adjustments, view, as_of):
    """run_forecast with an explicit list (never None: None would let the forecaster load a report itself), the
    review's as_of in both places, never report_id; dropped if the data moved during the run."""
    import contracts as K
    import forecaster as F

    if not isinstance(adjustments, list):
        raise Refusal("no adjustments list: refusing to let the forecaster read a report by itself")
    _unchanged(session, view)
    fr = F.run_forecast(scenario, adjustments=adjustments, settings=K.ForecastSettings(as_of=as_of), as_of=as_of,
                        save=True)
    if as_of is not None and fr.as_of != as_of:
        raise Refusal("the forecaster used another as_of than the review's: the result was not used")
    _unchanged(session, view)
    return fr


def _detail(forecast_id: str) -> dict:
    import forecaster as F
    return json.loads((F.FORECASTS / f"{forecast_id}.json").read_text())


def _view(fr) -> dict:
    return {"forecast_id": fr.forecast_id, "scenario": fr.scenario, "as_of": fr.as_of,
            "starting_cash_cents": fr.starting_cash_cents,
            "run_out_worst": fr.run_out_worst, "run_out_likely": fr.run_out_likely, "run_out_best": fr.run_out_best,
            "share_running_out": fr.share_running_out, "running_out_pct": fr.share_running_out,
            "below_floor_likely": fr.below_floor_likely, "safety_floor_cents": fr.safety_floor_cents,
            "trials": fr.trials, "horizon_months": fr.horizon_months, "seed": fr.seed, "chart_path": fr.chart_path,
            "assumptions": fr.assumptions}


def _chart(session: Session, path) -> str | None:
    return rel(session, path) if path else None


def _base(session: Session) -> dict:
    """The base forecast for the committed review (its decisions and as_of), run once per session (deterministic
    seed). Recording it opens the Suggest stage."""
    from . import review

    view, adjustments, note, as_of = _inputs(session)
    key = ("base", str(as_of), view.digest, json.dumps([a.model_dump(mode="json") for a in adjustments],
                                                       sort_keys=True))
    if key not in session.cache:
        may_write(session)                                 # run_forecast(save=True) writes data/forecasts/<id>.json
        fr = _run(session, None, adjustments, view, as_of)
        if view.review_id is not None and view.digest:
            review.record_base_forecast(session, view, fr.forecast_id)
        out = _view(fr)
        out["chart_path"] = _chart(session, out["chart_path"])
        out["adjustments_used"] = [{"finding_id": a.finding_id, "kind": a.kind} for a in adjustments]
        out["adjustments_note"] = note
        out["provenance"] = {"review_id": view.review_id, "decisions": view.digest[:16] if view.digest else None,
                             "as_of": view.as_of, "report": view.report}
        out["report_note"] = _detail(fr.forecast_id).get("report_note")
        session.cache[key] = out
    return session.cache[key]


def forecast_base(session: Session) -> dict:
    out = dict(_base(session))
    out["not_simulated"] = ("one-off shocks (a walk-in fridge failure, a slow month) are not in the base case; "
                            "test one with forecast_scenario")
    return out


def forecast_scenario(session: Session, scenario: dict) -> dict:
    import contracts as K

    try:
        sc = K.Scenario.model_validate(scenario)
    except ValueError as exc:                              # name the field, never the value that was sent
        errors = exc.errors() if hasattr(exc, "errors") else None
        field = str(errors[0]["loc"][0]) if errors and errors[0].get("loc") else ""
        field = field if field in SCENARIO["properties"] else "scenario"   # deeper loc parts can be caller keys
        raise Refusal(f"scenario: invalid {field} ({errors[0]['type'] if errors else 'value'})") from None
    if sc == K.Scenario(name=sc.name):
        raise Refusal("the scenario changes nothing; for the base case use forecast_base")
    base = _base(session)
    view, adjustments, _, as_of = _inputs(session)
    key = ("scenario", base["forecast_id"], sc.model_dump_json())
    if key not in session.cache:
        may_write(session)
        try:
            fr = _run(session, sc, adjustments, view, as_of)
        except Refusal:
            raise
        except ValueError:                                 # unknown dish or supplier, date outside the window
            raise Refusal("scenario: the forecaster refused it (a dish or supplier it doesn't know, or a date "
                          "outside the forecast window)") from None
        effects = _detail(fr.forecast_id).get("scenario_effects", [])
        menu = [e["approx_net_per_month_cents"] for e in effects if "approx_net_per_month_cents" in e]
        vs = fr.vs_base.model_dump(mode="json") if fr.vs_base else {}
        vs["base_forecast_id"] = base["forecast_id"]
        session.cache[key] = {
            "forecast_id": fr.forecast_id, "base_forecast_id": base["forecast_id"], "scenario": sc,
            "before": {k: base[k] for k in ("run_out_worst", "run_out_likely", "run_out_best", "below_floor_likely",
                                            "share_running_out")},
            "after": {"run_out_worst": fr.run_out_worst, "run_out_likely": fr.run_out_likely,
                      "run_out_best": fr.run_out_best, "below_floor_likely": fr.below_floor_likely,
                      "share_running_out": fr.share_running_out},
            "vs_base": vs, "monthly_cash_effect_cents": int(sum(menu)) if menu else None,
            "effects": effects, "assumptions": fr.assumptions, "chart_path": _chart(session, fr.chart_path)}
    return session.cache[key]


# ---------------------------------------------------------------- stage 2: what do we do about it?
def market_median(session: Session, dish: str) -> tuple[int | None, dict | None]:
    """(competitor median, position) for a dish from this session's latest scan that covered it; scans if none did."""
    for entry in reversed(session.log):
        if entry["name"] == "scan_market" and "error" not in entry["result"]:
            for p in entry["result"]["positions"]:
                if p["our_item"] == dish and p["service"] == "dinner":
                    return p["competitor_median_cents"], p
    r = call(session, "scan_market", {"items": [dish]})
    if "error" in r:
        raise Refusal(f"market scan: {r['error']}")
    p = next((p for p in r["positions"] if p["our_item"] == dish), None)
    return (p or {}).get("competitor_median_cents"), p


def _ch():
    from changes.cli import question
    from changes.executor import DRY_RUN, Executor, Refused
    from changes.shared import Suggestion
    return Suggestion, Executor, Refused, DRY_RUN, question


def _dry_run(session: Session, s) -> dict:
    """CH's checks, read-only: plan (fields, title, ±30%, ids, menu) and a dry-run apply that must report DRY_RUN."""
    _, Executor, Refused, DRY_RUN, question = _ch()
    ex = Executor(session.root, tag=session.tag)
    try:
        shown, ops = ex.plan(s)
    except Refused as exc:
        raise Refusal(f"CH would refuse it: {exc}") from None
    r = ex.apply(s, live=False)
    if r.applied or r.error != DRY_RUN:
        raise Refusal(f"CH's dry run did not pass: {r.error}")
    return {"question": question(shown, ops, ex.today), "planned_change": shown.get("planned_change"),
            "dry_run": r.model_dump(mode="json")}


def propose_fix(session: Session, dish: str, new_price_cents: int | None = None,
                action: str = "reprice_item") -> dict:
    from changes.executor import ALL_COURSES
    from cost_price import api

    Suggestion, *_ = _ch()
    eff = session.effective_date()
    with reader(session) as con:
        on_menu = api.menu(con, eff.isoformat(), courses=ALL_COURSES)[0]
        old = api.effective_price(con, dish, eff.isoformat())
        pending = [u for u in api.upcoming_changes(con, Session.today().isoformat()) if u["item"] == dish]
    if dish not in on_menu:                                # the name the caller typed is never echoed
        raise Refusal(f"that dish isn't on the menu on {eff}; dishes: {', '.join(sorted(on_menu))}")
    if pending:
        u = pending[0]
        raise Refusal(f"{dish} already has an approved change from {u['effective_from']} ({u['kind']}); "
                      "wait until it is in force")
    if action == "remove_item" and new_price_cents is not None:
        raise Refusal("new_price_cents belongs only to reprice_item")
    capped, median = False, None
    if action == "reprice_item":
        if old is None:
            raise Refusal(f"{dish} has no dine-in dinner price on {eff}")
        if new_price_cents is None:
            median, _ = market_median(session, dish)
            if median is None:
                raise Refusal(f"no competitor median for {dish} (fewer than 3 trusted prices): give new_price_cents")
            new, capped = target_price(old, _round_dollar(median), whole_dollars=True)
        else:
            new, capped = target_price(old, new_price_cents, whole_dollars=False)
        if new == old:
            raise Refusal(f"{dish} already costs {usd(old)}: nothing to change")
        scenario = {"name": f"{dish} {usd(old)} to {usd(new)}", "price_changes_cents": {dish: new},
                    "changes_start": eff.isoformat()}
        verb = "Raise" if new > old else "Lower"
        title = (f"{verb} {dish} from {usd(old)} to {usd(new)}" if max(old, new) < 100_000
                 else f"{verb} {dish} (dine-in dinner)")       # "$1,000.00" wouldn't parse in CH's title check
    else:
        new = None
        scenario = {"name": f"take {dish} off the menu", "remove_items": [dish], "changes_start": eff.isoformat()}
        title = f"Take {dish} off the menu"
    fc = call(session, "forecast_scenario", {"scenario": scenario})
    if "error" in fc:
        raise Refusal(f"forecast: {fc['error']}")
    extra = {"old_price_cents": old if action == "reprice_item" else None, "competitor_median_cents": median,
             "capped": capped,
             "forecast": {"forecast_id": fc["forecast_id"], "base_forecast_id": fc["base_forecast_id"],
                          "before": fc["before"], "after": fc["after"], "vs_base": fc["vs_base"]}}
    twin = _pending_twin(session, action, dish, new)
    if twin is not None:                                   # the same proposal is already waiting: don't write another
        path, s = twin
        try:
            checked = _dry_run(session, s)
        except Refusal:
            twin = None                                    # it no longer passes CH: write a fresh one below
        else:
            return {"path": rel(session, path), "suggestion": s, **checked, "approve_command": _command(session, path),
                    "reused": True, **extra}
    sid = session.new_id("orch")
    raw = dict(suggestion_id=sid, action=action, title=title, target=dish, new_price_cents=new,
               monthly_cash_effect_cents=fc["monthly_cash_effect_cents"],
               run_out_likely_before=fc["before"]["run_out_likely"], run_out_likely_after=fc["after"]["run_out_likely"],
               assumptions=fc["assumptions"] or [f"forecast {fc['forecast_id']}"],
               based_on=[fc["base_forecast_id"], fc["forecast_id"], dish])
    if new is None:
        raw.pop("new_price_cents")
    s = Suggestion.model_validate(raw)
    checked = _dry_run(session, s)                         # raises before anything is written
    path = session.data / "suggestions" / f"{sid}.json"
    may_write(session)                                     # a timed-out turn writes nothing more
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "x", encoding="utf-8") as f:           # never replaces a file
        f.write(s.model_dump_json(indent=2, exclude_none=True))
    return {"path": rel(session, path), "suggestion": s, **checked, "approve_command": _command(session, path),
            "reused": False, **extra}


def _pending_twin(session: Session, action: str, target: str, new: int | None):
    """(path, Suggestion) of the newest suggestion file this orchestrator wrote for the same (action, dish, new
    price) less than 24 hours ago, else None. A change that was approved and applied never reaches this point:
    propose_fix refuses a dish with an approved upcoming change, and an applied price "already costs" it."""
    Suggestion, *_ = _ch()
    folder = session.data / "suggestions"
    if not folder.is_dir():
        return None
    now, best = time.time(), None
    for p in folder.glob("orch-*.json"):
        try:
            st = p.stat()
            if not p.is_file() or p.is_symlink() or now - st.st_mtime > TWIN_SECONDS or not ID_RE.fullmatch(p.stem):
                continue
            s = Suggestion.model_validate_json(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        if s.suggestion_id != p.stem:
            continue
        if (s.action, s.target, getattr(s, "new_price_cents", None)) == (action, target, new):
            if best is None or st.st_mtime > best[0]:
                best = (st.st_mtime, p, s)
    return None if best is None else (best[1], best[2])


def preview_suggestion(session: Session, path: str) -> dict:
    Suggestion, *_ = _ch()
    folder = (session.data / "suggestions").resolve()
    p = Path(path)
    p = (p if p.is_absolute() else (folder / p if len(p.parts) == 1 else session.root / p)).resolve()
    if p.parent != folder or p.suffix != ".json":
        raise Refusal("only suggestion files in data/suggestions can be previewed")
    if not p.is_file():
        raise Refusal("that suggestion file isn't in data/suggestions")
    s = Suggestion.model_validate_json(p.read_text(encoding="utf-8"))
    return {"path": rel(session, p), "suggestion": s, **_dry_run(session, s), "approve_command": _command(session, p)}


# ---------------------------------------------------------------- the owner's review (proposals only)
def review_status(session: Session) -> dict:
    from . import review
    return review.status(session)


def propose_decision(session: Session, finding: str, decision: str, note: str | None = None) -> dict:
    from . import review
    return review.propose(session, finding, decision, note, by="llm")


def propose_accept_verified_errors(session: Session) -> dict:
    from . import review
    return review.accept_verified_errors(session)


HANDLERS = {"get_cash_position": get_cash_position, "list_bills_due": list_bills_due, "lookup": lookup,
            "get_findings": get_findings, "scan_market": scan_market, "get_menu": get_menu,
            "list_upcoming_changes": list_upcoming_changes, "forecast_base": forecast_base,
            "forecast_scenario": forecast_scenario, "propose_fix": propose_fix,
            "preview_suggestion": preview_suggestion, "bookkeeper_status": bookkeeper_status,
            "review_status": review_status, "propose_decision": propose_decision,
            "propose_accept_verified_errors": propose_accept_verified_errors}
assert set().union(*STAGE_TOOLS.values()) == set(SPECS)
assert HANDLERS.keys() == SPECS.keys()
