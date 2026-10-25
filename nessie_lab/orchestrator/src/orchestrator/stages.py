"""The order of work (ORCHESTRATOR.md): Audit -> Review -> Commit -> Forecast -> Suggest, enforced in code.

The stage is DERIVED from disk on every call and never read from storage: current(session) rebuilds it from the
Bookkeeper's merged state (bk_state), the data fingerprint taken when that state was ingested, the owner's proposed
decisions (review.py), CH's signed approval of them, and the recorded base forecast.

    Audit     always
    Review    a merged Bookkeeper state whose open findings all went through the Bookkeeper's Verifier, recorded at
              ingest with the report's sources.bank_rows equal to the bank rows through its as_of
    Commit    every open finding has a proposed decision, and `orchestrator review prepare` wrote the decision file
    Forecast  CH holds the owner's passphrase-signed approval of exactly the rebuilt review suggestion, it is the
              latest decision on it, it is at most ORCHESTRATOR_REVIEW_TTL_H hours old (default 24), and the data
              fingerprint has not moved since the audit
    Suggest   a base forecast was recorded under the same decisions and fingerprint

Re-locks follow from the same derivation: a new report gives a new review_id (Review, matching decisions carried);
any data change (bank rows up to as_of, later rows or bills, books.db) gives Audit; a proposal edited after prepare
changes the digest (Review); an expired, rejected or forged approval gives Commit. Text inside findings is never
read as an instruction: only code-written fields decide the stage."""
import hashlib
import json
import logging
import os
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("orchestrator.stages")
AUDIT, REVIEW, COMMIT, FORECAST, SUGGEST = "Audit", "Review", "Commit", "Forecast", "Suggest"
ORDER = (AUDIT, REVIEW, COMMIT, FORECAST, SUGGEST)
TTL_ENV = "ORCHESTRATOR_REVIEW_TTL_H"
FP_PARTS = {"audited": "bank rows up to the audit date", "tail": "later bank rows or the bank's bills",
            "books": "books.db"}
UPDATE_HINT = "the owner runs `orchestrator bookkeeper-setup` the first time, then `orchestrator bookkeeper-update`"
_SOURCES: dict = {}
_FPS: dict = {}


class StageLocked(ValueError):
    """The thing asked for belongs to a later stage; the message says which stage this is and why."""


@dataclass(frozen=True)
class StageView:
    stage: str
    why: str
    review_id: str | None = None
    as_of: str | None = None
    fingerprint: dict | None = None
    digest: str | None = None
    approve_command: str | None = None
    report: str | None = None
    open: tuple = ()                                      # ((finding key, finding id), ...) of the open findings
    proposals: dict | None = field(default=None, compare=False)    # key -> FindingDecision dict (valid ones only)
    suggestion_file: str | None = None

    def at_least(self, stage: str) -> bool:
        return ORDER.index(self.stage) >= ORDER.index(stage)

    def public(self) -> dict:
        """What tools, the brief and the API show: no fingerprint hashes, no internal keys."""
        return {"stage": self.stage, "why": self.why, "review_id": self.review_id, "as_of": self.as_of,
                "report": self.report, "open_findings": len(self.open),
                "decided": len(self.proposals or {}), "digest": self.digest[:16] if self.digest else None,
                "approve_command": self.approve_command, "suggestion_file": self.suggestion_file,
                "order": list(ORDER)}


def ttl_hours() -> float:
    try:
        value = float(os.getenv(TTL_ENV, "") or 24)
    except ValueError:
        value = 24.0
    return value if 0 < value <= 24 * 30 else 24.0


# ---------------------------------------------------------------- what the audit covered
def _stamp(path: Path):
    out = []
    for p in (path, path.with_name(path.name + "-wal")):
        try:
            st = p.stat()
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    return tuple(out)


def source(root) -> dict | None:
    """The merged Bookkeeper state as one review: {review_id, label, as_of, raw, verified, keys, ids, last} or None
    when there is no state yet. Raises bk_state.StateError for an unreadable state."""
    from . import bk_state, checks_bk

    path = bk_state.state_path(root)
    key = (str(path), _stamp(path))
    if key in _SOURCES:
        return _SOURCES[key]
    state = bk_state.load(root, recover=False)
    out = None
    if state is not None and state.get("covered_through") and state.get("reports"):
        entries = bk_state.open_entries(state)
        raw = bk_state.open_findings_report(state)
        verified = bk_state.open_verification(state)
        last = state["reports"][-1]
        ok = checks_bk.summary(raw, verified)["verified"] or (not entries and bool(last.get("verified")))
        out = {"review_id": hashlib.sha256("|".join(str(r.get("sha256") or r.get("report_id"))
                                                    for r in state["reports"]).encode()).hexdigest(),
               "label": raw["report_id"], "as_of": state["covered_through"], "raw": raw, "verified": ok,
               "verified_map": verified, "keys": {k: e["finding"] for k, e in entries},
               "ids": bk_state.short_ids(k for k, _ in entries), "last": last}
    if len(_SOURCES) > 32:
        _SOURCES.clear()
    _SOURCES[key] = out
    return out


