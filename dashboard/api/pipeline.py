"""One check of the books, as named steps that stream to the dashboard (Server-Sent Events).

Steps that exist as nessie_lab scripts run as real subprocesses; their numbers are read back from what they wrote, and what
they print streams to the developer page line by line (tracelog). The Bookkeeper step loads the newest saved Bookkeeper
report (data/reports/) until the agent runs inside this step. The check ends with the orchestrator's brief
(orchestrator_bridge.py): Market scout compares prices, then the orchestrator suggests changes for the owner to approve.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from contextlib import closing
from datetime import date

import core
from core import DATA, LAB, TAG
import forecast
import orchestrator_bridge
import tracelog

RUNS = {}
AGENTS = {"bank": "Nessie", "records": "Records", "matcher": "Matcher", "bookkeeper": "Bookkeeper", "forecast": "Forecaster",
          "market": "Market scout", "orchestrator": "Orchestrator"}
MAX_LOG_LINES = 40              # per script call on the developer page; the rest is counted, not shown


class Run:
    def __init__(self, source):
        self.id, self.source, self.t0 = uuid.uuid4().hex[:10], source, time.time()
        self.events, self.done, self.cond = [], False, threading.Condition()
        self.started, self.current = {}, None

    def emit(self, **event):
        with self.cond:
            event["t"] = round(time.time() - self.t0, 2)
            self.events.append(event)
            self.cond.notify_all()

    def finish(self):
        with self.cond:
            self.done = True
            self.cond.notify_all()

    def stream(self):
        """Every event so far, then each new one as it happens; ends when the run does."""
        i = 0
        while True:
            with self.cond:
                if i >= len(self.events) and not self.done:
                    self.cond.wait(timeout=15)
                batch, i, done = self.events[i:], len(self.events), self.done
            for event in batch:
                yield f"data: {json.dumps(event, default=str)}\n\n"
            if done and not batch:
                return
            if not batch:
                yield ": still working\n\n"


def _script(run, step, name, *args):
    """Runs a nessie_lab script. What it prints goes to the developer page as it prints it."""
    agent = AGENTS[step]
    span = tracelog.Span(agent, " ".join(["python", name, *args]), run.id, cwd="nessie_lab")
    p = subprocess.Popen([sys.executable, name, *args], cwd=LAB, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         env={**os.environ, "PYTHONUNBUFFERED": "1"})
    lines = []
    for line in p.stdout:
        line = line.rstrip()
        if not line:
            continue
        lines.append(line)
        if len(lines) <= MAX_LOG_LINES:
            tracelog.emit(agent, "log", line, run.id)
    code = p.wait(timeout=600)
    if len(lines) > MAX_LOG_LINES:
        tracelog.emit(agent, "log", f"... and {len(lines) - MAX_LOG_LINES} more lines", run.id)
    if code != 0:
        span.fail(f"{name} stopped with exit code {code}")
        raise RuntimeError(f"{name} failed: {(lines[-1:] or ['no output'])[0][:200]}")
    span.done(f"{name} finished, {len(lines)} lines of output")
    return "\n".join(lines)


def _day(iso):
    """2027-02-19 -> Feb 19, 2027"""
    return date.fromisoformat(str(iso)[:10]).strftime("%b %-d, %Y") if iso else iso


def _check(run, step, status, text):
    """A check the step made, for the developer page's timeline: pass, warn, fail or pending."""
    tracelog.emit(AGENTS[step], "check", text, run.id, status=status)


def _one(db, sql):
    with closing(sqlite3.connect(db.as_uri() + "?mode=ro", uri=True)) as con:
        return con.execute(sql).fetchone()


def start(source, upload=None):
    run = Run(source)
    RUNS[run.id] = run
    threading.Thread(target=_execute, args=(run, upload), daemon=True).start()
    return run


def _step(run, step, state, text, **data):
    run.emit(type="step", step=step, state=state, text=text, **data)
    agent = AGENTS[step]
    if state == "working":
        run.started[step], run.current = time.perf_counter(), step
        tracelog.emit(agent, "start", text, run.id, step=step)
    else:
        ms = round((time.perf_counter() - run.started.get(step, time.perf_counter())) * 1000)
        tracelog.emit(agent, state, text, run.id, ms, step=step, **{k: v for k, v in data.items() if k != "forecast"})


