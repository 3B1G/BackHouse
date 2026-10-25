# Bookkeeper report: JSON format

This report is how the Bookkeeper's findings reach the Forecaster, the Controller, the owner and the audit log.

## How it moves

```
Bookkeeper ──► checks.py ──► saved verified report (report_id, as_of)
                                        │
Controller: "forecast this scenario using report R-2026-10-24-01"
                                        │
                                        ▼
               Forecaster ──load_history(as_of, report_id)──► reads saved report
```

- **The Bookkeeper produces it.** `checks.py` verifies it, then **saves** it, e.g. `data/reports/R-2026-10-24-01.json` or a `bookkeeper_reports` SQLite table.
- **The Forecaster reads the saved file itself.** The Controller (the AI) only passes the `report_id`, never the contents, so no number passes through Claude.
- **The daily refresh** runs `nessie_sync` → Bookkeeper → `checks.py` → save report → rebuild features. A "what if" question reuses the latest verified report and doesn't rerun the Bookkeeper.
- **If no verified report exists:** use the last good one and show its date, or run without corrections and show a warning.

Amounts are **integer cents**, matching the repo.

---

## Example report

```json
{
  "report_id": "R-2026-10-24-01",
  "schema_version": "1.0",
  "generated_at": "2026-10-24T08:05:12-07:00",
  "as_of": "2026-10-24",
  "window": {"start": "2025-10-25", "end": "2026-10-24"},
  "sources": {
    "nessie_tag": "boh-1loc",
    "bank_snapshot": "data/nessie_sync_boh-1loc.db",
    "bank_rows": 1263,
    "books_db": "data/books.db"
  },
  "bookkeeper": {"version": "0.3.0", "model": "claude-sonnet-5-5", "trace_id": "TR-8841"},

  "verification": {
    "passed": true,
    "checked_at": "2026-10-24T08:05:40-07:00",
    "findings_checked": 7,
    "findings_rejected": 0,
    "notes": []
  },

  "coverage": {
    "bank_rows_reviewed": 1263,
    "matched_to_documents": 1182,
    "investigated": 81,
    "cleared_as_ok": 6
  },

  "summary": {
    "errors": 6,
    "needs_owner": 1,
    "total_impact_cents": 0,
    "recoverable_cents": 0,
    "ongoing_monthly_cents": 45200
  },

  "findings": [
    {
      "id": "F1",
      "root_cause": "duplicate_charge",
      "verdict": "error",
      "confidence": 0.97,
      "counterparty": "Bay Produce Co",
      "dates": {"first": "2026-04-20", "last": "2026-04-20"},
      "bank_ids": ["T…", "T…"],
      "books_refs": ["BPC-CHS-…"],
      "amounts": {"expected_cents": 0, "actual_cents": 0, "impact_cents": 0},
      "explanation": "Bay Produce Co debited twice on Apr 20 for one invoice.",
      "evidence": [
        {"type": "bank_row", "ref": "T…"},
        {"type": "bank_row", "ref": "T…"},
        {"type": "invoice", "ref": "BPC-CHS-…"}
      ],
      "treatment": {"kind": "exclude", "bank_ids": ["T…"]},
      "recoverable": true,
      "suggested_action": "Ask Bay Produce Co to refund the duplicate debit."
    },
    {
      "id": "F4",
      "root_cause": "card_fee_creep",
      "verdict": "error",
      "confidence": 0.95,
      "counterparty": "TableTap Payments",
      "dates": {"first": "2026-08-01", "last": "2026-10-24"},
      "bank_ids": ["T…", "…83 rows"],
      "books_refs": ["agreements: TableTap Payments"],
      "amounts": {"expected_rate": 0.026, "actual_rate": 0.029, "impact_cents": 0},
      "explanation": "Since Aug 1 the processor has charged 2.9% instead of the agreed 2.6% on every card batch.",
      "evidence": [
        {"type": "agreement", "ref": "TableTap Payments"},
        {"type": "computed", "ref": "implied fee rate per batch, 83 batches"}
      ],
      "treatment": {"kind": "ongoing", "since": "2026-08-01", "monthly_cents": 45200},
      "recoverable": true,
      "suggested_action": "Dispute the rate with TableTap and request a credit."
    },
    {
      "id": "F6",
      "root_cause": "unrecorded_charge",
      "verdict": "needs_owner",
      "confidence": 0.80,
      "counterparty": "TableTap POS",
      "dates": {"first": "2026-07-09", "last": "2026-07-09"},
      "bank_ids": ["T…"],
      "books_refs": [],
      "amounts": {"expected_cents": 0, "actual_cents": 34900, "impact_cents": 34900},
      "explanation": "A $349.00 'hardware protection' charge with no bill, invoice or agreement behind it.",
      "evidence": [
        {"type": "bank_row", "ref": "T…"},
        {"type": "search", "ref": "no matching invoice, bill or agreement"}
      ],
      "treatment": {"kind": "exclude", "bank_ids": ["T…"]},
      "assumption_if_unconfirmed": "Treated as a one-off; not repeated in the forecast.",
      "recoverable": "unknown",
      "suggested_action": "Ask the owner whether this was an approved purchase."
    },
    {
      "id": "F7",
      "root_cause": "missed_payment",
      "verdict": "error",
      "confidence": 0.93,
      "counterparty": "Sonoma Dairy Supply",
      "dates": {"first": "2026-10-…", "last": "2026-10-…"},
      "bank_ids": [],
      "books_refs": ["SDS-CHS-…"],
      "amounts": {"expected_cents": 0, "actual_cents": 0, "impact_cents": 0},
      "explanation": "Invoice is past due in the books, but no payment ever left the bank.",
      "evidence": [
        {"type": "invoice", "ref": "SDS-CHS-…"},
        {"type": "search", "ref": "no matching debit"}
      ],
      "treatment": {"kind": "owed", "cents": 0, "due": "2026-10-…"},
      "recoverable": false,
      "suggested_action": "Pay Sonoma Dairy Supply now to avoid late fees."
    }
  ],

  "cleared": [
    {"bank_ids": ["T…"], "reason": "Supplier refund; matches credit memo CM-2207."},
    {"bank_ids": ["T…"], "reason": "Large oven repair; documented by repair invoice RPR-BIG-1."}
  ]
}
```

