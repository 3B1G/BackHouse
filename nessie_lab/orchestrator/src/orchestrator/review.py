"""The owner's review of the Bookkeeper's open findings (ORCHESTRATOR.md: "the LLM proposes, the owner commits").

    propose(session, finding, decision, note)   one proposed decision per open finding: accept | dismiss | revise
    accept_verified_errors(session)              the batch: verdict "error" AND the Verifier agreed; no arguments
    prepare(session)                             the owner's CLI: writes the immutable decision file and the CH
                                                 suggestion data/suggestions/review-<digest16>.json
    status(session)                              every open finding with its proposal

Proposals are only proposals. The review is committed when the owner signs the review suggestion at the terminal
(`changes approve <file>`, WITHOUT --apply: CH plans action "other" with no ops, signs Suggestion.model_dump, and never
applies it). stages.current then checks that signature (api.current_approval, role reader), that it is the latest
decision, its age, and that its subject equals the suggestion rebuilt from the proposals. No tool, API route or CLI
command here records a commit.

Rules in code: an LLM's note is kept only if an owner message in this session contains it verbatim; a note with a
figure that is not one of the finding's own amounts (or any unit price like "3.25/lb") forces "revise" and says why,
because the LLM must never turn a new number into an adjustment. Mapping into checks_bk: accept -> pass, dismiss ->
reject, revise -> downgrade (cautious adjustments only, until the Bookkeeper's next report). State lives in
data/orchestrator/review_state.json, written under bk_state.lock(root, "review.lock") with a .bak."""
import hashlib
import json
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict

from . import bk_state, guard

VERSION = 1
STATE_NAME = "review_state.json"
LOCK_NAME = "review.lock"
DECISIONS = ("accept", "dismiss", "revise")
UNIT = re.compile(r"(?i)\d+(\.\d+)?\s*(/|per)\s*(lb|kg|oz|case|ea|each|gal|unit)s?\b")
BATCH_VERIFIER = "agreed"
_APPROVALS: dict = {}


class FindingDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    review_id: str
    finding_key: str
    finding_id: str                                       # display only
    finding_sha: str                                      # sha256 of the canonical raw finding
    decision: Literal["accept", "dismiss", "revise"]
    note: str | None = None
    note_source: Literal["owner_text", "owner_cli", "owner_ui"] | None = None
    proposed_by: Literal["llm", "owner_cli", "owner_ui", "carried", "batch"]
    forced: str | None = None
    decided_by: str = "owner"
    decided_at: datetime


class ReviewError(ValueError):
    """The proposal or the prepare can't be done as asked; the message says why (never quoting the caller)."""


# ---------------------------------------------------------------- state
def state_path(root) -> Path:
    return bk_state.folder(root) / STATE_NAME


def reviews_dir(root) -> Path:
    return bk_state.folder(root) / "reviews"


@contextmanager
def locked(root):
    with bk_state.lock(root, LOCK_NAME):
        yield


def load(root) -> dict:
    """The review state, {} when there is none. A corrupt file falls back to .bak, else {} (which is the Audit
    stage: nothing is opened by a broken file)."""
    path = state_path(root)
    for p in (path, path.with_name(path.name + ".bak")):
        try:
            st = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(st, dict) and st.get("version") == VERSION:
            return st
    return {}


def save(root, st: dict):
    path = state_path(root)
    if path.exists():
        try:
            json.loads(path.read_text(encoding="utf-8"))
            bk_state._atomic_write(path.with_name(path.name + ".bak"), path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    bk_state._atomic_write(path, json.dumps(st, indent=2, default=str))


def canonical(obj) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)


def finding_sha(f: dict) -> str:
    return hashlib.sha256(canonical({k: v for k, v in (f or {}).items()
                                     if k not in ("id", "status", "finding_key")}).encode()).hexdigest()


def digest(review_id: str, proposals: dict) -> str:
    """sha256 over what the decisions say (not who proposed them or when)."""
    body = {"review_id": review_id, "decisions": {k: [d.get("finding_sha"), d.get("decision"), d.get("note"),
                                                      d.get("forced")] for k, d in sorted(proposals.items())}}
    return hashlib.sha256(canonical(body).encode()).hexdigest()