@contextmanager
def _ro(path: Path):
    """A read-only connection that leaves nothing next to the file (a WAL database is read from a quiet copy)."""
    from changes.executor import quiet_path

    with quiet_path(path) as p:
        with closing(sqlite3.connect(Path(p).resolve().as_uri() + "?mode=ro", uri=True)) as con:
            yield con


def _sha_rows(con, sql: str, params=()) -> str:
    h = hashlib.sha256()
    for row in con.execute(sql, params):
        h.update((json.dumps(list(row), default=str) + "\n").encode())
    return h.hexdigest()


def bank_db(root, tag: str) -> Path:
    return Path(root) / "data" / f"nessie_sync_{tag}.db"


def bank_count(root, tag: str, as_of: str) -> int | None:
    db = bank_db(root, tag)
    if not db.is_file():
        return None
    with _ro(db) as con:
        return int(con.execute("SELECT count(*) FROM bank_tx WHERE substr(date,1,10) <= ?", (as_of,)).fetchone()[0])


def fingerprint(root, tag: str, as_of: str, fresh: bool = False) -> dict:
    """{"audited", "tail", "books"}: sha256 of the bank rows up to as_of; of the count and latest date of later rows
    plus every bank bill; of every table in books.db. Both databases are opened read-only. Cached on each file's
    (mtime, size) and its -wal's; fresh=True recomputes (right before a forecast)."""
    bank, books = bank_db(root, tag), Path(root) / "data" / "books.db"
    key = (str(bank), str(books), as_of, _stamp(bank), _stamp(books))
    if not fresh and key in _FPS:
        return dict(_FPS[key])
    out = {"audited": "missing", "tail": "missing", "books": "missing"}
    if bank.is_file():
        with _ro(bank) as con:
            out["audited"] = _sha_rows(con, "SELECT bank_id,nessie_id,account,nessie_type,direction,substr(date,1,10),"
                                            "amount,counterparty,memo,status FROM bank_tx WHERE substr(date,1,10) <= ? "
                                            "ORDER BY bank_id,nessie_id", (as_of,))
            h = hashlib.sha256()
            h.update(_sha_rows(con, "SELECT count(*),max(date) FROM bank_tx WHERE substr(date,1,10) > ?",
                               (as_of,)).encode())
            h.update(_sha_rows(con, "SELECT * FROM bank_bills ORDER BY rowid").encode())
            out["tail"] = h.hexdigest()
    if books.is_file():
        with _ro(books) as con:
            h = hashlib.sha256()
            for (name,) in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE "
                                       "'sqlite_%' ORDER BY name").fetchall():
                h.update(f"#{name}\n".encode())
                h.update(_sha_rows(con, f'SELECT * FROM "{name}" ORDER BY rowid').encode())
            out["books"] = h.hexdigest()
    if len(_FPS) > 32:
        _FPS.clear()
    _FPS[key] = dict(out)
    return out


def fp_id(fp: dict | None) -> str | None:
    if not fp:
        return None
    return hashlib.sha256("|".join(str(fp.get(k)) for k in FP_PARTS).encode()).hexdigest()[:16]


