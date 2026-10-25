"""Runs BackHouse's orchestrator (nessie_lab/orchestrator) for the dashboard, inside the orchestrator's own environment.
orchestrator_bridge.py starts it:

    uv run --project nessie_lab/orchestrator python dashboard/api/orchestrator_driver.py brief
    uv run --project nessie_lab/orchestrator python dashboard/api/orchestrator_driver.py ask     (the question on stdin)
    uv run --project nessie_lab/orchestrator python dashboard/api/orchestrator_driver.py scenario  (JSON on stdin)
    uv run --project nessie_lab/orchestrator python dashboard/api/orchestrator_driver.py <owner action>  (JSON on stdin)

It prints one JSON object per line: each tool call as it starts and as it returns, each ASI:One request, then the
result. The orchestrator itself never approves anything. The owner actions are the dashboard's buttons, each the owner's
own click: decide a finding, commit the review, set up the owner passphrase, approve a suggestion. Committing and
approving sign with the owner key, which only the owner's passphrase unlocks, exactly as CH's `changes approve` does at
a terminal. The passphrase arrives on stdin and is never printed.
"""
import asyncio
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from orchestrator import llm, tools
from orchestrator.brief import brief
from orchestrator.session import Session
from orchestrator.turn import proposals, turn

LAB = Path(os.environ.get("BACKHOUSE_LAB") or Path(__file__).resolve().parents[2] / "nessie_lab")   # tests: a scratch copy
_LOCK = threading.Lock()


def emit(**event):
    with _LOCK:                                  # tool calls can run in parallel threads
        print(json.dumps(event, default=str), flush=True)


def _what(name, args):
    """A few words on what one tool call asked, for the developer page."""
    scenario = args.get("scenario") if isinstance(args.get("scenario"), dict) else {}
    if scenario.get("name"):
        return str(scenario["name"])
    if name == "propose_fix":
        return f"{str(args.get('action') or 'reprice_item').replace('_', ' ')}: {args.get('dish')}"
    if name == "lookup":
        return str(args.get("tool") or "")
    if name == "list_bills_due" and args.get("before"):
        return f"due by {args['before']}"
    return ""


def _note(name, out):
    """What came back, in a few words (and, for the market scan, the counts the dashboard shows)."""
    try:
        if name == "get_cash_position":
            return f"{out['total_usd']} on {out['as_of']}", None
        if name in ("forecast_base", "forecast_scenario"):
            vs = out.get("vs_base") or {}
            if vs.get("days_moved_average") is not None:
                return f"{vs['days_moved_average']} days later on average", None
            likely = out.get("run_out_likely") or (out.get("after") or {}).get("run_out_likely")
            return (f"likely out of cash {likely}" if likely else "cash lasts the whole forecast"), None
        if name == "list_bills_due":
            return f"{out['count']} bills, {out['total_usd']}", None
        if name == "get_findings":
            return (f"{len(out['findings'])} findings" if out.get("available") else "no Bookkeeper report yet"), None
        if name == "scan_market":
            kinds = [p.get("position") for p in out.get("positions", [])]
            counts = {k: kinds.count(k) for k in ("below_market", "at_par", "above_market", "insufficient_data")}
            return (f"{counts['below_market']} below, {counts['at_par']} at, {counts['above_market']} above market; "
                    f"{counts['insufficient_data']} with too few prices"), counts
        if name == "propose_fix":
            return f"{out['suggestion']['title']}: dry run passed, saved for your approval", None
        if name == "list_upcoming_changes":
            return f"{len(out.get('changes', []))} approved changes coming", None
        if name == "get_menu":
            return f"{len(out.get('dishes', []))} dishes", None
    except (KeyError, TypeError, AttributeError):
        pass
    return None, None


_call = tools.call


def traced_call(session, name, args):
    emit(kind="call", tool=name, what=_what(name, args or {}))
    t = time.perf_counter()
    out = _call(session, name, args)
    error = out.get("error") if isinstance(out, dict) else None
    note, counts = (None, None) if error else _note(name, out)
    emit(kind="result", tool=name, ms=round((time.perf_counter() - t) * 1000), error=str(error)[:200] if error else None,
         note=note, counts=counts)
    return out


tools.call = traced_call                         # every caller looks it up on the module, so all calls are traced
_chat = llm.ASIClient.chat


async def traced_chat(self, messages, tools=None, deadline=None):
    t = time.perf_counter()
    try:
        out = await _chat(self, messages, tools=tools, deadline=deadline)
    except Exception as exc:                     # LLMError messages never carry the key
        emit(kind="model", ms=round((time.perf_counter() - t) * 1000), model=self.model, error=str(exc)[:200])
        raise
    asked = (out["choices"][0].get("message") or {}).get("tool_calls") or []
    emit(kind="model", ms=round((time.perf_counter() - t) * 1000), model=self.model,
         tokens=(out.get("usage") or {}).get("total_tokens"), tool_calls=len(asked))
    return out


