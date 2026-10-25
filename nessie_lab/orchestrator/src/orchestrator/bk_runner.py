"""Running the Bookkeeper (the teammate's, on the bk branch) and keeping bk_state up to date.

The Bookkeeper runs only from its own uv project (nessie_lab/bookkeeper), so it is called as a SUBPROCESS:

    <uv> run --project <nessie_lab>/bookkeeper bookkeeper --root <root> run --request-id <id> --as-of <date> --tag <tag>

(run from nessie_lab/). Since bk 872f15e a run is TWO investigations: the Bookkeeper drafts the report, then its
own Verifier repeats the investigation independently, compares, sends disagreements back for revision rounds, and
publishes ONE verified report (schema 1.2). That is ~40 ASI calls plus revisions and can take most of an hour, so the
timeouts are SETUP_TIMEOUT = 3600 s and UPDATE_TIMEOUT = 1200 s. The CLI prints its ReportResult as JSON:
{request_id, report_id, filename, as_of, verified} (no `status` key on success); `filename` names
data/reports/R-*.json and is pattern-checked. It needs ASI1_API_KEY and does one job at a time; a run lock
(data/orchestrator/bookkeeper_run.lock) makes sure the orchestrator never starts two.

A transport failure with a trace id is retried once with `bookkeeper resume --trace-id TR-...`, which now also runs
the Verifier, so the filename it prints is the final, published report. On a timeout the message names the report
the run may still publish, and `bookkeeper verify --report-id R-...` for a draft that was saved but not verified.

Inside the running uAgent (`orchestrator serve` with BOOKKEEPER_ADDR set) the request goes to the Bookkeeper agent
instead (asetup / aupdate, by default; ORCHESTRATOR_USE_AGENTS=0 turns it off). Its reply is the same ReportResult,
or {request_id, status: "error", error, error_type, trace_id}, which raises BookkeeperError. A Bookkeeper agent that
fails fast falls back to the subprocess (marked degraded); one that times out does not (it may still be running, and
a second run would double the ASI calls).

These are the owner's operations (`orchestrator bookkeeper-setup` / `bookkeeper-update`): no LLM tool runs them.
A turn may run a quick DELTA update first only with ORCHESTRATOR_BK_AUTO=1 and a delta-capable Bookkeeper
(pre_turn); it never starts a full rerun."""
import asyncio
import json
import os
import re
import secrets
import shlex
import shutil
import sqlite3
import subprocess
import sys
import time
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from . import bk_adapter as A
from . import bk_state as S
from . import env

SENDER = "local"                       # the fixed sender: `bookkeeper run` calls Bookkeeper.run(request) (sender "local")
TRACE_ID = re.compile(r"TR-[a-f0-9]{32}")
TRANSPORT_HINTS = ("timed out", "Could not reach", "HTTP 429", "HTTP 500", "HTTP 502", "HTTP 503", "HTTP 504")
SETUP_TIMEOUT, UPDATE_TIMEOUT = 3600, 1200        # two investigations per run now (see above)
AUTO_MIN_SECONDS, AUTO_RESERVE_SECONDS = 5, 20


class BookkeeperError(RuntimeError):
    """The Bookkeeper could not be run, or its run failed; the message says why and what to do."""


def bookkeeper_project() -> Path:
    return env.NESSIE_LAB / "bookkeeper"


def latest_bank_date(root, tag: str = "boh-1loc") -> date | None:
    """MAX(date) of bank_tx in data/nessie_sync_<tag>.db (opened read-only), or None when there is no sync DB."""
    db = Path(root) / "data" / f"nessie_sync_{tag}.db"
    if not db.is_file():
        return None
    try:
        with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            value = con.execute("SELECT MAX(date) FROM bank_tx").fetchone()[0]
    except sqlite3.Error:
        return None
    try:
        return date.fromisoformat(str(value)[:10]) if value else None
    except ValueError:
        return None


def new_request_id(as_of: date) -> str:
    return f"orch-bk-{as_of.isoformat()}-{secrets.token_hex(4)}"


def expected_report(request_id: str, sender: str = SENDER) -> str:
    """The file the Bookkeeper writes for this request: data/reports/R-<sha256(sender\\0request_id)>.json."""
    import hashlib
    return f"R-{hashlib.sha256(f'{sender}\0{request_id}'.encode()).hexdigest()}.json"