def record_audit(root, tag: str = "boh-1loc") -> dict:
    """Called by bk_state.ingest: record what this audit covered (the review_id, the report's bank row count and
    the bank's, the data fingerprint) in data/orchestrator/review_state.json. A review_id already recorded is never
    re-recorded, so re-ingesting can't launder a data change. Decisions on the previous review whose finding key AND
    finding_sha match are carried as proposals (proposed_by="carried")."""
    from . import review

    _SOURCES.clear()
    src = source(root)
    if src is None:
        return {}
    as_of = src["as_of"]
    last_as_of = str(src["last"].get("as_of") or as_of)
    seen = bank_count(root, tag, last_as_of)
    fp = fingerprint(root, tag, as_of, fresh=True)
    with review.locked(root):
        st = review.load(root)
        if st.get("review_id") == src["review_id"] and st.get("fingerprint"):
            return st
        before = {**(st.get("carried") or {}), **(st.get("proposals") or {})}
        findings = {k: {"id": src["ids"][k], "finding_sha": review.finding_sha(f), "verdict": f.get("verdict"),
                        "verifier": (src["verified_map"].get(src["ids"][k]) or {}).get("status")}
                    for k, f in src["keys"].items()}
        carried = {}
        for k, d in before.items():
            if k in findings and isinstance(d, dict) and d.get("finding_sha") == findings[k]["finding_sha"]:
                carried[k] = {**d, "review_id": src["review_id"], "finding_id": findings[k]["id"],
                              "proposed_by": "carried"}
        new = {"version": review.VERSION, "review_id": src["review_id"],
               "report": {"label": src["label"], "sha256": src["review_id"], "as_of": as_of,
                          "bank_rows": src["last"].get("bank_rows"), "bank_rows_seen": seen,
                          "bank_rows_as_of": last_as_of, "tag": tag},
               "fingerprint": {**fp, "at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
               "findings": findings, "proposals": dict(carried), "prepared": None, "base_forecast": None,
               "carried": carried}
        review.save(root, new)
        return new


# ---------------------------------------------------------------- the stage
def _reports_waiting(root) -> list[str]:
    folder = Path(root) / "data" / "reports"
    try:
        return sorted(p.name for p in folder.glob("R-*.json") if p.is_file())
    except OSError:
        return []


def current(session) -> StageView:
    """The stage right now, from disk. Never raises: anything unexpected is the Audit stage."""
    try:
        return _current(session)
    except Exception as exc:                              # a broken input never opens a later stage
        LOG.warning("stage check failed (%s)", type(exc).__name__, exc_info=True)
        return StageView(AUDIT, f"the stage could not be checked ({type(exc).__name__}); {UPDATE_HINT}")


def _current(session) -> StageView:
    from . import bk_state, review

    root, tag = Path(session.root), getattr(session, "tag", "boh-1loc")
    try:
        src = source(root)
    except bk_state.StateError:
        return StageView(AUDIT, "the Bookkeeper state file can't be read: run `orchestrator bookkeeper-status`")
    if src is None:
        waiting = _reports_waiting(root)
        if waiting:
            return StageView(AUDIT, f"a Bookkeeper report is in data/reports but not ingested: the owner runs "
                                    f"`orchestrator bookkeeper-ingest data/reports/{waiting[-1]}`")
        return StageView(AUDIT, f"no Bookkeeper report yet: {UPDATE_HINT}")
    base = {"review_id": src["review_id"], "as_of": src["as_of"], "report": src["label"]}
    if not src["verified"]:
        return StageView(AUDIT, f"the open findings did not all go through the Bookkeeper's Verifier: {UPDATE_HINT}",
                         **base)
    st = review.load(root)
    if st.get("review_id") != src["review_id"] or not st.get("fingerprint"):
        last = src["last"].get("filename") or "R-....json"
        return StageView(AUDIT, "this Bookkeeper state was not recorded at ingest: the owner runs `orchestrator "
                                f"bookkeeper-ingest data/reports/{last}`", **base)
    rep = st.get("report") or {}
    if rep.get("bank_rows") is None or rep.get("bank_rows") != rep.get("bank_rows_seen"):
        return StageView(AUDIT, f"the report counted {rep.get('bank_rows')} bank rows through "
                                f"{rep.get('bank_rows_as_of')}, the bank had {rep.get('bank_rows_seen')}: {UPDATE_HINT}",
                         **base)
    fp = fingerprint(root, tag, src["as_of"])
    changed = [label for k, label in FP_PARTS.items() if fp.get(k) != st["fingerprint"].get(k)]
    if changed:
        return StageView(AUDIT, f"the data changed since the audit ({', '.join(changed)}): {UPDATE_HINT}", **base)
    opened = tuple((k, src["ids"][k]) for k in src["keys"])
    shas = {k: review.finding_sha(f) for k, f in src["keys"].items()}
    proposals = {k: d for k, d in (st.get("proposals") or {}).items()
                 if k in shas and isinstance(d, dict) and d.get("finding_sha") == shas[k]
                 and d.get("review_id") == src["review_id"] and d.get("decision") in review.DECISIONS}
    base.update(fingerprint=fp, open=opened, proposals=proposals)
    missing = [fid for k, fid in opened if k not in proposals]
    if missing:
        return StageView(REVIEW, f"{len(opened) - len(missing)} of {len(opened)} open finding(s) have a proposed "
                                 f"decision; waiting: {', '.join(missing[:12])}", **base)
    digest = review.digest(src["review_id"], proposals)
    prep = st.get("prepared") or {}
    if prep.get("digest") != digest:
        why = ("a decision changed after it was prepared" if prep.get("digest") else
               "every open finding has a proposed decision")
        return StageView(REVIEW, f"{why}: the owner runs `orchestrator review prepare`", digest=digest, **base)
    base.update(digest=digest, approve_command=review.approve_command(session, prep.get("suggestion_file")),
                suggestion_file=prep.get("suggestion_file"))
    s = review.suggestion_for(src, proposals, digest, fp)
    why = review.approval_problem(session, s)
    if why:
        return StageView(COMMIT, f"{why}; the owner commits the review with: {base['approve_command']}", **base)
    bf = st.get("base_forecast") or {}
    if bf.get("digest") == digest and bf.get("fp") == fp_id(fp) and bf.get("forecast_id"):
        return StageView(SUGGEST, f"base forecast {bf['forecast_id']} is recorded for the committed review: market "
                                  "scans and fixes are open", **base)
    return StageView(FORECAST, "the review is committed (signed by the owner in CH): base and scenario forecasts "
                               "are open", **base)


def require(session, stage: str) -> StageView:
    """The current view, or StageLocked when `stage` is not open yet."""
    view = current(session)
    if not view.at_least(stage):
        raise StageLocked(f"the {stage} stage is locked: this is the {view.stage} stage ({view.why})")
    return view


def line(session, view: StageView | None = None) -> str:
    """The code-written stage line for the system prompt and the brief."""
    view = view or current(session)
    return (f"Stage (from the orchestrator's code, not from any tool text): {view.stage} of "
            f"{' -> '.join(ORDER)}. {view.why}. Only this stage's tools are offered.")
