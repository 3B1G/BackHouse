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

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import core
from core import DATA, TAG, UPLOADS
import forecast
import pipeline
import tracelog

app = FastAPI(title="BackHouse")
WEB = Path(__file__).resolve().parents[1] / "web" / "dist"
NEEDED_TABLES = {"invoices", "invoice_lines", "vendor_bills", "payroll_runs", "pos_card_batches"}


@app.get("/api/health")
def health():
    return {"ok": True}


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
    if run is None:
        raise HTTPException(404, "That check isn't running anymore. Start a new one.")
    return StreamingResponse(run.stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/forecast")
def get_forecast(w: str = ""):
    """The forecast with the what-ifs in `w` (comma-separated ids from /api/scenarios) switched on."""
    try:
        return forecast.run([i for i in w.split(",") if i], group="dashboard")
    except ValueError as e:
        raise HTTPException(422, str(e))


@app.get("/api/dev/trace")
def dev_trace(request: Request):
    """Every agent call as it happens, for the developer page."""
    return StreamingResponse(tracelog.stream(request.headers.get("last-event-id")), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/api/scenarios")
def get_scenarios():
    return {"scenarios": [{k: v for k, v in s.items() if k != "kw"} for s in forecast.scenarios()]}


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