# ---------------------------------------------------------------- notes
def _own_figures(f: dict) -> tuple[set, set]:
    """(dollar amounts, percents) the finding itself states: its cents fields and the figures in its own text."""
    money, pct = set(), set()

    def walk(v, k=""):
        if isinstance(v, dict):
            for kk, vv in v.items():
                walk(vv, str(kk))
        elif isinstance(v, list):
            for vv in v:
                walk(vv, k)
        elif isinstance(v, int) and not isinstance(v, bool) and "cents" in k.lower():
            money.add(abs(Decimal(v)) / 100)
        elif isinstance(v, str) and k in ("explanation", "root_cause", "suggested_action", "assumption_if_unconfirmed"):
            money.update(x for _, x, _ in guard.money_figures(v) if x is not None)
            pct.update(x for _, x, _ in guard.percent_figures(v) if x is not None)
    walk(f)
    return money, pct


def _near(value, gran, known) -> bool:
    return value is not None and any(abs(k - value) <= gran / 2 for k in known)


def note_problem(note: str | None, f: dict) -> str | None:
    """Why a note must go back to the Bookkeeper (forced "revise"), or None."""
    if not note:
        return None
    text = guard.normalize(note)
    if UNIT.search(text):
        return "the note gives a unit price; a new figure goes back to the Bookkeeper as context"
    money, pct = _own_figures(f)
    for raw, value, gran in guard.money_figures(text):
        if not _near(value, gran, money):
            return "the note has a dollar figure that is not one of this finding's amounts"
    for raw, value, gran in guard.percent_figures(text):
        if not _near(value, gran, pct):
            return "the note has a percentage that is not in this finding"
    return None


def _owner_said(note: str, owner_texts) -> bool:
    n = " ".join(guard.normalize(note).split())
    return bool(n) and any(n in " ".join(guard.normalize(t).split()) for t in owner_texts or ())


# ---------------------------------------------------------------- proposing
def _view(session, minimum: str = "Review"):
    from . import stages

    view = stages.current(session)
    if not view.at_least(minimum) or view.proposals is None:
        raise stages.StageLocked(f"no review is open: this is the {view.stage} stage ({view.why})")
    return view


def _key(view, ref) -> str:
    ref = str(ref or "").strip()
    for k, fid in view.open:
        if ref and ref in (k, fid):
            return k
    ids = ", ".join(fid for _, fid in view.open[:20])
    raise ReviewError(f"that is not an open finding of this review; open findings: {ids}")


def _record(session, view, items: list[tuple[str, str, str | None, str | None, str]]) -> list[dict]:
    """Write [(key, decision, note, note_source, proposed_by)] under the review lock. Refuses when the review moved
    on meanwhile (another report came in)."""
    from . import stages

    src = stages.source(session.root)
    out = []
    with locked(session.root):
        st = load(session.root)
        if not src or st.get("review_id") != view.review_id or src["review_id"] != view.review_id:
            raise ReviewError("the review changed while deciding (a new Bookkeeper report); look again")
        props = st.setdefault("proposals", {})
        for key, decision, note, source, by in items:
            f = src["keys"][key]
            forced = note_problem(note, f)
            d = FindingDecision(review_id=view.review_id, finding_key=key, finding_id=src["ids"][key],
                                finding_sha=finding_sha(f), decision="revise" if forced else decision,
                                note=note, note_source=source if note else None, proposed_by=by, forced=forced,
                                decided_at=datetime.now(timezone.utc))
            props[key] = d.model_dump(mode="json")
            out.append(props[key])
        save(session.root, st)
    return out


def propose(session, finding: str, decision: str, note: str | None = None, *, by: str = "llm") -> dict:
    """One proposed decision. by="llm": the note is kept only when an owner message in this session holds it word
    for word (session.owner_texts); by="owner_cli"/"owner_ui": the owner typed it."""
    if decision not in DECISIONS:
        raise ReviewError("decision must be accept, dismiss or revise")
    view = _view(session)
    key = _key(view, finding)
    note = guard.normalize(note).strip()[:500] if note else None
    dropped = False
    if note and by == "llm":
        if _owner_said(note, getattr(session, "owner_texts", None)):
            source = "owner_text"
        else:
            note, source, dropped = None, None, True
    else:
        source = {"owner_cli": "owner_cli", "owner_ui": "owner_ui"}.get(by)
    d = _record(session, view, [(key, decision, note, source, by)])[0]
    return _result(session, [d], note_dropped=dropped)


