"""The orchestrator's merged view of every Bookkeeper report it has ingested: data/orchestrator/bookkeeper_state.json.

One long FULL backfill sets the baseline; later reports (full reruns today, delta reports once the Bookkeeper ships
them) are merged into it, so the forecast keeps the earlier context and an update only has to cover the newest days.

    {version, baseline: {report_id, filename, as_of, window},
     reports: [{report_id, filename, sha256, as_of, mode, window, capabilities, ingested_at,
                findings_new, findings_closed, findings_not_rereported}],
     covered_through: date,
     findings: {key: {finding, status, first_seen, last_seen, last_report_id, rereported, note?}}}

Finding ids are renumbered on every run, so findings are matched by bk_adapter.finding_key (and, failing that, by
root cause plus overlapping bank ids). Statuses: open (used by the forecast), not_rereported (a later FULL run
didn't list it: kept, never used), resolved / superseded (closed by the Bookkeeper: kept, never used).

Writes are atomic (a temp file, then os.replace) under a lock file; the previous good state is kept as .bak, and a
corrupt state file is recovered from it or refused with a clear message, never silently replaced."""
import hashlib
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from datetime import date, datetime, timezone
from pathlib import Path

from . import bk_adapter as A

VERSION = 1
STATE_NAME = "bookkeeper_state.json"
REPORT_NAME = re.compile(r"^R-[A-Za-z0-9._-]+\.json$")
LOCK_STALE_SECONDS = 300
LOCK_WAIT_SECONDS = 10
OPEN, NOT_REREPORTED = "open", "not_rereported"
MAX_REPORT_BYTES = 20_000_000


class StateError(RuntimeError):
    """The state file or a report can't be used; the message says why and what to do."""


def folder(root) -> Path:
    return Path(root) / "data" / "orchestrator"


def state_path(root) -> Path:
    return folder(root) / STATE_NAME


def _today_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- lock and atomic writes
@contextmanager
def lock(root, name: str = "bookkeeper_state.lock", wait: float = LOCK_WAIT_SECONDS, stale: float = LOCK_STALE_SECONDS):
    """An exclusive lock file (O_CREAT|O_EXCL). A lock older than `stale` seconds is taken over."""
    path = folder(root) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    end = time.monotonic() + wait
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > stale:
                    path.unlink(missing_ok=True)
                    continue
            except OSError:
                pass
            if time.monotonic() >= end:
                raise StateError(f"data/orchestrator/{name} is held by another run; try again when it finishes "
                                 f"(or delete it if no orchestrator is running)") from None
            time.sleep(0.05)
    try:
        os.write(fd, f"{os.getpid()}\n".encode())
        os.close(fd)
        yield path
    finally:
        try:
            path.unlink()
        except OSError:
            pass