def _orchestrate(run):
    """Market scout compares the menu's prices, then the orchestrator suggests changes: its brief, in its own environment.
    The check's numbers stand whatever happens here, so a failure shows on these two steps and the check still finishes."""
    _step(run, "market", "working", "Comparing your prices with nearby restaurants' menus")
    span = tracelog.Span("Orchestrator", "Brief: what is going on, then what to do about it", run.id)
    scanned = []

    def on_event(e):                             # the scan's result ends Market scout's step and starts the orchestrator's
        if e.get("kind") != "result" or e.get("tool") != "scan_market" or scanned:
            return
        scanned.append(e)
        counts = e.get("counts") or {}
        if e.get("error") or not counts:
            _check(run, "market", "fail", f"The price scan stopped: {e.get('error') or 'no result'}")
            _step(run, "market", "waiting", "Couldn't compare prices this time")
        else:
            below, at, above, thin = (counts[k] for k in ("below_market", "at_par", "above_market", "insufficient_data"))
            _check(run, "market", "pass", f"Compared {below + at + above} dishes with nearby restaurants' own menus")
            _check(run, "market", "warn" if below else "pass", f"{below} below market, {at} at market, {above} above")
            if thin:
                _check(run, "market", "pending", f"{thin} dishes have too few nearby prices to compare")
            _step(run, "market", "done", f"{below} dishes are priced below market, {above} above", counts=counts)
        _step(run, "orchestrator", "working", "Looking for changes that help your cash")

    try:
        plan = orchestrator_bridge.plan(orchestrator_bridge.run("brief", run.id, on_event=on_event))
    except Exception as e:                       # OrchestratorError, or uv itself missing
        span.fail(f"Brief stopped: {str(e)[:200]}")
        if not scanned:
            _step(run, "market", "waiting", "Market scout didn't run")
        _step(run, "orchestrator", "waiting", f"The orchestrator couldn't run: {str(e)[:200]}")
        return
    if not scanned:
        _step(run, "market", "waiting", "Market scout didn't run")
        _step(run, "orchestrator", "working", "Looking for changes that help your cash")
    if plan["market"] and plan["market"]["offline"]:
        _check(run, "market", "pass", "Read the saved copies of their menu pages, so nothing was fetched")
    if plan["cash_cents"] is not None:
        _check(run, "orchestrator", "pass", f"${plan['cash_cents'] / 100:,.2f} in the bank on {_day(plan['as_of'])}")
    if plan["bills"]:
        b = plan["bills"]
        _check(run, "orchestrator", "pass", f"{b['count']} bills due by {_day(b['before'])}, ${b['total_cents'] / 100:,.2f} in all")
    fixes = plan["fixes"]
    for fx in fixes:
        _check(run, "orchestrator", "pass", f"{fx['title']}: cash lasts {fx['days_later']:g} days longer on average")
    if fixes:
        _check(run, "orchestrator", "pass", "Changes dry-ran each one and saved it for your approval; nothing was applied")
        _check(run, "orchestrator", "pending", "You approve each change at your terminal, with the owner passphrase")
    else:
        _check(run, "orchestrator", "pass", "No change the forecast supports right now")
    run.emit(type="plan", plan=plan)
    span.done(f"Suggested {len(fixes)} change{'' if len(fixes) == 1 else 's'}")
    _step(run, "orchestrator", "done", f"Suggested {len(fixes)} change{'' if len(fixes) == 1 else 's'} that help your cash"
          if fixes else "No change helps your cash right now", fixes=len(fixes))