def accept_verified_errors(session, by: str = "batch") -> dict:
    """Propose accept for every open finding with verdict "error" that the Verifier agreed with and that has no
    proposal yet. needs_owner, replaced and Verifier-only findings are left for one-by-one decisions."""
    from . import stages

    view = _view(session)
    src = stages.source(session.root)
    st = load(session.root)
    findings = st.get("findings") or {}
    todo, skipped = [], []
    for key, fid in view.open:
        f, info = src["keys"][key], findings.get(key) or {}
        if key in view.proposals:
            continue
        if f.get("verdict") == "error" and info.get("verifier") == BATCH_VERIFIER:
            todo.append((key, "accept", None, None, by))
        else:
            skipped.append(fid)
    made = _record(session, view, todo) if todo else []
    out = _result(session, made)
    out["skipped_need_owner"] = skipped
    return out


def _result(session, made: list[dict], note_dropped: bool = False) -> dict:
    from . import stages

    view = stages.current(session)
    out = {"proposed": [{"finding_id": d["finding_id"], "decision": d["decision"], "note": d["note"],
                         "forced": d["forced"], "proposed_by": d["proposed_by"]} for d in made],
           "stage": view.stage, "stage_why": view.why,
           "committed": False, "note": "a proposal only: the owner commits the review with `orchestrator review "
                                       "prepare` and then `changes approve` at the terminal"}
    if note_dropped:
        out["note_dropped"] = "the note was dropped: it is not in anything the owner wrote in this chat"
    return out


def status(session) -> dict:
    """Every open finding with its proposal; the stage and what the owner does next."""
    from . import stages

    view = stages.current(session)
    out = {"stage": view.public(), "committed": view.at_least(stages.FORECAST), "findings": []}
    if view.proposals is None:
        return out
    src = stages.source(session.root)
    st = load(session.root)
    info = st.get("findings") or {}
    for key, fid in view.open:
        f, d = src["keys"][key], view.proposals.get(key)
        out["findings"].append({"finding_id": fid, "verdict": f.get("verdict"), "root_cause": f.get("root_cause"),
                                "counterparty": f.get("counterparty"),
                                "impact_cents": abs(int((f.get("amounts") or {}).get("impact_cents") or 0)),
                                "verifier": (info.get(key) or {}).get("verifier"),
                                "proposal": None if d is None else {k: d.get(k) for k in (
                                    "decision", "note", "forced", "proposed_by", "decided_at")}})
    batch = [x["finding_id"] for x in out["findings"]
             if x["proposal"] is None and x["verdict"] == "error" and x["verifier"] == BATCH_VERIFIER]
    out["batch"] = {"finding_ids": batch, "line": (f"{len(batch)} verified error(s) can be accepted in one step: "
                                                   "`orchestrator review accept-errors`") if batch else None}
    return out


# ---------------------------------------------------------------- prepare and commit
def suggestion_for(src: dict, proposals: dict, dig: str, fp: dict):
    """The CH Suggestion the owner signs: action "other", rebuilt the same way every time from the proposals."""
    from changes.shared import Suggestion

    from . import stages

    counts = {d: sum(p["decision"] == d for p in proposals.values()) for d in DECISIONS}
    lines = []
    for key, fid in sorted(((k, src["ids"][k]) for k in proposals), key=lambda x: x[1]):
        p = proposals[key]
        line = f"{fid} [{key[:10]}] {p['decision']}"
        if p.get("forced"):
            line += f" (forced: {p['forced']})"
        if p.get("note"):
            line += f"; owner's note: {p['note']}"
        lines.append(line)
    return Suggestion(suggestion_id=f"review-{dig[:16]}", action="other", target=src["label"],
                      title=(f"Commit the owner's review of {src['label']}: {counts['accept']} accept, "
                             f"{counts['dismiss']} dismiss, {counts['revise']} revise"),
                      assumptions=lines or ["no open findings"],
                      based_on=[src["review_id"], f"decisions-sha256:{dig}", f"fp:{stages.fp_id(fp)}"])


