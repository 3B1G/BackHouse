# Bookkeeper

Two ASI-powered uAgents that investigate restaurant records through 11 read-only domain tools over local snapshots. **Bookkeeper** investigates and saves a draft report. **Verifier** repeats the same investigation independently, compares its findings with the draft, and publishes the report, containing only the findings both agree on. Bookkeeper returns the published filename to the requesting orchestrator. Neither agent refreshes Nessie, modifies the books or runs Monte Carlo.

```
Orchestrator ──request──► Bookkeeper: investigate → data/drafts/R-<key>.json
Bookkeeper ──{request_id, report_id, draft}──► Verifier: investigate again → compare → data/reports/R-<key>.json
Verifier ──filename, verified──► Bookkeeper ──filename, verified──► Orchestrator
```

## Setup

From the repository root:

```bash
uv python install 3.13
uv sync --project bookkeeper
uv run --project bookkeeper python --version
```

This subproject uses `bookkeeper/.venv` and its own lockfile. The parent project remains on Python 3.14. Hatch explicitly packages `src/bookkeeper`; the distribution name is `restaurant-bookkeeper`.

Set `ASI1_API_KEY` in your shell or the **repository root** `.env`. `ASI_MODEL` defaults to `asi1`; `asi1-mini` is faster and `asi1-ultra` is also available. See `.env.example` for all settings; never commit real credentials. Set `BOOKKEEPER_SEED` and `VERIFIER_SEED` (different values) before starting the agent servers.

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
  "verified": true
}
```

`run` drafts the report and then verifies it in the same process (no agents needed), so it costs two investigations. `verified` is `true` only if the Verifier agreed with every finding; see [Verification](#verification).

Files are immutable. The request key combines sender and request ID: redelivery returns the existing report, or re-sends a saved draft for verification without investigating again. Use a new request ID to request a fresh report. Reusing an ID with different parameters is an error. Drafts are under `data/drafts` and reports under `data/reports`; tool/model traces are under `data/bookkeeper_traces` and `data/verifier_traces`. Shared storage is required for the orchestrator to open the filename.

## Local orchestrator demo

Start the Verifier and Bookkeeper in two terminals:

```bash
uv run --project bookkeeper bookkeeper serve-verifier
uv run --project bookkeeper bookkeeper serve
```

Each agent finds the other's address from its seed in the shared `.env` (or from `BOOKKEEPER_VERIFIER_ADDRESS` / `VERIFIER_BOOKKEEPER_ADDRESS`), and the Verifier only accepts requests from that Bookkeeper. Copy the Bookkeeper address printed at startup. In a third terminal:

```bash
uv run --project bookkeeper bookkeeper client \
  --target agent1... --request-id local-demo-1