The `T…`, `…` and `0` values are placeholders: fill them from the real data. The $349.00 charge and the July 9 and Aug 1 dates match the data. $452 a month for the card fee is the current estimate from `scenario_check.py`.

---

## What each section is for

| Section | Purpose | Main reader |
|---|---|---|
| `report_id`, `as_of`, `window`, `sources` | Which data it covers and which Nessie snapshot, so the forecast can say "based on bookkeeping as of Oct 24." Needed for the backtest cutoff. | Forecaster, audit log |
| `bookkeeper` | Version, model and `trace_id`, linking to the full record of tool calls | checks.py, audit log |
| `verification` | Whether `checks.py` passed it. **The Forecaster only uses reports with `passed: true`.** | Forecaster |
| `coverage` | How much was reviewed: proof of thoroughness for judges | Dashboard, pitch |
| `summary` | Totals for the dashboard headline | Controller, dashboard |
| `findings[]` | One entry per **root cause**, not per row. The 83 card-fee rows are one finding. | Everyone |
| `cleared[]` | Suspicious-looking items checked and found fine (the decoys). Shows it didn't just flag everything. | Dashboard, pitch |

## Fields inside each finding

| Field | Meaning |
|---|---|
| `id` | Stable ID (`F1`…) so the Controller, owner decisions and audit log can refer to it |
| `root_cause` | Fixed list: `duplicate_charge`, `supplier_overcharge`, `commission_overcharge`, `card_fee_creep`, `cash_deposit_short`, `unrecorded_charge`, `missed_payment` |
| `verdict` | `error` or `needs_owner` (`ok` items go in `cleared`) |
| `confidence` | 0–1. Below a threshold (say 0.7), `checks.py` downgrades it to `needs_owner` |
| `bank_ids`, `books_refs` | Exactly which bank rows and documents are involved |
| `amounts` | Expected, actual and impact in cents (or rates for fee problems). `checks.py` recomputes these. |
| `explanation` | Plain English for the owner. Every number in it must match `amounts`. |
| `evidence[]` | What was looked at. `checks.py` confirms each item appears in the trace. |
| `treatment` | **For the Forecaster.** See the next table. |
| `recoverable` | Could the money come back? Used by the "with fixes" scenario. |
| `suggested_action` | For the Controller to offer the owner |
| `assumption_if_unconfirmed` | For `needs_owner` items: what the forecast assumes until the owner answers |

