# Bookkeeper

An ASI-powered uAgent that investigates restaurant records through two simulated read-only tools and saves an **unverified** JSON report. It returns the filename to the requesting orchestrator. It does not refresh Nessie, modify the books, run a verifier, or run Monte Carlo.

## Setup

From the repository root:

```bash
uv python install 3.13
uv sync --project bookkeeper
uv run --project bookkeeper python --version
```

This subproject uses `bookkeeper/.venv` and its own lockfile. The parent project remains on Python 3.14. Hatch explicitly packages `src/bookkeeper`; the distribution name is `restaurant-bookkeeper`.

Set `ASI1_API_KEY` in your shell or the **repository root** `.env`. `ASI_MODEL` defaults to `asi1`. See `.env.example` for all settings; never commit real credentials. Set `BOOKKEEPER_SEED` before starting the agent server.

The default sources must already exist:

- `data/books.db`
- `data/nessie_sync_boh-1loc.db`

The default cutoff is the latest transaction date in the snapshot, not today's date. Requests beyond that date fail rather than imply live coverage. Snapshot completeness is assumed, not verified. With the current snapshot, the latest transaction date is October 23, 2026.

## Generate a report directly

```bash
uv run --project bookkeeper bookkeeper run --request-id first-report
```

Optional: `--as-of YYYY-MM-DD` and `--tag boh-1loc`. To use another data root, put `--root /path/to/nessie_lab` **before** `run`, `serve`, or `client`.

The CLI prints:

```json
{
  "request_id": "first-report",
  "report_id": "R-<request-hash>",
  "filename": "data/reports/R-<request-hash>.json",
  "as_of": "2026-10-23",
  "verified": false
}
```

Files are immutable. The request key combines sender and request ID: redelivery returns the existing report. Use a new request ID to request a fresh report. Reusing an ID with different parameters is an error. Reports are under `data/reports`; tool/model traces are under `data/bookkeeper_traces`. Shared storage is required for the orchestrator to open the filename.

## Local orchestrator demo

Start Bookkeeper in one terminal:

```bash
uv run --project bookkeeper bookkeeper serve
```

Copy the agent address printed at startup. In another terminal:

```bash
uv run --project bookkeeper bookkeeper client \
  --target agent1... --request-id local-demo-1
```

The client sends a standard `ChatMessage`, receives an immediate acknowledgement, then prints the final report response. Stop it with Ctrl-C after receiving the result. The default server and client ports are 8001 and 8002. For custom ports use `serve --port`, `client --port --endpoint`, and `BOOKKEEPER_LOCAL_CLIENT_ENDPOINT` accordingly. Local mode uses explicit endpoint resolution and does not register the agents publicly. The demo client's fixed seed is only for localhost testing.

## ASI orchestrator integration

For Agentverse connectivity set `BOOKKEEPER_MAILBOX=true`, keep `BOOKKEEPER_SEED` stable, start `serve`, and connect the agent to an Agentverse mailbox using the startup instructions. This external account setup is required before an ASI orchestrator can contact it; installing the package alone does not publish a reachable agent.

Send a `ChatMessage` with one `TextContent` containing:

```json
{"request_id":"orchestrator-job-1","nessie_tag":"boh-1loc","as_of":"2026-07-24"}
```

`as_of` and `nessie_tag` are optional. The handler acknowledges the incoming message ID, retains the session context, and sends the same result shape as the CLI, followed by `EndSessionContent`. Invalid requests and failed runs receive `status: "error"` and no filename. Requests are serialized in one service process; multiple server processes sharing the output directory are not supported.

The ASI model uses `https://api.asi1.ai/v1/chat/completions`. A run allows up to 40 model turns: at most 39 for investigation, with the last reserved for writing the report with tools disabled. The model is warned as its query budget runs low and must disclose any remaining coverage gaps. These are ordinary tool interactions, not verifier iterations. No report revision/retry loop runs after a final draft. API failures, tool calls during finalization, or malformed final JSON fail the run. The server logs the current turn and report-writing phase. Failed runs retain a trace with an error message when source loading succeeded; the client receives a safe diagnostic and trace ID.

