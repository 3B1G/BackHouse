"""Connect a running mailbox-mode orchestrator to Agentverse without the browser.

uagents 0.26.1 (uagents/agent.py, Agent.__init__) adds `POST /connect` with request model
uagents.mailbox.AgentverseConnectRequest {user_token, agent_type, endpoint?, team?} and response
RegistrationResponse {success, detail?}, but only when enable_agent_inspector=True (make_agent turns it on in
mailbox mode). uagents/asgi.py lists /connect in RESERVED_ENDPOINTS and answers 403 unless the request comes from
127.0.0.1, and the orchestrator's GuardedServer also wants its bearer token (data/orchestrator/rest_token, written
at startup), so this must run on the same machine, as the same user, as the agent. Once a /connect succeeds the
agent closes /connect and /disconnect for the rest of the run.

    ORCHESTRATOR_MAILBOX=1 orchestrator serve        # in one terminal
    AGENTVERSE_TOKEN=... python -m orchestrator.register [--port 8005]

The token is read from AGENTVERSE_TOKEN (or nessie_lab/.env, BackHouse/.env) and is never printed or logged.
The browser Agent Inspector can't connect it: the agent refuses requests from web origins."""
import argparse
import os
import sys
from pathlib import Path

import httpx

from .llm import load_env

DEFAULT_ROOT = Path(__file__).resolve().parents[3]           # nessie_lab/


def rest_token(root: Path = DEFAULT_ROOT) -> str | None:
    try:
        return (Path(root) / "data" / "orchestrator" / "rest_token").read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def connect(port: int = 8005, token: str | None = None, transport: httpx.BaseTransport | None = None,
            root: Path = DEFAULT_ROOT) -> dict:
    load_env()
    token = (token or os.getenv("AGENTVERSE_TOKEN", "")).strip()
    if not token:
        return {"success": False, "detail": "set AGENTVERSE_TOKEN (an Agentverse API key) first"}
    bearer = rest_token(root)
    if not bearer:
        return {"success": False, "detail": "no data/orchestrator/rest_token: start the agent (mailbox mode) first"}
    url = f"http://127.0.0.1:{int(port)}/connect"
    try:
        with httpx.Client(transport=transport, trust_env=False, timeout=30) as http:
            response = http.post(url, json={"user_token": token, "agent_type": "mailbox"},
                                 headers={"Authorization": f"Bearer {bearer}"})
    except httpx.HTTPError as exc:
        return {"success": False, "detail": f"could not reach the agent on 127.0.0.1:{port} ({type(exc).__name__}); "
                                            "is it running with ORCHESTRATOR_MAILBOX=1?"}
    if response.status_code == 404:
        return {"success": False, "detail": "the agent has no /connect endpoint: start it in mailbox mode (with "
                                            "ORCHESTRATOR_INSPECTOR on); it closes after one successful connect"}
    if response.status_code in (401, 403):
        return {"success": False, "detail": f"HTTP {response.status_code}: the agent refused the token (is "
                                            "data/orchestrator/rest_token from this run?)"}
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict):
        return {"success": False, "detail": f"HTTP {response.status_code}"}
    detail = str(body.get("detail") or "").replace(token, "[token]").replace(bearer, "[token]")[:300]
    return {"success": bool(body.get("success")), "detail": detail or None}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Connect the running orchestrator to an Agentverse mailbox")
    parser.add_argument("--port", type=int, default=8005)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="nessie_lab directory the agent serves")
    args = parser.parse_args(argv)
    result = connect(args.port, root=args.root)
    print(("connected: " if result["success"] else "not connected: ") + (result.get("detail") or ""))
    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
