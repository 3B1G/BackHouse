# Bookkeeper

An ASI-powered uAgent that investigates restaurant records through 11 read-only domain tools over local snapshots and saves an **unverified** JSON report. It returns the filename to the requesting orchestrator. It does not refresh Nessie, modify the books, run a verifier, or run Monte Carlo.

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

The ASI model uses `https://api.asi1.ai/v1/chat/completions`.

### Batched investigation

A run is a set of small, independent investigations ("batches"), never one long conversation, so no model call carries every area's evidence. Batches run in parallel, `BOOKKEEPER_CONCURRENCY` at a time (default 4; lower it if ASI returns HTTP 429). Each batch starts a fresh conversation with its own brief, its date window, a subset of the 11 tools, and a compact bank memo index for its area (memo pattern, count, dates and total; at most 60 lines). The plan (`batches.py`):

| Batches | Window | Turn cap |
|---|---|---|
| Card settlements, cash deposits, delivery payouts | whole window, exceptions only | 6 each |
| One per supplier (from `suppliers`) | whole window | 10 |
| Recurring obligations (rent, loan, payroll, vendor bills, sales tax, draws, repairs, permits, transfers) | whole window | 12 |
| Charges with no supporting document | whole window | 10 |

With the current snapshot that is 10 batches and, typically, about 20 model calls; on the scripted end-to-end run the largest single call was about 43k characters. When a batch is finished the model replies with its result directly (a Markdown code fence around the JSON is accepted). If it replies with anything else, is cut off at the output limit, or reaches its last turn, tools are disabled and it is asked for the result. Each batch returns a `BatchResult` (findings, cleared, coverage, limitations). Patterns are reported with at most 20 representative `bank_ids`; `exclude`/`correct_to` treatments still list every erroneous row.

Code then assembles the report: findings with the same root cause and counterparty that are close in time or share bank rows are merged (the same duplicate seen by a supplier batch and the unrecorded-charges batch becomes one finding), conflicting treatments of the same bank rows keep the more confident finding and are noted, cleared items overlapping a finding are dropped, and the summary and coverage are computed. A batch whose result is invalid, cut off or written while asking for tools is listed under limitations as "Not reviewed: …" and the run continues; the run fails if no batch produced a result or if the run as a whole lacks successful bank and books evidence. An API or network failure in one batch fails the run, but only after the other batches finish, so their results are kept in the trace for resume.

**Hand-off rules.** Reports are consumed by the team's `nessie_lab/contracts.py` (`from_bk_report`), which drops findings it cannot convert. Bookkeeper therefore checks every batch result against the same rules: a finding cites evidence, names a bank row or a document, uses only bank ids the batch's tools returned, gives positive `ongoing`, `correct_to` and `owed` amounts, and puts the document still owed in `books_refs` for an `owed` treatment. An early result that breaks a rule gets one tools-disabled write with the problems listed; in the written result, findings that still break a rule are left out, with a "Left out a … finding (…): …" limitation and `dropped_findings` in the trace. The report schema itself (1.1) is unchanged.

Each response may make at most 20 tool calls; any calls beyond that, or calls to a tool outside the batch's subset, are answered with a "not run" error (and do not count as evidence). Output limits are `ASI_MAX_TOKENS` (investigation turns, default 12000) and `ASI_REPORT_MAX_TOKENS` (result writing, default 32000). These are ordinary tool interactions, not verifier iterations; no result is revised once written. The server logs each batch's turns. The trace (`format: "batched/1"`) records the plan, each batch's conversation, result or reason, the token `usage` of every call, and the assembly. Failed runs retain a trace with an error message when source loading succeeded; the client receives a safe diagnostic and trace ID.

Investigation API calls have a 120-second read timeout. Final report writing has a
600-second read timeout, configurable with `ASI_READ_TIMEOUT_SECONDS` and
`ASI_REPORT_TIMEOUT_SECONDS` respectively. These are per-read network timeouts,
not a whole-run deadline. A slow final response can take several minutes.