llm.ASIClient.chat = traced_chat
TAG = "boh-1loc"
SUGGESTION = re.compile(r"^data/suggestions/(orch|review)-[A-Za-z0-9._-]{1,80}\.json$")
OWNER_MODES = ("owner", "decide", "accept-verified", "setup", "commit", "approve")


def _db(role="reader"):
    from cost_price.db import connect

    return connect(LAB / "data" / "cost_price.db", role)       # cost_price enforces each role with an SQLite authorizer


def owner_status(session):
    """The owner's review: the stage, every open finding with its decision so far, and whether the owner key exists."""
    from cost_price.owner import fingerprint, public_key
    from orchestrator import review, stages

    out = review.status(session)
    src = stages.source(session.root) or {}
    full = {src["ids"][k]: f for k, f in (src.get("keys") or {}).items()}
    for f in out["findings"]:
        x = full.get(f["finding_id"]) or {}
        dates = x.get("dates") or {}
        f.update(explanation=x.get("explanation"), suggested_action=x.get("suggested_action"),
                 first=dates.get("first"), last=dates.get("last"), recoverable=x.get("recoverable"))
    with _db() as con:
        pub = public_key(con)
    return {**out, "owner_key": pub is not None, "fingerprint": fingerprint(pub) if pub else None}


def sign(rel, passphrase, apply):
    """What `changes approve <file> [--apply --live]` does, with the passphrase the owner typed in the dashboard:
    unlock the owner key, plan the change fresh, sign exactly what was planned, then (for a change) apply it."""
    from changes.cli import load_suggestion, question
    from changes.executor import Executor, subject_id
    from changes.shared import Approval
    from cost_price.owner import unlock

    if not SUGGESTION.fullmatch(str(rel)):
        raise ValueError("that isn't a suggestion file")
    subject = load_suggestion(LAB / rel)
    with _db() as con:
        key = unlock(con, str(passphrase or ""))
    ex = Executor(LAB, key, tag=TAG)
    shown, ops = ex.plan(subject)
    line = question(shown, ops, ex.today)
    ex.record_approval(subject, Approval(suggestion_id=subject_id(subject), decision="approved",
                                         decided_at=datetime.now(timezone.utc), note="approved in the BackHouse dashboard"),
                       shown=shown)
    out = {"line": line, "applied": None}
    if apply:
        out["applied"] = ex.apply(subject, live=True).model_dump(mode="json")
    return out


def owner(mode, session, args):
    from orchestrator import review

    if mode == "decide":
        review.propose(session, str(args.get("finding")), str(args.get("decision")), by="owner_ui")
    elif mode == "accept-verified":
        review.accept_verified_errors(session, by="owner_ui")
    elif mode == "setup":
        from cost_price.owner import init_owner, public_key

        with _db("owner") as con:
            if public_key(con):
                raise ValueError("the owner passphrase is already set up")
            init_owner(con, str(args.get("passphrase") or ""))
    elif mode == "commit":
        prepared = review.prepare(session)
        signed = sign(prepared["suggestion_file"], args.get("passphrase"), apply=False)   # signs the review, applies nothing
        tools.call(session, "forecast_base", {})           # the base forecast under these decisions: Suggest opens
        return {**owner_status(session), "signed": signed["line"]}
    elif mode == "approve":
        return {**sign(args.get("file"), args.get("passphrase"), apply=True), "owner_key": True}
    return owner_status(session)


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "brief"
    session = Session(root=LAB)                  # offline: Market scout reads the saved menu pages
    try:
        if mode == "brief":
            b = brief(session)
            menu = next((e["result"] for e in reversed(session.log)
                         if e["name"] == "get_menu" and "error" not in e["result"]), {})
            emit(kind="done", text=b["text"], facts=b["facts"], menu=menu)
        elif mode == "ask":
            out = asyncio.run(turn(session, sys.stdin.read()))
            emit(kind="done", **out, proposals=proposals(session))
        elif mode == "scenario":                 # one what-if on the Forecaster's 1,000 futures, for the Chat
            emit(kind="done", **tools.call(session, "forecast_scenario", {"scenario": json.loads(sys.stdin.read() or "{}")}))
        elif mode in OWNER_MODES:
            emit(kind="done", **owner(mode, session, json.loads(sys.stdin.read() or "{}")))
        else:
            raise ValueError(f"unknown mode {mode!r}")
    except Exception as exc:                     # messages name what went wrong, never the passphrase
        emit(kind="error", error=str(exc)[:300] or type(exc).__name__, type=type(exc).__name__)
        sys.exit(1)


if __name__ == "__main__":
    main()
