"""CLI. Offline by default for every command (the Market scout reads saved menu pages only); --online lets it fetch.

  orchestrator brief [--root DIR] [--online] [--as-of YYYY-MM-DD] [--json]
  orchestrator tools                                   the tools and their parameters
  orchestrator call <tool> '<json args>'               or a path to a JSON file (PowerShell 5.1 mangles quotes)
  orchestrator suggestions                             data/suggestions/*.json
  orchestrator ask "<question>"                        orchestrator.turn (LLM); falls back to the brief
  orchestrator ask --server [--port 8005] "<question>"  the same, through a running `serve` (POST /ask with the
                                                       bearer token from data/orchestrator/rest_token)
  orchestrator serve                                   orchestrator.agent (uAgent), when it exists

Nothing here approves or applies: the owner runs the approve command each suggestion carries."""
import argparse
import asyncio
import importlib
import inspect
import json
import sys
from datetime import date
from pathlib import Path

from . import env
from .session import Session


def _args_json(text: str | None) -> dict:
    """JSON text, '@file' or a path to a JSON file -> dict."""
    if not text:
        return {}
    candidate = Path(text[1:] if text.startswith("@") else text)
    try:
        is_file = candidate.is_file()
    except OSError:                                       # e.g. a long JSON string isn't a valid path
        is_file = False
    if is_file:
        text = candidate.read_text(encoding="utf-8-sig")
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("tool arguments must be a JSON object")
    return value


def _print(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def _brief(session, as_json=False, note=None):
    from .brief import brief

    out = brief(session)
    if note:
        print(note)
    if as_json:
        _print(out)
    else:
        print(out["text"])
    return out


def _ask_server(root: Path, port: int, question: str, transport=None) -> int:
    """POST /ask on this machine's agent with its bearer token. The token is never printed."""
    import httpx

    from .agent import read_rest_token

    token = read_rest_token(root)
    if not token:
        print("ask --server: no data/orchestrator/rest_token; start `orchestrator serve` first")
        return 1
    try:
        with httpx.Client(transport=transport, trust_env=False, timeout=90) as http:
            r = http.post(f"http://127.0.0.1:{int(port)}/ask", json={"text": question, "session_id": "cli"},
                          headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as exc:
        print(f"ask --server: could not reach 127.0.0.1:{port} ({type(exc).__name__})")
        return 1
    if r.status_code != 200:
        print(f"ask --server: HTTP {r.status_code}")
        return 1
    out = r.json()
    print(out.get("text", ""))
    for note in out.get("notes") or []:
        print(f"note: {note}")
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(prog="orchestrator")
    p.add_argument("--root", type=Path, default=env.DEFAULT_ROOT, help="nessie_lab directory (its data/ is used)")
    p.add_argument("--online", action="store_true", help="let the Market scout fetch pages (default: offline)")
    p.add_argument("--as-of", type=date.fromisoformat, default=None, help="YYYY-MM-DD (default: latest bank date)")
    p.add_argument("--tag", default="boh-1loc", help="Nessie tag of the sync DB")
    sub = p.add_subparsers(dest="command", required=True)
    b = sub.add_parser("brief")
    b.add_argument("--json", action="store_true")
    b.add_argument("--root", type=Path, dest="root_after", default=None)        # also accepted after `brief`
    b.add_argument("--online", action="store_true", dest="online_after")
    sub.add_parser("tools")
    c = sub.add_parser("call")
    c.add_argument("tool")
    c.add_argument("args", nargs="?", default=None, help="JSON object, @file or a JSON file path")
    sub.add_parser("suggestions")
    a = sub.add_parser("ask")
    a.add_argument("question")
    a.add_argument("--server", action="store_true", help="ask the running agent (POST /ask) instead of in-process")
    a.add_argument("--port", type=int, default=8005)
    sub.add_parser("serve")
    args = p.parse_args(argv)
    root = getattr(args, "root_after", None) or args.root
    online = args.online or getattr(args, "online_after", False)
    session = Session(root=root, offline=not online, as_of=args.as_of, tag=args.tag)

    if args.command == "tools":
        from .tools import TOOLS
        for t in TOOLS:
            f = t["function"]
            params = ", ".join(f"{k}{'' if k in f['parameters']['required'] else '?'}"
                               for k in f["parameters"]["properties"])
            print(f"{f['name']}({params})\n    {f['description']}")
        return 0
    if args.command == "call":
        from .tools import call
        try:
            tool_args = _args_json(args.args)
        except (ValueError, OSError) as exc:
            _print({"error": f"arguments: {exc}"})
            return 1
        out = call(session, args.tool, tool_args)
        _print(out)
        return 1 if "error" in out else 0
    if args.command == "suggestions":
        folder = session.data / "suggestions"
        files = sorted(folder.glob("*.json")) if folder.exists() else []
        for f in files:
            try:
                s = json.loads(f.read_text(encoding="utf-8"))
                print(f"{f}\n    {s.get('action')} {s.get('target')}: {s.get('title')}")
            except (OSError, ValueError) as exc:
                print(f"{f}\n    unreadable ({exc})")
        if not files:
            print(f"no suggestions in {folder}")
        return 0
    if args.command == "ask" and args.server:
        return _ask_server(session.root, args.port, args.question)
    if args.command == "ask":
        try:
            turn = importlib.import_module("orchestrator.turn")
        except Exception as exc:                          # not built yet, or it failed to import
            _brief(session, note=f"(no LLM turn available: {type(exc).__name__}: {str(exc)[:200]}; "
                                 "here is the deterministic brief)")
            return 0
        try:
            out = turn.turn(session, args.question)       # async; with no LLM key it answers from the brief itself
            if inspect.isawaitable(out):
                out = asyncio.run(out)
        except Exception as exc:
            _brief(session, note=f"(LLM turn failed: {type(exc).__name__}: {str(exc)[:200]}; "
                                 "here is the deterministic brief)")
            return 0
        if isinstance(out, dict):
            print(out.get("text", ""))
            for note in out.get("notes") or []:
                print(f"note: {note}")
        else:
            print(out)
        return 0
    if args.command == "serve":
        try:
            agent_mod = importlib.import_module("orchestrator.agent")
        except Exception as exc:
            print(f"serve: not available ({type(exc).__name__}: {exc})")
            return 1
        if not callable(getattr(agent_mod, "make_agent", None)):
            print("serve: not available (orchestrator.agent has no make_agent)")
            return 1
        try:
            agent = agent_mod.make_agent(session.root)
        except ValueError as exc:                         # e.g. ORCHESTRATOR_SEED not set
            print(f"serve: {exc}")
            return 1
        agent.run()
        return 0
    _brief(session, as_json=args.json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
