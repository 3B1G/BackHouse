"""/api/orch: the dashboard's server-side window onto the orchestrator's JSON API (`orchestrator serve`, port 8005).

The orchestrator wants a bearer token on every request (nessie_lab/data/orchestrator/rest_token, new each time `serve`
starts), so it is read here on every call and never reaches the browser. The browser's Origin isn't forwarded either:
requests from a page that isn't the dashboard are turned away here (403) instead. The orchestrator answers problems as
HTTP 200 with ok: false and an error, and that JSON passes through unchanged.

Only the review routes are here, plus health, brief and suggestions. There is no commit or approve route: the owner
commits the review at their own terminal (`orchestrator review prepare`, then `changes approve ...` with the passphrase).
"""
import os
from typing import Literal

import requests
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from core import DATA

ORCH_URL = os.environ.get("BOH_ORCH_URL", "http://127.0.0.1:8005").rstrip("/")
TOKEN = DATA / "orchestrator" / "rest_token"
DASHBOARD_ORIGINS = {f"http://{host}:{port}" for host in ("localhost", "127.0.0.1") for port in (5173, 8000)}
NOT_RUNNING = "Orchestrator isn't running: start `orchestrator serve` in nessie_lab"
TIMEOUT = (3, 100)              # connect, read: the brief can take up to 90 s


def same_dashboard(request: Request):
    """A browser sends Origin on POSTs and cross-site requests; when it does, it must be this dashboard's."""
    origin = request.headers.get("origin")
    if origin is not None and origin.rstrip("/").lower() not in DASHBOARD_ORIGINS:
        raise HTTPException(403, "Only the BackHouse dashboard can use this.")


router = APIRouter(prefix="/api/orch", dependencies=[Depends(same_dashboard)])


def _token():
    try:
        return TOKEN.read_text(encoding="utf-8").strip() or None
    except OSError:
        return None


def _call(method, path, body=None):
    token = _token()
    if not token:
        raise HTTPException(503, NOT_RUNNING)
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        res = requests.request(method, ORCH_URL + path, json=body, headers=headers, timeout=TIMEOUT)
    except requests.ConnectionError:
        raise HTTPException(503, NOT_RUNNING)
    except requests.Timeout:
        raise HTTPException(504, "The orchestrator took too long to answer. Try again.")
    if res.status_code in (401, 403):
        raise HTTPException(502, f"The orchestrator turned the dashboard away (error {res.status_code}). "
                                 "Restart `orchestrator serve` and try again.")
    try:
        data = res.json()
    except ValueError:
        raise HTTPException(502, f"The orchestrator sent something that isn't JSON (error {res.status_code}).")
    return JSONResponse(data, status_code=res.status_code)


class Decide(BaseModel):
    finding: str | None = Field(None, max_length=80)
    decision: Literal["accept", "dismiss", "revise"] | None = None
    note: str | None = Field(None, max_length=500)
    accept_verified_errors: bool = False


@router.get("/health")
def health():
    return _call("GET", "/api/health")


@router.get("/brief")
def brief():
    return _call("GET", "/api/brief")


@router.get("/review")
def review():
    return _call("GET", "/api/review")


@router.post("/review/decide")
def decide(req: Decide):
    """Records the owner's proposed decision. It never commits: that happens at the terminal."""
    body = req.model_dump(exclude_none=True)
    if not req.accept_verified_errors:
        body.pop("accept_verified_errors")
    return _call("POST", "/api/review/decide", body)


@router.get("/suggestions")
def suggestions():
    return _call("GET", "/api/suggestions")