def uv_command(environ=None) -> list[str]:
    environ = os.environ if environ is None else environ
    if environ.get("ORCHESTRATOR_BK_UV"):
        return shlex.split(environ["ORCHESTRATOR_BK_UV"], posix=os.name != "nt")
    uv = shutil.which("uv")
    if uv:
        return [uv]
    return [shutil.which("python") or sys.executable, "-m", "uv"]


def command(root, payload: dict, project: Path | None = None, environ=None) -> list[str]:
    project = project or bookkeeper_project()
    return [*uv_command(environ), "run", "--project", str(project), "bookkeeper", "--root", str(Path(root).resolve()),
            *A.cli_args(payload)]


def _reply(stdout: str) -> dict | None:
    """The ReportResult from the CLI's stdout (pretty-printed JSON, possibly after log lines): the first JSON object
    with a string `filename`."""
    text = stdout or ""
    candidates = [text]
    start = text.find("{")
    while start != -1:
        candidates.append(text[start:])
        start = text.find("{", start + 1)
    for c in candidates:
        try:
            value = json.loads(c)
        except ValueError:
            continue
        if isinstance(value, dict) and isinstance(value.get("filename"), str):
            return value
    return None


def _filename(stdout: str) -> str | None:
    reply = _reply(stdout)
    return None if reply is None else reply["filename"]


def verify_hint(request_id: str, sender: str = SENDER) -> str:
    """What to run when a draft may be saved but its verification did not finish."""
    rid = expected_report(request_id, sender)[:-5]
    return (f"if the Bookkeeper saved its draft but the Verifier did not finish, run `uv run --project bookkeeper "
            f"bookkeeper verify --report-id {rid}` from nessie_lab/, then `orchestrator bookkeeper-ingest "
            f"data/reports/{rid}.json`")


def _failure(stderr: str) -> str:
    lines = [ln.strip() for ln in (stderr or "").splitlines() if ln.strip()]
    bad = [ln for ln in lines if ln.startswith("Bookkeeper failed")]
    msg = (bad or lines or ["no output"])[-1]
    return re.sub(r"(?i)(api[_-]?key|token|bearer)\S*\s*[:=]?\s*\S+", r"\1 <hidden>", msg)[:300]


def _report_path(root, filename: str) -> Path:
    from .tools import report_name
    name = report_name(filename)
    if name is None:
        raise BookkeeperError("the Bookkeeper named a report outside data/reports")
    path = Path(root) / "data" / "reports" / name
    if not path.is_file():
        raise BookkeeperError(f"the Bookkeeper said it wrote data/reports/{name}, but the file is not there")
    return path


def run_bookkeeper(root, as_of: date | None = None, since: date | None = None, prior: str | None = None,
                   timeout: float = UPDATE_TIMEOUT, tag: str = "boh-1loc", **kw) -> Path:
    """Run the Bookkeeper once (draft + its Verifier loop) and return the published report (data/reports/R-*.json).
    See run_verified for the arguments."""
    return run_verified(root, as_of, since, prior, timeout, tag, **kw)[0]