def _atomic_write(path: Path, text: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
    try:
        with open(tmp, "x", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def _check_state(state) -> dict:
    if not isinstance(state, dict) or state.get("version") != VERSION:
        raise ValueError("not a version-1 state")
    if not isinstance(state.get("findings"), dict) or not isinstance(state.get("reports"), list):
        raise ValueError("findings/reports missing")
    if state.get("covered_through") is not None:
        date.fromisoformat(state["covered_through"])
    for v in state["findings"].values():
        if not isinstance(v, dict) or not isinstance(v.get("finding"), dict) or "status" not in v:
            raise ValueError("a finding entry is malformed")
    return state


def _read(path: Path) -> dict:
    return _check_state(json.loads(path.read_text(encoding="utf-8")))


def load(root, recover: bool = True) -> dict | None:
    """The state, or None when there is none yet. A corrupt file is recovered from .bak when that is good (the
    corrupt file is kept as .corrupt-<time>), else StateError."""
    path = state_path(root)
    if not path.exists():
        return None
    try:
        return _read(path)
    except (OSError, ValueError) as exc:
        bak = path.with_name(path.name + ".bak")
        if recover and bak.is_file():
            try:
                state = _read(bak)
            except (OSError, ValueError):
                state = None
            if state is not None:
                path.replace(path.with_name(f"{path.name}.corrupt-{int(time.time())}"))
                _atomic_write(path, json.dumps(state, indent=2))
                state.setdefault("notes", []).append("the state file was corrupt; recovered the previous good copy")
                return state
        raise StateError(f"data/orchestrator/{STATE_NAME} is corrupt ({type(exc).__name__}) and there is no good "
                         "backup: move it aside and run `orchestrator bookkeeper-setup --force`, or re-ingest the "
                         "reports with `orchestrator bookkeeper-ingest`") from None


def save(root, state: dict):
    """Atomic: the previous good state goes to .bak first, then the new one replaces the file. Call under lock()."""
    _check_state(state)
    path = state_path(root)
    if path.exists():
        try:
            _read(path)
            _atomic_write(path.with_name(path.name + ".bak"), path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass                                          # never back up a corrupt file over a good backup
    state = {k: v for k, v in state.items() if k != "notes"}
    _atomic_write(path, json.dumps(state, indent=2, sort_keys=False))


def empty() -> dict:
    return {"version": VERSION, "baseline": None, "reports": [], "covered_through": None, "findings": {}}


# ---------------------------------------------------------------- reports
def report_file(root, report_path) -> Path:
    """A report path -> the file inside <root>/data/reports, named R-*.json; anything else is refused."""
    reports = (Path(root) / "data" / "reports").resolve()
    raw = str(report_path).replace("\\", "/")
    p = Path(raw)
    if not p.is_absolute():
        parts = p.parts
        p = (reports / p) if len(parts) == 1 else (Path(root) / p)
    name = Path(raw).name
    if not REPORT_NAME.fullmatch(name) or ".." in Path(raw).parts:
        raise StateError("only Bookkeeper reports named R-*.json in data/reports can be ingested")
    try:
        resolved = p.resolve()
    except OSError:
        raise StateError("that report path can't be resolved") from None
    if resolved.parent != reports or p.is_symlink():
        raise StateError("only Bookkeeper reports named R-*.json in data/reports can be ingested")
    if not resolved.is_file():
        raise StateError(f"data/reports/{name} does not exist")
    return resolved


def read_report(path: Path) -> tuple[dict, str]:
    if path.stat().st_size > MAX_REPORT_BYTES:
        raise StateError("the report is too large to be a Bookkeeper report")
    raw = path.read_bytes()
    try:
        report = json.loads(raw.decode("utf-8-sig"))
    except ValueError:
        raise StateError(f"{path.name} is not JSON") from None
    problems = A.validate_report(report)
    if problems:
        raise StateError(f"{path.name} is not a schema-1.1 Bookkeeper report: {'; '.join(problems[:5])}")
    return report, hashlib.sha256(raw).hexdigest()


def _match(state: dict, f: dict, taken: set) -> str:
    """The state key for finding f: its key when known, else a known finding with the same root cause and
    overlapping bank ids (the secondary match), else its own new key."""
    key = A.finding_key(f)
    if key in state["findings"] or A.FINDING_KEY_FIELD in f:
        return key
    for k, entry in state["findings"].items():
        if k not in taken and A.same_finding(entry["finding"], f):
            return k
    return key


def ingest(root, report_path, baseline: bool | None = None) -> dict:
    """Merge one report into the state (see the module docstring). Idempotent: a report already ingested is a no-op.
    baseline=True makes a FULL report the new baseline (bookkeeper-setup --force); by default the first full report
    becomes the baseline. -> {"noop", "report_id", "mode", "new", "updated", "closed", "not_rereported",
    "covered_through"}"""
    path = report_file(root, report_path)
    report, sha = read_report(path)
    rid = str(report["report_id"])
    delta = A.is_delta(report)
    as_of = A.report_as_of(report)
    window = A.report_window(report)
    end = A.covered_end(report).isoformat()
    with lock(root):
        state = load(root) or empty()
        for r in state["reports"]:
            if r["filename"] == path.name or r["report_id"] == rid:
                if r.get("sha256") not in (None, sha):
                    raise StateError(f"{path.name} was ingested before with different content; reports are immutable")
                return {"noop": True, "report_id": rid, "mode": r["mode"], "covered_through": state["covered_through"],
                        "why": "already ingested"}
        covered = state["covered_through"]
        if delta:
            if not state["baseline"]:
                raise StateError("a delta report needs a baseline: run `orchestrator bookkeeper-setup` first")
            start = window.get("since") or window["start"]
            if date.fromisoformat(start).toordinal() > date.fromisoformat(covered).toordinal() + 1:
                raise StateError(f"{path.name} starts on {start}, but the state covers only through {covered}: "
                                 "the days in between would be missing")
        elif covered and end < covered:
            raise StateError(f"{path.name} covers through {end}, older than the state ({covered}); not ingested")

        seen, new, updated, closed, dropped, taken = set(), 0, 0, 0, 0, set()
        for f in report["findings"]:
            key = _match(state, f, taken)
            taken.add(key)
            seen.add(key)
            st = A.status(f)
            entry = state["findings"].get(key)
            if entry is None:
                new += 1 if st == OPEN else 0
                closed += 1 if st != OPEN else 0
                state["findings"][key] = {"finding": f, "status": st, "first_seen": as_of.isoformat(),
                                          "last_seen": as_of.isoformat(), "last_report_id": rid, "rereported": True}
                continue
            if st != OPEN and entry["status"] not in A.CLOSED_STATUSES:
                closed += 1
            else:
                updated += 1
            entry.update(finding=f, status=st, last_seen=as_of.isoformat(), last_report_id=rid, rereported=True)
            entry.pop("note", None)
        if not delta:                                     # a full snapshot: what it doesn't list is no longer open
            for key, entry in state["findings"].items():
                if key not in seen and entry["status"] == OPEN:
                    entry.update(status=NOT_REREPORTED, rereported=False,
                                 note=f"not in {rid} (a full rerun through {end}); kept for the record, not used "
                                      "in the forecast until a report lists it again")
                    dropped += 1
        if not delta and (baseline or (baseline is None and not state["baseline"])):
            state["baseline"] = {"report_id": rid, "filename": path.name, "as_of": as_of.isoformat(),
                                 "window": window}
        state["covered_through"] = max(filter(None, [covered, end]))
        state["reports"].append({"report_id": rid, "filename": path.name, "sha256": sha, "as_of": as_of.isoformat(),
                                 "mode": "delta" if delta else "full", "window": window,
                                 "capabilities": A.capabilities(report), "ingested_at": _today_utc(),
                                 "findings_new": new, "findings_closed": closed,
                                 "findings_not_rereported": dropped})
        save(root, state)
    return {"noop": False, "report_id": rid, "mode": "delta" if delta else "full", "new": new, "updated": updated,
            "closed": closed, "not_rereported": dropped, "covered_through": state["covered_through"]}


# ---------------------------------------------------------------- what the rest of the orchestrator reads
def counts(state: dict) -> dict:
    out = {"open": 0, NOT_REREPORTED: 0, "resolved": 0, "superseded": 0}
    for e in state["findings"].values():
        out[e["status"]] = out.get(e["status"], 0) + 1
    return out


def open_entries(state: dict) -> list[tuple[str, dict]]:
    return sorted(((k, e) for k, e in state["findings"].items() if e["status"] == OPEN),
                  key=lambda ke: (ke[1]["first_seen"], ke[0]))


def short_ids(keys) -> dict[str, str]:
    """{key: id}: key[:10], longer where two open keys share a prefix, so ids never collide."""
    keys, n = list(keys), 10
    while len({k[:n] for k in keys}) < len(keys) and n < 64:
        n += 2
    return {k: k[:n] for k in keys}


def open_findings_report(state: dict) -> dict:
    """A synthetic schema-1.1-shaped report of the OPEN findings only, ids namespaced as key[:10], summary totals
    recomputed. This is what contracts.from_bk_report consumes."""
    entries = open_entries(state)
    ids = short_ids(k for k, _ in entries)
    findings = [{**A.clean_finding(e["finding"]), "id": ids[k]} for k, e in entries]
    base = state.get("baseline") or {}
    start = (base.get("window") or {}).get("start") or state["covered_through"]
    digest = hashlib.sha256("|".join(r["report_id"] for r in state["reports"]).encode()).hexdigest()[:12]
    return A.merged_report(findings, report_id=f"R-merged-{digest}", as_of=state["covered_through"],
                           window={"start": start, "end": state["covered_through"]},
                           limitations=[f"merged from {len(state['reports'])} Bookkeeper report(s); "
                                        f"{counts(state)[NOT_REREPORTED]} finding(s) not re-reported are left out"])


def days_behind(state: dict, latest: date | None) -> int | None:
    if latest is None or not state.get("covered_through"):
        return None
    return max(0, (latest - date.fromisoformat(state["covered_through"])).days)


def status_view(state: dict | None, latest: date | None) -> dict:
    """bookkeeper-status and the bookkeeper_status tool."""
    if state is None:
        return {"available": False, "latest_bank_date": latest.isoformat() if latest else None,
                "why": "no Bookkeeper baseline yet: the owner runs `orchestrator bookkeeper-setup` once"}
    last = state["reports"][-1] if state["reports"] else None
    behind = days_behind(state, latest)
    return {"available": True, "baseline": state["baseline"], "covered_through": state["covered_through"],
            "latest_bank_date": latest.isoformat() if latest else None, "days_behind": behind,
            "up_to_date": behind == 0, "findings": counts(state), "reports_ingested": len(state["reports"]),
            "last_run": None if last is None else {k: last[k] for k in ("report_id", "filename", "as_of", "mode",
                                                                        "ingested_at", "findings_new",
                                                                        "findings_closed", "findings_not_rereported")},
            "delta_capable": bool(last and last.get("capabilities", {}).get("delta")),
            "incremental_requests": A.incremental_enabled(),
            "notes": list(state.get("notes") or [])}