If batches' result-writing calls fail with a transport error, resume them without repeating
the investigation:

```bash
uv run --project bookkeeper bookkeeper resume --trace-id TR-<failed-trace-id>
```

This makes one writing call, with tools disabled, for each batch that failed at that step,
using its saved messages, then assembles the report from the saved batch results, dates
and cash balances. Batches that failed during investigation are listed as not reviewed,
because resume never reopens the databases or runs a tool. It does not run a verifier. The original failed trace remains unchanged; the new trace references
it through `resumed_from`. The CLI prints the saved report filename. Repeating the
resume command returns that same report once it succeeds. Recovery is keyed by
the trace ID, separately from the agent's sender/request pair: rerunning the old
client request would start another investigation, so use the returned filename.
Investigation failures and completed but malformed drafts cannot be resumed by
this command; no automatic report-revision or retry loop is added. Traces written
before batching (one conversation, including the older `query_nessie`/`query_books`
traces) still resume through their original path, and previously saved reports and
receipts are still returned as-is.

## Investigation tools

The model gets 11 read-only function tools (`asi_tool_definitions()` in `bookkeeper_tools.py`), in two groups:

| Group | Tools |
|---|---|
| bank (Nessie snapshot) | `search_bank_transactions`, `get_bank_transaction`, `get_payee_history`, `get_account_balances`, `get_bank_bills` |
| books (`books.db`) | `search_costs`, `get_cost`, `get_agreements`, `get_card_batches`, `get_cash_deposit_slips`, `get_delivery_payouts` |

A report needs at least one successful result from each group; error and `unavailable` results do not count, and the model need not call every tool. Arguments are validated against the schema before a tool runs; problems come back as `{"error": ...}` for the model to correct.

Every response carries `cutoff` and the `limitations` that apply to it, and is capped at 120 KB. Primary lists (`search_bank_transactions`, `search_costs`, `get_card_batches`, `get_cash_deposit_slips`, `get_delivery_payouts`) take `limit` (at most 50) and `offset` and return `total_matches`, `returned`, `truncated` and `next_offset`, in a stable order; totals and sums cover every match, not just the page. Supplementary lists (same-amount rows, memo groups, chargebacks, line items, earlier costs) are capped and report `<name>_total` and `<name>_truncated`.

Money is integer cents except `unit_price_dollars`; `*_pct` fields are percents. Model-facing `bank_id` values are native Nessie ids.

**Settlement matching.** `get_card_batches`, `get_cash_deposit_slips` and `get_delivery_payouts` match every book row to its bank deposit in code, one-to-one and only against bank rows on or before the cutoff: card batches to the `<PROCESSOR> DEP MMDD` deposit for the batch date (within 10 days), platform weeks to `<PLATFORM> PAYOUT WK MMDD` (within 20 days of the week start), and cash slips to one `BRANCH DEPOSIT` on the slip date (same-day pairs first, then up to 3 business days, closest amount). Rows gain `bank_ids`, `bank_date`, `deposit_cents`, `difference_cents` (deposit minus expected) and `match_status`: `matched` (within `tolerance_cents`: 10 for cards and payouts, 0 for cash), `amount_mismatch`, `missing` (due by the cutoff, no deposit), `not_yet_due` or `multiple`. Responses add `match_counts`, `difference_total_cents` and `unmatched_deposits` (deposits of that kind no row accounts for). `exceptions_only=true` returns only rows that do not reconcile, which is how the settlement batches work.

### Historical cutoff

`ToolSession(root, tag, cutoff)` loads both databases read-only once per request and freezes them in memory: source changes during a run are not seen, and the next run sees them. Documents are filtered at the cutoff before anything is computed, and parent/child documents are filtered together. The Book (costs, line items, descriptions, credits, related records) is rebuilt from the filtered documents by `book_projection.py`.