## Treatments: how the Forecaster uses each finding

| `treatment.kind` | Fields | What the Forecaster does | Examples |
|---|---|---|---|
| `exclude` | `bank_ids` | Leaves these rows out when learning patterns | Duplicate supplier charge, the one-off $349 charge |
| `correct_to` | `bank_id`, `cents` | Learns from the corrected amount | Supplier $100 over the invoice, cash deposit $200 short, 3 weeks of 20% delivery commission |
| `ongoing` | `since`, `monthly_cents` | Keeps costing money in the base forecast; stops in "with fixes" | Card fee 2.9% instead of 2.6% since Aug 1 |
| `owed` | `cents`, `due` | Adds money about to go out | The invoice that was never paid |

**The report never changes today's cash.** The bank balance is real. Findings only change what the Forecaster learns and what it expects next. Possible refunds belong in the "with fixes" scenario, not the base case.

---

## Rules
1. **Saved reports never change.** Owner decisions ("yes, that $349 was approved") go in the audit log against the finding ID. The next Bookkeeper run produces a new report that includes them.
2. **It still works with `grade.py`.** Add a small export that turns findings into the flag list `grade.py` expects (`{"bank_id": ...}` or `{"books_ref": ...}`), so every report can be scored against the answer key.
3. **Define it once in `contracts.py`** as pydantic models, so the Bookkeeper can't write an invalid report and the Forecaster can't misread one.
4. **Keep the backtest honest:** for a backtest at `as_of = 2026-07-24`, the Bookkeeper runs on data up to that date only.
5. **Never use the answer key** (`data/grader/`) or the `|ref=` tags to build a report.

## Implemented Bookkeeper V1 (schema 1.1)

The `bookkeeper/` subproject implements the producer only. The independent
Bookkeeper-verifier and the planned three-round comparison loop are future work.
V1 saves reports without evidence/financial verification: `verification.passed`
is always `false`, `checked_at` is `null`, `findings_checked` and
`findings_rejected` are zero, and `notes` states that verification was not run.
Pydantic enforces the output shape only. Do not use these as verified Forecaster inputs.

The contract adds:

- `request_id`: the orchestrator's request identifier. Reports use a stable unique
  `R-<hash>` ID derived from sender and request ID. A new request ID creates a new report.
- `limitations`: disclosures about coverage, historical reference data and missing capabilities.
- `balances`: `as_of`, `accounts`, `total_cents`, and `basis`. Each account contains
  `account`, `opening_as_of`, `opening_balance_cents`, `inflow_cents`,
  `outflow_cents`, and `balance_cents`. Balances include only completed bank movements
  from the opening date through `as_of`, with opening balances interpreted as before
  that day's transactions. Findings and possible refunds never adjust this cash.

`bank_ids` in V1 refer to native Nessie transaction IDs (`nessie_id`), not the
synthetic `|ref=` values. Only the separate grading export maps them back to the
IDs expected by `grade.py`. No answer key is accessed during report generation.
Snapshot `sources.bank_rows` counts all bank rows through `as_of`; `coverage` is
model-reported and has not been independently checked. `summary.total_impact_cents`
uses adverse cash differences (expected inflow minus actual inflow, or actual
outflow minus expected outflow), with unpaid obligations positive as owed.

The two simulated tools read the saved Nessie snapshot and `books.db`. The default
`as_of` is the latest bank transaction date; no claim of live coverage is made.
Invoice pending-versus-paid reconciliation is future work until historical invoice
statuses/payment records are available. The agent does not invent those fields.
