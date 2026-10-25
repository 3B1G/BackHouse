"""The owner's "Run the Bookkeeper", from the Chat's card: the orchestrator's own update, run in the background.

`orchestrator bookkeeper-update` first compares the days the Bookkeeper has checked with the newest bank day. When nothing
is new it answers "up to date" at once and spends nothing; otherwise it reruns the full, verified investigation on ASI:One
(up to 20 minutes), the new report is ingested, and the owner reviews its findings and signs again. The website starts
nothing bigger: a first run, or a rerun from scratch, takes up to an hour and stays the owner's command line
(`orchestrator bookkeeper-setup`). One run at a time; what it prints streams to the developer page.
"""
import os
import shutil
import subprocess
import threading
import time

import orchestrator_bridge
import tracelog
from core import LAB, env_value

ROOT = LAB                                       # the nessie_lab whose data/ the Bookkeeper checks
LIMIT = 1200                                     # the orchestrator's own timeout for an update; the process gets a minute more
COVERAGE_SECONDS = 30                            # how old the card's "checked through" may be
_LOCK = threading.Lock()
_JOB: dict = {"state": "idle"}                   # idle, running, done or failed; started_at, finished_at, result, note, last
_COVERAGE: dict = {"at": 0.0, "value": None}


def coverage(fresh=False):
    """How far the Bookkeeper has checked, against the newest bank day (the orchestrator's read-only bookkeeper_status),
    or None when the orchestrator can't say (no bank data yet, say)."""
    if not fresh and time.monotonic() - _COVERAGE["at"] < COVERAGE_SECONDS:
        return _COVERAGE["value"]
    try:
        out = orchestrator_bridge.run("bk-status", "owner", timeout=60)
    except orchestrator_bridge.OrchestratorError:
        out = None
    value = None
    if out and not out.get("error"):
        counts = out.get("findings") if isinstance(out.get("findings"), dict) else {}
        value = {"available": bool(out.get("available")), "covered_through": out.get("covered_through"),
                 "latest_bank_date": out.get("latest_bank_date"), "days_behind": out.get("days_behind"),
                 "open_findings": counts.get("open")}
    _COVERAGE.update(at=time.monotonic(), value=value)
    return value


def status():
    """The card's state: the owner's run, if any, and how far the Bookkeeper has checked (not asked again mid-run)."""
    with _LOCK:
        job = dict(_JOB)
    return {"run": job, "coverage": _COVERAGE["value"] if job["state"] == "running" else coverage()}


def start():
    """Starts the update in the background. Raises RuntimeError while one is running, or before the Bookkeeper has a
    report: its first run is the command line's."""
    with _LOCK:                                  # at once, before asking the orchestrator (a second or two)
        if _JOB.get("state") == "running":
            raise RuntimeError("The Bookkeeper is already running.")
    known = coverage(fresh=True)
    if not (known and known["available"]):
        raise RuntimeError("The Bookkeeper hasn't made a report for this data yet. Its first run takes up to an hour, so it "
                           "starts from the command line: orchestrator bookkeeper-setup.")
    with _LOCK:
        if _JOB.get("state") == "running":
            raise RuntimeError("The Bookkeeper is already running.")
        _JOB.clear()
        _JOB.update(state="running", started_at=time.time(), finished_at=None, result=None, note=None, last=None)
        job = dict(_JOB)
    threading.Thread(target=_run, daemon=True).start()
    return job


def _command():
    return [shutil.which("uv") or "uv", "run", "--quiet", "--project", str(LAB / "orchestrator"), "orchestrator",
            "--root", str(ROOT), "bookkeeper-update"]


def _env():
    env = {k: v for k, v in os.environ.items() if k != "VIRTUAL_ENV"}
    # the Bookkeeper's own package too: it runs as the orchestrator's subprocess (see orchestrator_bridge.SOURCES)
    paths = [*orchestrator_bridge.SOURCES, LAB / "bookkeeper" / "src"]
    env.update(PYTHONUNBUFFERED="1", PYTHONPATH=os.pathsep.join(map(str, paths)))
    if "ASI1_API_KEY" not in env and (key := env_value("ASI1_API_KEY")):
        env["ASI1_API_KEY"] = key                # the Bookkeeper's ASI:One key; never logged
    return env


def _run():
    span = tracelog.Span("Bookkeeper", "Owner ran the Bookkeeper: is there anything new to check?", "owner")
    lines, code = [], None
    try:
        p = subprocess.Popen(_command(), cwd=LAB, env=_env(), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        timer = threading.Timer(LIMIT + 60, p.kill)
        timer.start()
        try:
            for raw in p.stdout:
                line = raw.strip()
                if not line:
                    continue
                lines.append(line[:300])
                tracelog.emit("Bookkeeper", "log", line[:300], "owner")
                with _LOCK:
                    _JOB["last"] = line[:200]
            code = p.wait()
        finally:
            timer.cancel()
    except OSError as e:
        lines.append(f"The Bookkeeper couldn't start: {e}")
    result, note = _outcome(code, lines)
    with _LOCK:
        _JOB.update(state="done" if code == 0 else "failed", finished_at=time.time(), result=result, note=note)
    orchestrator_bridge.forget()                 # the review and the Chat read the new state
    _COVERAGE["at"] = 0.0
    (span.done if code == 0 else span.fail)(note[:200])


def _outcome(code, lines):
    """(result, note): up_to_date, new_report or failed, with one sentence for the owner."""
    last = lines[-1] if lines else ""
    if code == 0 and any(line.startswith("Bookkeeper is up to date") for line in lines):
        return "up_to_date", "Up to date: the Bookkeeper has already checked every bank day, so nothing needed to run."
    if code == 0:
        return "new_report", "Done: the Bookkeeper's new, verified report is in. Review its findings on the dashboard and sign again."
    if last.startswith("bookkeeper-update: "):           # the orchestrator's one-line error
        last = last.split(": ", 1)[1]
    if code is not None and code < 0:
        return "failed", "It took too long, so it was stopped."
    return "failed", last or "The Bookkeeper stopped without saying why."