def run_verified(root, as_of: date | None = None, since: date | None = None, prior: str | None = None,
                 timeout: float = UPDATE_TIMEOUT, tag: str = "boh-1loc", *, runner=None,
                 environ=None, project: Path | None = None, progress=None, agent=None) -> tuple[Path, bool | None]:
    """-> (report path, the reply's `verified`). as_of is clamped to the latest bank date (a later one makes the
    Bookkeeper raise). `agent`: an async callable(payload) -> reply dict (agent mode), else the subprocess. Raises
    BookkeeperError with a clear message."""
    environ = dict(os.environ if environ is None else environ)
    runner = runner or tree_run                           # looked up per call, so tests can replace subprocess.run
    say = progress or (lambda *_: None)
    latest = latest_bank_date(root, tag)
    if latest is None:
        raise BookkeeperError(f"data/nessie_sync_{tag}.db has no bank rows: run nessie_sync.py first")
    as_of = min(as_of or latest, latest)
    if since is not None and since > as_of:
        raise BookkeeperError(f"nothing to do: since {since} is after the latest bank date {as_of}")
    rid = new_request_id(as_of)
    payload = A.build_request(as_of, since, prior, request_id=rid, tag=tag, environ=environ)
    if agent is not None:
        return _via_agent(root, agent, payload, timeout)
    project = project or bookkeeper_project()
    if not (project / "pyproject.toml").is_file():
        raise BookkeeperError("the Bookkeeper project (nessie_lab/bookkeeper) is not here: merge the bk branch, or "
                              "ingest a report the Bookkeeper owner made with `orchestrator bookkeeper-ingest <path>`")
    if not environ.get("ASI1_API_KEY"):
        raise BookkeeperError("ASI1_API_KEY is not set (nessie_lab/.env or BackHouse/.env): the Bookkeeper needs it")
    for name in ("VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):        # let uv use the Bookkeeper's own environment
        environ.pop(name, None)
    argv = command(root, payload, project, environ)
    with S.lock(root, "bookkeeper_run.lock", wait=1, stale=max(timeout, 60) + 60):
        say(f"Bookkeeper: request {rid}, as_of {as_of}" + (f", since {payload[A.SINCE_FIELD]}"
                                                            if A.SINCE_FIELD in payload else " (full window)"))
        started = time.monotonic()
        out = _run(runner, argv, timeout, environ, rid)
        if out.returncode != 0:
            why, trace = _failure(out.stderr), TRACE_ID.search((out.stderr or "") + (out.stdout or ""))
            if trace and any(h in why for h in TRANSPORT_HINTS):
                left = timeout - (time.monotonic() - started)
                say(f"Bookkeeper: transport failure ({why}); resuming trace {trace.group(0)}")
                if left > 5:
                    argv = [*uv_command(environ), "run", "--project", str(project), "bookkeeper", "--root",
                            str(Path(root).resolve()), "resume", "--trace-id", trace.group(0)]
                    out = _run(runner, argv, left, environ, rid)
                    if out.returncode == 0:                 # resume verifies too: this filename is final
                        return _done(root, out, rid)
                    why = _failure(out.stderr)
            hint = f"; {verify_hint(rid)}" if "erif" in why else ""
            raise BookkeeperError(f"the Bookkeeper run failed: {why}. Run it again (a new request id is used each "
                                  f"time){hint}")
        return _done(root, out, rid)


_ORIGINAL_RUN = subprocess.run


def tree_run(argv, *, timeout=None, **kw):
    """subprocess.run, except a timeout kills the whole process tree. `uv run` starts the Bookkeeper as a child
    process; plain subprocess.run would kill only uv and leave the Bookkeeper running (and spending ASI calls) on
    Windows. When subprocess.run has been replaced (tests), defer to it so fakes and guards still apply."""
    if subprocess.run is not _ORIGINAL_RUN:
        return subprocess.run(argv, timeout=timeout, **kw)
    capture = kw.pop("capture_output", False)
    if capture:
        kw.update(stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if os.name == "nt":
        kw["creationflags"] = kw.get("creationflags", 0) | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    with subprocess.Popen(argv, **kw) as proc:
        try:
            out, err = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.call(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                import signal
                os.killpg(proc.pid, signal.SIGKILL)
            proc.communicate()
            raise
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _run(runner, argv, timeout, environ, rid):
    try:
        return runner(argv, cwd=str(env.NESSIE_LAB), capture_output=True, text=True, encoding="utf-8",
                      errors="replace", timeout=timeout, env=environ)
    except subprocess.TimeoutExpired:
        raise BookkeeperError(f"the Bookkeeper did not finish within {int(timeout)} s (a run is two investigations "
                              f"now). It may still write data/reports/{expected_report(rid)}; if it does, run "
                              f"`orchestrator bookkeeper-ingest data/reports/{expected_report(rid)}`; "
                              f"{verify_hint(rid)}; or retry with a longer --timeout") from None
    except FileNotFoundError:
        raise BookkeeperError("could not start uv (set ORCHESTRATOR_BK_UV to the uv command)") from None


def _done(root, out, rid) -> tuple[Path, bool | None]:
    reply = _reply(out.stdout)
    if reply is None:
        raise BookkeeperError(f"the Bookkeeper finished but printed no report filename (expected "
                              f"data/reports/{expected_report(rid)})")
    verified = reply.get("verified") if isinstance(reply.get("verified"), bool) else None
    return _report_path(root, reply["filename"]), verified


def _clean(text, cap=300) -> str:
    text = re.sub(r"[\x00-\x1f\x7f]", " ", str(text or ""))
    return re.sub(r"(?i)(api[_-]?key|token|bearer)\S*\s*[:=]?\s*\S+", r"\1 <hidden>", text)[:cap]


class AgentTimeout(BookkeeperError):
    """The Bookkeeper agent did not answer in time; it may still be running (so nothing falls back)."""


def adopt_reply(root, reply, request_id: str | None = None, sender: str = SENDER) -> tuple[Path, bool | None]:
    """A Bookkeeper agent reply -> (report path, verified). An error reply ({status: "error", error, trace_id})
    raises BookkeeperError; a success reply has NO status key: {request_id, report_id, filename, as_of, verified}."""
    if not isinstance(reply, dict):
        raise BookkeeperError("the Bookkeeper agent's reply was not a JSON object")
    if reply.get("status") == "error":
        trace = TRACE_ID.search(str(reply.get("trace_id") or ""))
        more = f"; resume it with `bookkeeper resume --trace-id {trace.group(0)}`" if trace else ""
        if "erif" in str(reply.get("error")) and request_id:
            more += f"; {verify_hint(request_id, sender)}"
        raise BookkeeperError(f"the Bookkeeper agent failed: {_clean(reply.get('error')) or 'no detail'}{more}")
    if request_id and reply.get("request_id") not in (None, request_id):
        raise BookkeeperError("the Bookkeeper agent replied for another request")
    if not isinstance(reply.get("filename"), str):
        raise BookkeeperError("the Bookkeeper agent replied without a report filename")
    verified = reply.get("verified") if isinstance(reply.get("verified"), bool) else None
    return _report_path(root, reply["filename"]), verified


async def arun_agent(root, agent, payload: dict, timeout: float, sender: str = SENDER) -> tuple[Path, bool | None]:
    """Agent mode on the caller's event loop: send the ReportRequest payload, adopt the report it names."""
    try:
        reply = await asyncio.wait_for(agent(payload), timeout)
    except asyncio.TimeoutError:
        raise AgentTimeout(f"the Bookkeeper agent did not reply within {int(timeout)} s. It may still publish "
                           f"data/reports/{expected_report(payload['request_id'], sender)}; ingest it with "
                           f"`orchestrator bookkeeper-ingest`, or "
                           f"{verify_hint(payload['request_id'], sender)}") from None
    return adopt_reply(root, reply, payload.get("request_id"), sender)


def _via_agent(root, agent, payload, timeout) -> tuple[Path, bool | None]:
    return asyncio.run(arun_agent(root, agent, payload, timeout))       # sync callers only (no running loop)


# ---------------------------------------------------------------- the owner's operations
def _said(verified) -> str:
    return {True: "verified by its Verifier", False: "NOT verified"}.get(verified, "verification not reported")


def setup(root, as_of=None, timeout=SETUP_TIMEOUT, force=False, tag="boh-1loc", progress=print, **kw) -> dict:
    """The one-time FULL backfill over the whole bank window. A no-op when a baseline exists, unless force."""
    state = S.load(root)
    if state and state.get("baseline") and not force:
        return {"status": "exists", "baseline": state["baseline"], "covered_through": state["covered_through"]}
    started = time.monotonic()
    progress("Bookkeeper backfill: one full run over the whole bank window, drafted by the Bookkeeper and checked "
             "by its Verifier (two investigations, ~40 ASI calls plus revisions; can take most of an hour)")
    path, verified = run_verified(root, as_of=as_of, timeout=timeout, tag=tag, progress=progress, **kw)
    progress(f"Bookkeeper: wrote data/reports/{path.name} ({_said(verified)}); ingesting")
    merged = S.ingest(root, path, baseline=True)
    return {"status": "done", "seconds": round(time.monotonic() - started, 1), "report": path.name,
            "reply_verified": verified, **merged}


def plan_update(root, tag="boh-1loc", allow_full=True, environ=None) -> dict:
    """What an update would do: {"status": "up_to_date"|"delta"|"full"|"skipped", ...}. Never runs anything."""
    environ = os.environ if environ is None else environ
    state = S.load(root)
    if not state or not state.get("baseline"):
        raise BookkeeperError("no Bookkeeper baseline yet: run `orchestrator bookkeeper-setup` first")
    latest = latest_bank_date(root, tag)
    covered = date.fromisoformat(state["covered_through"])
    base = {"covered_through": state["covered_through"], "latest_bank_date": latest.isoformat() if latest else None}
    if latest is None or covered >= latest:
        return {"status": "up_to_date", **base}
    if A.incremental_enabled(environ):
        return {"status": "delta", "as_of": latest, "since": covered + timedelta(days=1),
                "prior": state["reports"][-1]["report_id"], **base}
    if allow_full:
        return {"status": "full", "as_of": latest, **base}
    return {"status": "skipped", "why": "only a full rerun is possible, and that is never automatic", **base}


def update(root, timeout=UPDATE_TIMEOUT, tag="boh-1loc", allow_full=True, progress=print, environ=None, **kw) -> dict:
    """Bring the state up to the latest bank date. With ORCHESTRATOR_BK_INCREMENTAL=1: a delta request since
    covered_through+1 (with prior_report_id); else, only when allow_full, a full rerun at the latest date, diffed
    by ingest. -> {"status": "up_to_date"|"updated"|"skipped", ...}"""
    plan = plan_update(root, tag, allow_full, environ)
    if plan["status"] in ("up_to_date", "skipped"):
        return plan
    started = time.monotonic()
    if plan["status"] == "delta":
        progress(f"Bookkeeper update: delta {plan['since']} .. {plan['as_of']}")
        path, verified = run_verified(root, as_of=plan["as_of"], since=plan["since"], prior=plan["prior"],
                                      timeout=timeout, tag=tag, progress=progress, environ=environ, **kw)
    else:
        progress(f"Bookkeeper update: the Bookkeeper has no delta mode yet (ORCHESTRATOR_BK_INCREMENTAL is off), so "
                 f"this is a full, verified rerun through {plan['as_of']}, diffed against the state")
        path, verified = run_verified(root, as_of=plan["as_of"], timeout=timeout, tag=tag, progress=progress,
                                      environ=environ, **kw)
    progress(f"Bookkeeper: wrote data/reports/{path.name} ({_said(verified)}); ingesting")
    merged = S.ingest(root, path)
    return {"status": "updated", "seconds": round(time.monotonic() - started, 1), "report": path.name,
            "reply_verified": verified, **merged}


# ---------------------------------------------------------------- the served orchestrator: through the agent
async def asetup(root, agent, timeout=SETUP_TIMEOUT, force=False, tag="boh-1loc", sender=SENDER, environ=None) -> dict:
    """setup() through the Bookkeeper agent, on the caller's event loop (the uAgent). agent: async callable(payload)
    -> reply dict; sender: the orchestrator's agent address (the Bookkeeper keys its report id on it)."""
    state = await asyncio.to_thread(S.load, root)
    if state and state.get("baseline") and not force:
        return {"status": "exists", "baseline": state["baseline"], "covered_through": state["covered_through"]}
    latest = await asyncio.to_thread(latest_bank_date, root, tag)
    if latest is None:
        raise BookkeeperError(f"data/nessie_sync_{tag}.db has no bank rows: run nessie_sync.py first")
    started = time.monotonic()
    payload = A.build_request(latest, request_id=new_request_id(latest), tag=tag, environ=environ)
    path, verified = await arun_agent(root, agent, payload, timeout, sender)
    merged = await asyncio.to_thread(S.ingest, root, path, True)
    return {"status": "done", "seconds": round(time.monotonic() - started, 1), "report": path.name,
            "reply_verified": verified, "via": "bookkeeper agent", **merged}


async def aupdate(root, agent, timeout=UPDATE_TIMEOUT, tag="boh-1loc", allow_full=True, sender=SENDER,
                  environ=None) -> dict:
    """update() through the Bookkeeper agent, on the caller's event loop."""
    plan = await asyncio.to_thread(plan_update, root, tag, allow_full, environ)
    if plan["status"] in ("up_to_date", "skipped"):
        return plan
    started = time.monotonic()
    payload = A.build_request(plan["as_of"], plan.get("since"), plan.get("prior"),
                              request_id=new_request_id(plan["as_of"]), tag=tag, environ=environ)
    path, verified = await arun_agent(root, agent, payload, timeout, sender)
    merged = await asyncio.to_thread(S.ingest, root, path)
    return {"status": "updated", "seconds": round(time.monotonic() - started, 1), "report": path.name,
            "reply_verified": verified, "via": "bookkeeper agent", **merged}


# ---------------------------------------------------------------- before a turn
def behind_note(root, tag="boh-1loc") -> tuple[str | None, dict | None, date | None]:
    try:
        state = S.load(root)
    except S.StateError:
        return "Bookkeeper state is unreadable; run `orchestrator bookkeeper-status`", None, None
    if not state or not state.get("covered_through"):
        return None, state, None
    latest = latest_bank_date(root, tag)
    if latest is None or date.fromisoformat(state["covered_through"]) >= latest:
        return None, state, latest
    return (f"Bookkeeper covers through {state['covered_through']}; run `orchestrator bookkeeper-update`",
            state, latest)


async def pre_turn(session, stop=None, dispatch=None, runner=None, environ=None) -> list[str]:
    """Notes for a turn when the Bookkeeper state is behind the bank. With ORCHESTRATOR_BK_AUTO=1, INCREMENTAL=1 and
    a delta-capable Bookkeeper, run a quick DELTA update first, inside the turn's deadline. Never a full rerun.
    Never raises."""
    environ = os.environ if environ is None else environ
    try:
        root = Path(getattr(session, "root"))
        tag = getattr(session, "tag", "boh-1loc") or "boh-1loc"
        if not S.state_path(root).exists():
            return []
        note, state, _ = await asyncio.to_thread(behind_note, root, tag)
        if note is None or state is None:
            return [note] if note else []
        if not A.auto_enabled(environ):
            return [note]
        last = state["reports"][-1] if state["reports"] else {}
        if not (last.get("capabilities", {}).get("delta") and A.incremental_enabled(environ)):
            return [note, "auto-update skipped: the Bookkeeper has no delta mode (or ORCHESTRATOR_BK_INCREMENTAL is "
                          "off), and a full rerun never runs automatically"]
        remaining = stop.remaining() if stop is not None else float(UPDATE_TIMEOUT)
        budget = remaining - AUTO_RESERVE_SECONDS
        if budget < AUTO_MIN_SECONDS:
            return [note, "auto-update skipped: not enough time left in this turn"]
        agent = _agent_call(dispatch)
        if agent is None:
            out = await asyncio.wait_for(asyncio.to_thread(update, root, budget, tag, False, lambda *_: None,
                                                           environ, runner=runner), budget + 2)
        else:
            out = await asyncio.wait_for(_agent_update(root, agent, budget, tag, environ), budget + 2)
        if out.get("status") == "updated":
            return [f"Bookkeeper auto-update: {out['report']} merged; covers through {out['covered_through']}"]
        return [note]
    except asyncio.TimeoutError:
        return ["Bookkeeper auto-update did not finish in time; run `orchestrator bookkeeper-update`"]
    except Exception as exc:                              # a turn never fails because of the Bookkeeper
        return [f"Bookkeeper auto-update failed ({type(exc).__name__}); run `orchestrator bookkeeper-update`"]


async def _agent_update(root, agent, timeout, tag, environ) -> dict:
    """The auto-update through the Bookkeeper agent (on this event loop): delta only, never full."""
    plan = await asyncio.to_thread(plan_update, root, tag, False, environ)
    if plan["status"] != "delta":
        return plan
    payload = A.build_request(plan["as_of"], plan["since"], plan["prior"], request_id=new_request_id(plan["as_of"]),
                              tag=tag, environ=environ)
    path, _ = await arun_agent(root, agent, payload, timeout, getattr(agent, "sender", SENDER))
    merged = await asyncio.to_thread(S.ingest, root, path)
    return {"status": "updated", "report": path.name, **merged}


def _agent_call(dispatch):
    """Agent mode: a PeerDispatch with a Bookkeeper address -> async callable(payload) -> reply dict. It uses the
    dispatch's ask_bookkeeper (the reply is matched by request_id, because the Bookkeeper answers from its Verifier
    handler, outside send_and_receive's session) when there is one, else its _call."""
    address = getattr(dispatch, "bookkeeper", None)
    ask = getattr(dispatch, "ask_bookkeeper", None)
    call = getattr(dispatch, "_call", None)
    if not address or (ask is None and call is None):
        return None

    async def send(payload):
        reply, why = await (ask(payload) if ask is not None else call(address, payload))
        if reply is None:
            raise BookkeeperError(f"the Bookkeeper agent {why}")
        return reply
    send.sender = getattr(dispatch, "me", None) or SENDER
    return send