def _execute(run, upload):
    check = tracelog.Span("Pipeline", f"Check started from {'your upload' if upload else 'the sample books'}", run.id)
    try:
        name, street = _one(DATA / "books.db", "select name, street_name from locations limit 1")
        run.emit(type="run", restaurant=f"{name.rsplit(' ', 1)[0]}, {street.replace(' (SIMULATED)', '')}")
        _step(run, "bank", "working", "Connecting to Nessie")
        out = _script(run, "bank", "nessie_sync.py", "--tag", TAG)
        if "round trip OK" not in out:
            _check(run, "bank", "fail", "Round trip failed: the local copy doesn't match Nessie")
            raise RuntimeError("Nessie's copy didn't match the bank feed")
        _check(run, "bank", "pass", "Round trip OK: the local copy matches what Nessie holds")
        n, first, last = _one(DATA / f"nessie_sync_{TAG}.db", "select count(*), min(date), max(date) from bank_tx")
        _check(run, "bank", "pass", f"{n:,} transactions, {_day(first)} to {_day(last)}")
        _step(run, "bank", "done", f"Read {n:,} bank transactions from Nessie", count=n, first=first, last=last)

        _step(run, "records", "working", "Reading your records" + (f" from {upload['name']}" if upload else ""))
        with forecast.LOCK:
            _script(run, "records", "build_book.py")
            _script(run, "records", "features.py")
        costs, pending, pending_cents = _one(DATA / "books.db", "select count(*), sum(status = 'pending'), "
                                             "sum(case when status = 'pending' then amount_cents else 0 end) from costs")
        _check(run, "records", "pass", f"The Book holds {costs:,} costs")
        _check(run, "records", "pass", f"{pending} bills still unpaid (${(pending_cents or 0) / 100:,.0f}), and the forecast counts them")
        _step(run, "records", "done", f"Read {costs:,} costs; {pending} bills are still unpaid", costs=costs, pending=pending,
              pending_cents=pending_cents)

        _step(run, "matcher", "working", "Matching bank transactions to your records")
        out = _script(run, "matcher", "baseline_reconciler.py")
        m = re.search(r"bank rows (\d+), matched (\d+); flags (\d+): (\{.*\})", out)
        bank_rows, matched, flags = (int(m.group(i)) for i in (1, 2, 3))
        kinds = json.loads(m.group(4).replace("'", '"'))
        exact = bank_rows - kinds.get("variance", 0) - kinds.get("unexpected", 0)
        _check(run, "matcher", "pass", f"{exact:,} of {bank_rows:,} bank transactions match your records")
        _check(run, "matcher", "warn" if flags else "pass",
               f"{flags} need a closer look" + (": " + ", ".join(f"{v} {k.replace('_', ' ')}" for k, v in kinds.items()) if kinds else ""))
        _step(run, "matcher", "done", f"{exact:,} transactions match your records; {flags} need a closer look",
              exact=exact, flags=flags, kinds=kinds)

        _step(run, "bookkeeper", "working", "Looking into what didn't match")
        reports = sorted((DATA / "reports").glob("*.json"), key=lambda p: p.stat().st_mtime) if (DATA / "reports").exists() else []
        passed = []
        if not reports:
            tracelog.Span("Bookkeeper", "Looked for a saved Bookkeeper report in data/reports", run.id).done("None yet")
            _check(run, "bookkeeper", "pending", "No Bookkeeper report for this data yet, so no findings to check")
            _step(run, "bookkeeper", "waiting", "The Bookkeeper hasn't checked this data yet")
        else:
            import contracts
            import bookkeeper_tools
            span = tracelog.Span("Bookkeeper", f"contracts.from_bk_report({reports[-1].name})", run.id)
            findings, adjustments, rejected = contracts.from_bk_report(reports[-1], bookkeeper_tools.bank_id_map())
            span.done(f"{len(findings)} findings, {len(adjustments)} adjustments, {len(rejected)} rejected")
            _check(run, "bookkeeper", "pass", f"{len(findings)} findings passed contracts.py")
            if rejected:
                _check(run, "bookkeeper", "warn", f"{len(rejected)} findings rejected by contracts.py")
            # Only fixes the verifier passed reach the Forecaster (contracts.passed_adjustments). checks.py, the verifier,
            # isn't in the repo yet, so no finding counts as verified and nothing passes until it is.
            passed = contracts.passed_adjustments(adjustments, [])
            _check(run, "bookkeeper", "pass" if passed else "pending",
                   f"{len(passed)} of {len(adjustments)} fixes passed the verifier and go to the Forecaster" if passed
                   else f"The verifier isn't built yet, so none of its {len(adjustments)} fixes reach the forecast")
            for f in findings:
                run.emit(type="finding", finding=f.model_dump(mode="json"))
            _step(run, "bookkeeper", "done", f"Confirmed {len(findings)} problems", findings=len(findings),
                  rejected=len(rejected), report=reports[-1].name)

        forecast.use_adjustments(passed)       # every forecast from here on, what-ifs and Chat included, uses them
        _step(run, "forecast", "working", "Running 1,000 futures" + (f" with {len(passed)} verified fixes" if passed else ""))
        base = forecast.run(group=run.id)
        _check(run, "forecast", "pass" if passed else "pending",
               f"Counts {len(passed)} verified Bookkeeper fixes" if passed else "No verified Bookkeeper fixes yet, so it uses your records as they are")
        _check(run, "forecast", "pass", f"1,000 futures in {base['runtime_ms']} ms")
        pays = base["paydays"]
        risky = next((i for i, p in enumerate(pays) if p["clears"] < 0.95), len(pays))
        if risky:
            _check(run, "forecast", "pass", f"Payroll safe through {_day(pays[risky - 1]['date'])}")
        likely = base["runout"]["likely"]
        _check(run, "forecast", "warn" if likely else "pass", f"Likely out of cash {_day(likely)}" if likely else "Cash lasts beyond two years")
        _step(run, "forecast", "done", f"Ran 1,000 futures in {base['runtime_ms'] / 1000:.1f} s", forecast=base)
        _orchestrate(run)
        run.emit(type="done")
        check.done(f"Check finished in {time.time() - run.t0:.1f} s")
    except Exception as e:                       # the dashboard shows which step stopped and why
        run.emit(type="error", text=str(e)[:300])
        tracelog.emit(AGENTS.get(run.current, "Pipeline"), "error", str(e)[:300], run.id)
        check.fail("Check stopped")
    finally:
        run.finish()