- **Payment status.** A cost is `paid` only when a record dated on or before the cutoff shows the payment: payroll runs, owner draws, sales-tax paid dates, recorded invoice payments (when an `invoice_payments` table exists) and chargeback debits. Otherwise it is `pending` (recorded payments fall short), `scheduled` (a lease or loan obligation due after the cutoff) or `unknown`. Autopay assumptions and current open AP are never used. A status-filtered `search_costs` reports `omitted_unknown_status`.
- **Bank bills.** Nessie's bills are current state with no dated snapshot, so `get_bank_bills` returns `status: "unavailable"`.
- **Snapshot-only state.** Open AP and deposits in transit, including cash not yet deposited, are omitted.
- **Future facts.** Dispute outcomes, debit dates and tax payment dates after the cutoff are hidden. Contractual due dates and installment plans stay. `expected_settlement_date` and `expected_payout_date` are estimates from the agreements, and `expected_after_cutoff` marks those not yet due.
- **Delivery weeks.** POS tickets are cut off before weekly aggregation; weeks with `complete_week: false` are partial.
- **Reference data.** Agreements, supplier terms, leases, loans and recipes have no version history, so their validity at the cutoff is assumed.

Cash is calculated by one function shared by the report and `get_account_balances`: opening balances plus completed deposits minus completed purchases and withdrawals, from each account's opening date through `as_of`. Internal transfers affect each account but cancel in the combined balance. Pending transactions and proposed corrections or refunds do not change cash. `get_account_balances` rejects dates after the cutoff or before the opening balances.

Traces record `toolset` (version and groups) and `sources` (snapshot paths, bank rows, cutoff, window start), which resume reuses without reopening the databases.

To swap in production data sources, pass a snapshot provider to `Bookkeeper(..., snapshots=...)` that exposes `call(name, arguments)`, source paths, `as_of`, `start`, `bank_rows`, `limitations`, `balances()` and `close()`, and keep the 11 model-facing schemas unchanged. This version still reads only the local snapshots; it adds no live API access or verification.

The standalone `nessie_lab/bookkeeper_tools.py` is a compatibility wrapper around the same implementation in legacy mode: synthetic `T…` bank ids, the Book's own statuses, no cutoff, Claude-format `TOOLS` and `ToolSession(max_calls=8)`. That mode is never used by Bookkeeper.

## Report contract and verification

`contracts.py` defines schema version 1.1, extending `bookkeeper_report_format.md` with `request_id`, `balances`, and `limitations`. The model supplies findings, coverage, summaries and cleared items. The application supplies source metadata, trace IDs, balances and verification state.

Pydantic checks structure and types only. No financial/evidence verification is performed, coverage is model-reported, and `verification.passed` is always `false` (`checked_at: null`, zero findings checked/rejected). These reports are not yet eligible for a Forecaster that requires verified inputs. The independent verifier and its three-round comparison process remain future work.

## Tests and grading export

```bash
uv run --project bookkeeper pytest bookkeeper/tests -q
uv run --project bookkeeper bookkeeper export-flags \
  data/reports/R-REPORT.json data/flags_bookkeeper.json
```

The offline export translates native Nessie IDs to the grading IDs already in the sync snapshot. For `contracts.from_bk_report`, `bookkeeper_tools.bank_id_map()` maps Nessie ids from both the committed `nessie_manifest_<tag>.json` and the local sync. If the snapshot's Nessie ids are not the committed manifest's (a different Nessie load), the run logs a warning and the report says so in its limitations: converting it then needs that machine's sync. It never reads the answer key. For `exclude`/`correct_to` findings it exports treatment-target IDs rather than every evidence row, so a legitimate original payment is not flagged alongside its duplicate. Run `grade.py` separately if desired; grading is not part of the agent's generation process.

Tests use a scripted model and small deterministic fixtures, plus optional read-only checks on local generated databases. A live model run requires an ASI API key and incurs API usage.
