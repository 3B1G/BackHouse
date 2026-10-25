"""BackHouse dashboard API. Run from the repo root:

    uv run --project dashboard uvicorn main:app --app-dir dashboard/api --port 8000

While developing, add --reload --reload-dir dashboard/api --timeout-graceful-shutdown 2 (the developer page's live
stream never ends on its own, so without the timeout a reload waits for it).

In development the React app (dashboard/web, `pnpm dev`) proxies /api here; after `pnpm build`, this also serves the app.
"""
import json
import sqlite3
import uuid
from contextlib import closing
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core
from core import DATA, TAG, UPLOADS
import orchestrator_bridge
import pipeline
import tracelog

app = FastAPI(title="BackHouse")
WEB = Path(__file__).resolve().parents[1] / "web" / "dist"
NEEDED_TABLES = {"invoices", "invoice_lines", "vendor_bills", "payroll_runs", "pos_card_batches"}


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/readiness")
def readiness():
    """Whether the bank and records are in place yet (missing: "bank", "records"), so the app can ask for them."""
    return core.readiness()


@app.post("/api/bank/connect")
def connect_bank():
    """Proves the bank connection: reads the restaurant's accounts from Nessie, live."""
    from nessie_client import NessieError, call
    manifest = json.loads((DATA / f"nessie_manifest_{TAG}.json").read_text())
    try:
        accounts = [call("GET", f"/accounts/{aid}") for aid in manifest["accounts"].values()]
    except NessieError as e:
        raise HTTPException(502, f"Nessie didn't answer: {str(e)[:120]}. Check the connection and try again.")
    return {"accounts": [{"name": (a.get("nickname") or "").split(" | ")[0], "type": a.get("type")} for a in accounts]}


@app.post("/api/records")
async def upload_records(file: UploadFile):
    """Checks an uploaded books export. This demo reads Demo Bistro's export (.db); other formats say so plainly."""
    data = await file.read()
    if not data.startswith(b"SQLite format 3\x00"):
        raise HTTPException(422, "This demo reads a books export saved as .db. CSV and Excel uploads aren't supported yet.")
    UPLOADS.mkdir(exist_ok=True)
    uid = uuid.uuid4().hex[:10]
    path = UPLOADS / f"{uid}.db"
    path.write_bytes(data)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
        tables = {r[0] for r in con.execute("select name from sqlite_master where type = 'table'")}
        missing = NEEDED_TABLES - tables
        if missing:
            raise HTTPException(422, f"This file is missing {', '.join(sorted(missing))}, so it isn't a books export we can read.")
        invoices, last = con.execute("select count(*), max(invoice_no) from invoices").fetchone()
    with closing(sqlite3.connect((DATA / "books.db").as_uri() + "?mode=ro", uri=True)) as con:
        sample = con.execute("select count(*), max(invoice_no) from invoices").fetchone()
    if (invoices, last) != tuple(sample):
        raise HTTPException(422, "These are a different restaurant's books. The demo can check Demo Bistro's books for now.")
    return {"upload_id": uid, "name": file.filename, "size": len(data), "invoices": invoices}


class RunRequest(BaseModel):
    source: str = "sample"
    upload_id: str | None = None
    name: str | None = None


@app.post("/api/runs")
def start_run(req: RunRequest):
    upload = {"name": req.name or "your file"} if req.source == "upload" else None
    return {"run_id": pipeline.start(req.source, upload).id}


@app.get("/api/runs/{run_id}/events")
def run_events(run_id: str):
    run = pipeline.RUNS.get(run_id)
    events = None if run else pipeline.saved(run_id)              # after an API restart, a finished check from disk
    if run is None and events is None:
        raise HTTPException(404, "That check isn't running anymore. Start a new one.")
    return StreamingResponse(run.stream() if run else pipeline.replay(events), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/dev/trace")
def dev_trace(request: Request):
    """Every agent call as it happens, for the developer page."""
    return StreamingResponse(tracelog.stream(request.headers.get("last-event-id")), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# The owner's buttons. Each body is JSON, so another site can't post one from the owner's browser (it would need a CORS
# preflight this API never answers); a passphrase only ever goes on to the orchestrator's driver, on its stdin.
class Decision(BaseModel):
    finding: str
    decision: Literal["accept", "dismiss"]


class Passphrase(BaseModel):
    passphrase: str


class Approval(BaseModel):
    file: str
    passphrase: str


class OwnPrice(BaseModel):
    dish: str
    new_price_cents: int


class Decline(BaseModel):
    id: str
    action: str
    dish: str | None = None
    new_price_cents: int | None = None
    title: str | None = None


def _owner(mode, payload=None):
    try:
        return orchestrator_bridge.owner(mode, payload)
    except orchestrator_bridge.OrchestratorError as e:
        raise HTTPException(400, f"{str(e)[:1].upper()}{str(e)[1:300]}")


@app.get("/api/owner")
def owner_review():
    """The owner's review of the Bookkeeper's findings: the stage, each finding and its decision, and the owner key."""
    return _owner("owner")


@app.post("/api/owner/decide")
def owner_decide(d: Decision):
    return _owner("decide", d.model_dump())


@app.post("/api/owner/accept-verified")
def owner_accept_verified():
    return _owner("accept-verified")


@app.post("/api/owner/setup")
def owner_setup(p: Passphrase):
    return _owner("setup", p.model_dump())


@app.post("/api/owner/commit")
def owner_commit(p: Passphrase):
    """Signs the review with the owner key, then records the base forecast, which opens the suggestions."""
    return _owner("commit", p.model_dump())


@app.post("/api/owner/approve")
def owner_approve(a: Approval):
    """Signs one suggestion with the owner key and has Changes apply it to the cost/price database."""
    return _owner("approve", a.model_dump())


@app.post("/api/owner/propose")
def owner_propose(p: OwnPrice):
    """The owner's own price for a dish: the orchestrator forecasts it and Changes dry-runs it, ready to approve."""
    return _owner("propose", p.model_dump())


@app.post("/api/owner/decline")
def owner_decline(d: Decline):
    orchestrator_bridge.decline(d.model_dump())
    return {"ok": True}


@app.post("/api/owner/plan")
def owner_plan():
    """The orchestrator's brief again, after a review or an approval: the stage now, and the suggestions it opens."""
    try:
        return orchestrator_bridge.plan(orchestrator_bridge.run("brief", "owner"))
    except orchestrator_bridge.OrchestratorError as e:
        raise HTTPException(400, str(e)[:300])


class ChatRequest(BaseModel):
    messages: list[dict]


@app.post("/api/chat")
def chat_answer(req: ChatRequest):
    import chat
    return StreamingResponse(chat.answer(req.messages), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


if WEB.exists():
    app.mount("/assets", StaticFiles(directory=WEB / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        target = WEB / path
        return FileResponse(target if path and target.is_file() else WEB / "index.html")