```

The client sends a standard `ChatMessage`, receives an immediate acknowledgement, then prints the final report response once the Verifier has published it. Stop it with Ctrl-C after receiving the result. The default ports are 8001 (Bookkeeper), 8002 (client) and 8003 (Verifier). For custom ports use `serve --port`, `serve-verifier --port`, `client --port --endpoint`, and `BOOKKEEPER_LOCAL_CLIENT_ENDPOINT`, `BOOKKEEPER_VERIFIER_ENDPOINT` and `VERIFIER_BOOKKEEPER_ENDPOINT` accordingly. Local mode uses explicit endpoint resolution and does not register the agents publicly. The demo client's fixed seed is only for localhost testing.

## ASI orchestrator integration

To bring the agents online on Agentverse, run from `nessie_lab/`:

```bash
./online.sh
```

It starts the Verifier, the Bookkeeper and the orchestrator in mailbox mode, prefixes their output with `[verifier]`, `[bookkeeper]` and `[orchestrator]`, prints the three addresses once they are up, and stops all of them cleanly on Ctrl-C, or as soon as one exits. It sets `<AGENT>_MAILBOX` only for the processes it starts, so with `.env` left at `false` the script is the only way they go online and a plain `serve` stays local. It refuses to start when port 8001, 8003 or 8005 is already in use. Keep the seeds stable: they fix the addresses, and each address has its own mailbox. The Bookkeeper's reply names its report file. With `AGENTVERSE_TOKEN` set, a mailbox Bookkeeper also attaches the report: it uploads it to Agentverse's file storage, lets only the requester read it, and the orchestrator saves it into its own `data/reports/` before reading it, so the orchestrator can run on another machine. Without the token, or if the upload fails, the reply only names the file, so the agents must share this folder's `data/` on one machine.

A mailbox agent advertises its Agentverse mailbox, not a local endpoint, and finds its peers through the Almanac, so every agent needs a mailbox. The script checks first (`bookkeeper mailbox-status BOOKKEEPER VERIFIER ORCHESTRATOR`). The first time a seed is used it needs `AGENTVERSE_TOKEN` (an Agentverse API key) in `.env`: it starts that agent briefly with its inspector on, creates the mailbox through the inspector's `POST /connect` (`bookkeeper mailbox-connect --port …`, or `python -m orchestrator.register` for the orchestrator), and restarts it without the inspector. The inspector routes (`/connect`, `/disconnect`, `/messages`) accept requests from any web origin, so a page open in your browser could connect the agent to another account while they are served; `<PREFIX>_INSPECTOR=true` turns them on only for that step. To connect by hand instead, start the agent with that flag, open the `Agent inspector available at …` link it logs while signed in to Agentverse, and choose **Connect**, **Mailbox**, **Finish**.

In mailbox mode, messages arrive through the mailbox. The agent's own server listens on 127.0.0.1 only and serves `GET /health`; without a REST route, uagents 0.26.1 stops a mailbox agent right after startup. Registration uses the Almanac API alone, with a 10-second timeout (uagents' 1-second default is shorter than the API's reply time); a mailbox agent needs no on-chain registration, which wants FET in the agent's wallet.

This external account setup is required before an ASI orchestrator can contact it; installing the package alone does not publish a reachable agent.

**Who it answers.** People talk to the orchestrator, which understands plain language and sends the Bookkeeper the request below. A mailbox Bookkeeper runs requests only from the orchestrator (`BOOKKEEPER_ORCHESTRATOR_ADDRESS`, or derived from `ORCHESTRATOR_SEED` when they share `.env`) and from `BOOKKEEPER_ALLOWED_SENDERS` (comma-separated, such as the owner's ASI:One address): each run is two investigations of ASI usage. Anyone else, and anyone but its Bookkeeper on the Verifier, is logged and gets one short note an hour, so two refusing agents can't loop. In local mode only local peers can reach it, as before.

Send a `ChatMessage` with one `TextContent` containing:

```json
{"request_id":"orchestrator-job-1","nessie_tag":"boh-1loc","as_of":"2026-07-24"}
```

`as_of` and `nessie_tag` are optional. The handler acknowledges the incoming message ID, retains the session context, sends the draft to the Verifier, and, when the Verifier replies, sends the same result shape as the CLI plus a `summary` a person can read (finding counts, total, recoverable and ongoing impact, the three largest findings), followed by `EndSessionContent`. A sender other than the orchestrator first gets a plain-text note that a new run has started and can take up to an hour; it is prose, not JSON, so the orchestrator's reply matcher could never mistake it for the result. Invalid requests, failed runs and failed verifications receive `status: "error"` and no filename (`trace_id` names the Bookkeeper's or the Verifier's trace). A request that is not a valid `ReportRequest` is answered with what is wrong (for example, which fields are unknown) and the accepted format, so ASI:One's model can correct it, and is logged with its sender and the first 200 characters of its text. Requests are serialized in each service process; multiple server processes of one agent sharing the output directory are not supported. Bookkeeper and Verifier must share the data directory.

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

**Hand-off rules.** Reports are consumed by the team's `nessie_lab/contracts.py` (`from_bk_report`), which drops findings it cannot convert. Bookkeeper therefore checks every batch result against the same rules: a finding cites evidence, names a bank row or a document, uses only bank ids the batch's tools returned, uses its root cause's treatment (below), gives positive `ongoing`, `correct_to` and `owed` amounts, and puts the document still owed in `books_refs` for an `owed` treatment. A result that breaks a rule, whether early or on the batch's last turn, gets one tools-disabled correction with the problems listed. Findings that still break a rule are left out, with a "Left out a … finding (…): …" limitation and `dropped_findings` in the trace. The report schema is unchanged.

**Treatment by root cause.** Each root cause has exactly one treatment (`TREATMENT` in `batches.py`, stated in the prompt), so the Bookkeeper and the Verifier describe the same issue the same way:

| Root cause | Treatment | Amount |
|---|---|---|
| `duplicate_charge`, `unrecorded_charge` | `exclude` | the erroneous rows only |
| `supplier_overcharge` | `correct_to` | the invoice amount |
| `commission_overcharge` | `correct_to` | the expected payout |
| `cash_deposit_short` | `correct_to` | the counted cash on the slip |
| `card_fee_creep` | `ongoing` | the current monthly excess, since the first affected date |
| `missed_payment` | `owed` | the unpaid document (first in `books_refs`) and its due date |

`correct_to` fixes exactly one bank row (`cents` is what the row should have been, not the difference). Several affected rows, such as three weeks of payouts, are one finding per row.

Each response may make at most 20 tool calls; any calls beyond that, or calls to a tool outside the batch's subset, are answered with a "not run" error (and do not count as evidence). Output limits are `ASI_MAX_TOKENS` (investigation turns, default 12000) and `ASI_REPORT_MAX_TOKENS` (result writing, default 32000). These are ordinary tool interactions, not verifier iterations; no result is revised once written. The server logs each batch's turns. The trace (`format: "batched/1"`) records the plan, each batch's conversation, result or reason, the token `usage` of every call, and the assembly. Failed runs retain a trace with an error message when source loading succeeded; the client receives a safe diagnostic and trace ID.

Every ASI reply is streamed (`stream: true`). `asi1` is a reasoning model: a reply can run
to tens of thousands of tokens and take minutes, so the timeouts bound the **silence between
streamed chunks** (including the wait for the first one), not a whole call: 120 seconds for
investigation calls and 600 seconds for result writing, configurable with
`ASI_READ_TIMEOUT_SECONDS` and `ASI_REPORT_TIMEOUT_SECONDS`. A reply that keeps producing
tokens never times out; a stalled one fails fast. Debug logging reports long replies every
30 seconds. Traces keep each reply's `reasoning_content` and token `usage`. A call that
stalls or loses its connection, even partway through a reply, is retried `ASI_RETRIES` times (default 2), waiting
`ASI_RETRY_BACKOFF_SECONDS` × attempt (default 5 s) in between; the run fails only if every
attempt does. Retries are logged and recorded as `retries` on the call's trace event.
HTTP 429, 500, 502, 503 and 504 (the server's transient failures) are retried the same way; other HTTP errors, such as 401 for a bad key, and output-limit cut-offs are not.

If batches' result-writing calls fail with a transport error, resume them without repeating
the investigation:

```bash
uv run --project bookkeeper bookkeeper resume --trace-id TR-<failed-trace-id>
```

This makes one writing call, with tools disabled, for each batch that failed at that step,
using its saved messages, then assembles the report from the saved batch results, dates
and cash balances. Batches that failed during investigation are listed as not reviewed,
because resume never reopens the databases or runs a tool. The CLI then verifies the
resumed draft like `run` (the Verifier's own investigation does open the databases). The original failed trace remains unchanged; the new trace references
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

To swap in production data sources, pass a snapshot provider to `Bookkeeper(..., snapshots=...)` that exposes `call(name, arguments)`, source paths, `as_of`, `start`, `bank_rows`, `limitations`, `balances()` and `close()`, and keep the 11 model-facing schemas unchanged. This version still reads only the local snapshots; it adds no live API access.

The standalone `nessie_lab/bookkeeper_tools.py` is a compatibility wrapper around the same implementation in legacy mode: synthetic `T…` bank ids, the Book's own statuses, no cutoff, Claude-format `TOOLS` and `ToolSession(max_calls=8)`. That mode is never used by Bookkeeper.

## Report contract and verification

`contracts.py` defines schema version 1.2: 1.1 (`bookkeeper_report_format.md` plus `request_id`, `balances` and `limitations`) with a `verifier` block, and a `verification` block that is now filled in. Saved 1.1 reports still load. The model supplies findings, coverage, summaries and cleared items. The application supplies source metadata, trace IDs, balances and verification state. Pydantic checks structure and types only.

### Verification

The Verifier (`verifier.py`) gets the draft's filename, never its findings, and runs the same batched investigation at the draft's cutoff with its own prompt intro, model (`VERIFIER_MODEL`, default `ASI_MODEL`) and temperature (`VERIFIER_TEMPERATURE`, default 0). Then, in code:

1. **Snapshot.** The cutoff, window start, bank row count and cash balances must equal the draft's. If they differ, the snapshot changed between the runs: nothing is published and the request fails.
2. **Pairing** (`compare.py`). Each Bookkeeper finding is paired with at most one Verifier finding with the same root cause, preferring shared bank rows (including treatment targets), then shared documents, then the same counterparty.
3. **Agreement.** A pair agrees when the treatment kind matches and:
   - `exclude` names the same rows
   - `correct_to` names the same row
   - `owed` names the same first document
   - `ongoing` starts within 7 days
   - cents (and `impact_cents`) agree within $10 or 5%, whichever is larger
   
   Cleared items, coverage and wording are not compared.
4. **Revision rounds.** Every disagreement is sent back as feedback to the Bookkeeper batch it came from:
   - a disputed Bookkeeper finding (no match, a different amount, rows or treatment, or a bank id not in the snapshot), with the reason and the Verifier's version
   - an issue only the Verifier found, as "you may have missed this"

   The Verifier replies with a `RevisionRequest` instead of publishing. The Bookkeeper redoes only those batches, with tools and with the feedback and its previous result in the prompt, reuses every other batch result, and sends draft round 2 (`data/drafts/R-….r2.json`). It must confirm everything with its own tools: the hand-off rules still apply. The Verifier never investigates again; it compares each new draft with its saved investigation (`data/verifier_drafts/`). Rounds stop when nothing is disputed, when a revision leaves the disagreements exactly as they were (each round record keeps a `disputes` fingerprint; the report's notes say it stopped early), or after `VERIFIER_MAX_ROUNDS` comparisons (default 3).
5. **Publishing: the Verifier wins.** Agreed findings are published as the Bookkeeper wrote them, with the Verifier's verdict. Whatever is still disputed after the last round is settled in the Verifier's favour:
   - its version of a disputed finding is published
   - its findings the Bookkeeper never reported are published
   - Bookkeeper findings it could not find are left out

   Findings are renumbered F1…Fn, and `verification.notes` and `limitations` say which came from the Verifier and what was left out. `verification.passed` is `true` on every published report, and `rounds` counts the comparisons. `findings_rejected` counts the Bookkeeper's findings not published as written.

Each round's comparison and the feedback sent are saved in `data/verifications/R-…/round-N.json`. The Verifier's trace (`data/verifier_traces/TR-….json`) holds its investigation and the Bookkeeper's trace ID. Every step is idempotent: a re-sent round returns the same next step, and a redelivered request resumes from the latest draft. If verification fails (API error, changed snapshot), nothing is published and the drafts stay: redeliver the request, or run `bookkeeper verify --report-id R-…`.

Two runs of the same model can share a blind spot; agreement means the two investigations reproduce each other, not that the answer key was consulted (it never is).

## Tests and grading export

```bash
uv run --project bookkeeper pytest bookkeeper/tests -q
uv run --project bookkeeper bookkeeper export-flags \
  data/reports/R-REPORT.json data/flags_bookkeeper.json
```

The offline export translates native Nessie IDs to the grading IDs already in the sync snapshot. For `contracts.from_bk_report`, `bookkeeper_tools.bank_id_map()` maps Nessie ids from both the committed `nessie_manifest_<tag>.json` and the local sync. If the snapshot's Nessie ids are not the committed manifest's (a different Nessie load), the run logs a warning and the report says so in its limitations: converting it then needs that machine's sync. It never reads the answer key. For `exclude`/`correct_to` findings it exports treatment-target IDs rather than every evidence row, so a legitimate original payment is not flagged alongside its duplicate. Run `grade.py` separately if desired; grading is not part of the agent's generation process.

Tests use a scripted model and small deterministic fixtures, plus optional read-only checks on local generated databases. A live model run requires an ASI API key and incurs API usage.