Investigation API calls have a 120-second read timeout. Final report writing has a
600-second read timeout, configurable with `ASI_READ_TIMEOUT_SECONDS` and
`ASI_REPORT_TIMEOUT_SECONDS` respectively. These are per-read network timeouts,
not a whole-run deadline. A slow final response can take several minutes.

If the final writing call fails with a transport error, resume it without repeating
the investigation:

```bash
uv run --project bookkeeper bookkeeper resume --trace-id TR-<failed-trace-id>
```

This makes one writing call with tools disabled, using the saved messages,
evidence, dates, and cash balances. It does not query changed source files or run
a verifier. The original failed trace remains unchanged; the new trace references
it through `resumed_from`. The CLI prints the saved report filename. Repeating the
resume command returns that same report once it succeeds. Recovery is keyed by
the trace ID, separately from the agent's sender/request pair: rerunning the old
client request would start another investigation, so use the returned filename.
Investigation failures and completed but malformed drafts cannot be resumed by
this command; no automatic report-revision or retry loop is added.

## Tool replacement interface

Only `query_nessie` and `query_books` are exposed to the model. Both accept:

```json
{"operation":"query","sql":"SELECT * FROM invoices ORDER BY invoice_no","offset":0,"limit":200}
```

For schema discovery, use `operation: "schema"`, `sql: null`, `offset: 0`, and `limit: 200`. Query results contain `rows`, `truncated`, `next_offset`, and `as_of`. Limit is 1–200; use stable `ORDER BY` when paging. Aggregates are allowed. Queries have a three-second execution budget and 120 KB result cap.

`DataTool.call()` in `tools.py` is the adapter contract. The current `SnapshotTools` owns two private SQLite copies. To integrate production tools, supply an equivalent snapshot provider to `Bookkeeper(..., snapshots=...)` exposing `tools`, source paths, cutoff, start, bank rows, limitations, `balances()` and `close()`. Keep the two model-facing schemas unchanged. All model data access must pass through these adapters.

Adapters filter source records at the cutoff before SQL runs, restrict tables, remove synthetic bank IDs and `|ref=` markers, and deny writes/attachments. Parent/child documents are filtered together. Model `bank_ids` refer to native `nessie_id` values. Static contracts and recipes lack version history; historical validity is explicitly assumed. Snapshot-only bills, open AP and deposits-in-transit are omitted. Future tax payment dates and dispute outcomes are hidden. Invoice status comparison remains future work; the current database has no historical invoice state.

Most monetary values use integer cents; `invoice_lines.unit_price` uses dollars per unit. Cash is calculated in code using opening balances plus completed deposits minus completed purchases/withdrawals. Internal transfers affect each account but cancel in the combined balance. Pending transactions and proposed corrections/refunds do not change cash.

## Report contract and verification

`contracts.py` defines schema version 1.1, extending `bookkeeper_report_format.md` with `request_id`, `balances`, and `limitations`. The model supplies findings, coverage, summaries and cleared items. The application supplies source metadata, trace IDs, balances and verification state.

Pydantic checks structure and types only. No financial/evidence verification is performed, coverage is model-reported, and `verification.passed` is always `false` (`checked_at: null`, zero findings checked/rejected). These reports are not yet eligible for a Forecaster that requires verified inputs. The independent verifier and its three-round comparison process remain future work.

## Tests and grading export

```bash
uv run --project bookkeeper pytest bookkeeper/tests -q
uv run --project bookkeeper bookkeeper export-flags \
  data/reports/R-REPORT.json data/flags_bookkeeper.json
```

The offline export translates native Nessie IDs to the grading IDs already in the sync snapshot. It never reads the answer key. For `exclude`/`correct_to` findings it exports treatment-target IDs rather than every evidence row, so a legitimate original payment is not flagged alongside its duplicate. Run `grade.py` separately if desired; grading is not part of the agent's generation process.

Tests use a scripted model and small fixtures, plus optional read-only checks on local generated databases. A live model run requires an ASI API key and incurs API usage.