def approve_command(session, suggestion_file: str | None) -> str | None:
    """The owner's terminal command: `changes approve` WITHOUT --apply (CH never applies action "other")."""
    from . import env

    if not suggestion_file:
        return None
    path = Path(session.root) / suggestion_file

    def q(p):
        return f'"{p}"' if " " in str(p) else str(p)

    if Path(session.root).resolve() == env.DEFAULT_ROOT.resolve():
        opts, target = "", suggestion_file
    else:
        opts, target = f" --root {q(Path(session.root).resolve())}", q(path.resolve())
    opts += "" if getattr(session, "tag", "boh-1loc") == "boh-1loc" else f" --tag {session.tag}"
    return f"python -m uv run --project changes changes{opts} approve {target}"


def _approval_row(db: Path, sid: str, subject: dict):
    """(why or None, decided_at) from CH's records: api.current_approval (the signature is checked with the stored
    public key) and api.latest_approval. Read with role "reader" on a quiet copy; cached on the file stamps."""
    from changes.executor import quiet_path
    from cost_price import api
    from cost_price.db import connect

    from . import stages

    key = (str(db), stages._stamp(db), sid, canonical(subject))
    if key in _APPROVALS:
        return _APPROVALS[key]
    with quiet_path(db) as path, connect(path, "reader") as con:
        _, why = api.current_approval(con, sid, subject)
        latest = api.latest_approval(con, sid)
    out = (why, latest["decided_at"] if latest else None)
    if len(_APPROVALS) > 64:
        _APPROVALS.clear()
    _APPROVALS[key] = out
    return out


def approval_problem(session, s, now: datetime | None = None) -> str | None:
    """None when CH holds the owner's signed, latest, fresh approval of exactly `s`; else why not (code-written)."""
    from . import stages

    db = Path(session.root) / "data" / "cost_price.db"
    if not db.is_file():
        return "data/cost_price.db not found, so no approval can be checked"
    try:
        why, decided = _approval_row(db, s.suggestion_id, s.model_dump(mode="json"))
    except Exception as exc:
        return f"CH's approvals could not be read ({type(exc).__name__})"
    if why:
        return f"not committed: {why}"
    try:
        at = datetime.fromisoformat(str(decided))
        at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
    except ValueError:
        return "the approval has an unreadable date"
    now = now or datetime.now(timezone.utc)
    hours = stages.ttl_hours()
    if (at - now).total_seconds() > 300:
        return "the approval is dated in the future; check the clock"
    if (now - at).total_seconds() > hours * 3600:
        return f"the approval of {decided} is more than {hours:g} h old: approve the review again"
    return None


def _write_once(path: Path, text: str) -> bool:
    """Write `text` to a new file ("x"). True if written or already identical; False if a different file is there."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(path, "x", encoding="utf-8") as f:
            f.write(text)
        return True
    except FileExistsError:
        try:
            return path.read_text(encoding="utf-8") == text
        except OSError:
            return False


def prepare(session, expected: str | None = None) -> dict:
    """The owner's step before signing: every open finding has a proposal. Writes (immutable)
    data/orchestrator/reviews/<review_id>/decisions-<digest16>.json and data/suggestions/review-<digest16>.json, the
    revise notes to data/orchestrator/reviews/context.json for the next Bookkeeper run, and returns the approve
    command. `expected`: the digest (or its first 16) the owner saw; a different current digest is refused."""
    from . import stages

    with locked(session.root):
        view = stages.current(session)
        if view.proposals is None:
            raise stages.StageLocked(f"no review is open: this is the {view.stage} stage ({view.why})")
        missing = [fid for k, fid in view.open if k not in view.proposals]
        if missing:
            raise ReviewError(f"not every open finding has a proposed decision; waiting: {', '.join(missing[:12])}")
        src = stages.source(session.root)
        dig = digest(view.review_id, view.proposals)
        if expected and not dig.startswith(str(expected).strip().lower()):
            raise ReviewError("the decisions changed since you looked (stale digest): run `orchestrator review "
                              "status` and prepare again")
        st = load(session.root)
        if st.get("review_id") != view.review_id:
            raise ReviewError("the review changed while preparing; look again")
        d16 = dig[:16]
        rel_dec = f"data/orchestrator/reviews/{view.review_id[:32]}/decisions-{d16}.json"
        body = {"review_id": view.review_id, "digest": dig, "report": view.report, "as_of": view.as_of,
                "fingerprint": stages.fp_id(view.fingerprint),
                "decisions": [view.proposals[k] for k in sorted(view.proposals)]}
        dec_path = Path(session.root) / rel_dec
        if not dec_path.exists():
            _write_once(dec_path, json.dumps(body, indent=2, default=str))
        s = suggestion_for(src, view.proposals, dig, view.fingerprint)
        rel_s = f"data/suggestions/{s.suggestion_id}.json"
        if not _write_once(Path(session.root) / rel_s, s.model_dump_json(indent=2, exclude_none=True)):
            raise ReviewError(f"{rel_s} was changed after it was written: delete it and prepare again")
        st["prepared"] = {"digest": dig, "decision_file": rel_dec, "suggestion_file": rel_s, "sid": s.suggestion_id,
                          "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        save(session.root, st)
        revise = [{"finding_key": k, "finding_id": p["finding_id"], "note": p.get("note"), "forced": p.get("forced")}
                  for k, p in sorted(view.proposals.items()) if p["decision"] == "revise"]
        bk_state._atomic_write(reviews_dir(session.root) / "context.json", json.dumps(
            {"review_id": view.review_id, "report": view.report, "as_of": view.as_of, "revise": revise,
             "note": "owner context for the next Bookkeeper run (its ReportRequest takes no extra fields yet)"},
            indent=2))
    cmd = approve_command(session, rel_s)
    return {"prepared": True, "digest": d16, "decision_file": rel_dec, "suggestion_file": rel_s,
            "approve_command": cmd, "committed": False,
            "next": f"the owner signs the review at the terminal (passphrase), WITHOUT --apply: {cmd}"}


def record_base_forecast(session, view, forecast_id: str):
    from . import stages

    with locked(session.root):
        st = load(session.root)
        if st.get("review_id") != view.review_id:
            return
        st["base_forecast"] = {"forecast_id": forecast_id, "digest": view.digest, "fp": stages.fp_id(view.fingerprint),
                               "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        save(session.root, st)


# ---------------------------------------------------------------- what the forecast uses
def adjustments(session, view) -> tuple[list, str]:
    """(adjustments, note) for the forecast. With a committed review: checks_bk on the owner's decisions (accept ->
    all, dismiss -> none, revise -> cautious only). Without one (view.proposals None): every finding awaits the
    owner, cautious adjustments only."""
    import bookkeeper_tools as bt
    import contracts as K

    from . import checks_bk, stages, tools

    if view.proposals is None or not view.at_least(stages.FORECAST):
        got, _ = tools._findings(session)
        if got is None:
            return [], "no Bookkeeper report yet: history used as the bank shows it"
        path, findings, _, _, _, passed, ver = got
        return list(passed), (f"{len(passed)} cautious adjustment(s) from {path.name} ({ver['label']}; no committed "
                              f"review: each of the {len(findings)} finding(s) awaits the owner and counts only if "
                              "cautious)")
    src = stages.source(session.root)
    if src is None or src["review_id"] != view.review_id:
        raise ReviewError("the committed review's Bookkeeper state changed: refusing to forecast")
    findings, adjs, _ = K.from_bk_report(src["raw"], bt.bank_id_map())
    present = {f.finding_id for f in findings}
    decisions = {src["ids"][k]: {"decision": d["decision"], "note": d.get("note")}
                 for k, d in view.proposals.items() if src["ids"].get(k) in present}
    used = checks_bk.passed(src["raw"], adjs, findings, decisions, src["verified_map"])
    counts = {d: sum(x["decision"] == d for x in decisions.values()) for d in DECISIONS}
    return used, (f"{len(used)} adjustment(s) from {src['label']} as of {view.as_of}, owner-reviewed decisions "
                  f"{view.digest[:16]} ({counts['accept']} accept, {counts['dismiss']} dismiss, {counts['revise']} "
                  "revise), signed by the owner in CH")
