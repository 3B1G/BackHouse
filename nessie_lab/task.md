# Replace Bookkeeper’s SQL tools with the 11 domain tools

## Summary

Make the existing 11 tools in `bookkeeper_tools.py` Bookkeeper’s default investigation interface. Preserve historical `--as-of` reports, native Nessie references, immutable reports, tracing, and resume behavior.

These tools will continue reading local Nessie and books snapshots. This migration does not introduce live API access or verification.

## Implementation

- Refactor the domain functions into a request-scoped tool session configured with `root`, Nessie tag, and cutoff. Freeze source data for each investigation; remove reliance on global caches, generator configuration, and process-wide path changes.
- Keep one maintained implementation in `bookkeeper_tools.py`, package it with the Bookkeeper distribution, and add pandas to Bookkeeper’s dependencies. Preserve existing standalone entrypoints as compatibility wrappers.
- Expose the 11 existing names using ASI’s function-tool schema format. Remove `query_nessie` and `query_books` from new model runs.
- Update the investigation prompt to explain searches, document lookups, settlement comparisons, pagination, units, and historical limitations.
- Retain the 40-turn budget, final writing turn without tools, and maximum 20 calls per response. Do not adopt the standalone demo’s eight-call budget.
- Require successful evidence retrieval from both bank and books tools; do not require calling all 11 tools. Error or unavailable responses do not count as successful access.

## Data and tool contracts

- Preserve the default cutoff of the latest bank transaction date and rejection of dates beyond coverage or before available opening balances.
- Filter source documents before computing results. Extract reusable, side-effect-free cost projection logic from `build_book.py`; reconstruct descriptions, credits, and related records from cutoff-filtered documents instead of trusting current-state derived text.
- Return unknown payment status where historical evidence is absent. Do not infer payment from autopay assumptions or current open AP. Status-filtered searches must disclose omitted unknown states.
- Keep all 11 tools available, but return an explicit unavailable result for bank-bill history when the source lacks a dated snapshot. Omit snapshot-only deposits-in-transit with a limitation.
- Filter POS tickets before weekly payout aggregation and label incomplete weeks. Hide future dispute outcomes and actual payment dates; retain contractual due dates and clearly identified expected settlement dates.
- Use native Nessie IDs throughout model-facing bank fields, including the existing `bank_id` argument. Strip synthetic IDs and `|ref=` markers from results and related records. Retain synthetic-ID translation only in offline grading and legacy standalone compatibility.
- Add `offset`, deterministic ordering, `truncated`, and `next_offset` to primary list results. Keep the 50-row maximum and existing defaults; totals describe all matches, not just the returned page. Bound supplementary lists and identify any truncation.
- Include cutoff and limitations in tool responses, preserve the 120 KB response cap, and validate arguments before dispatch.
- Use the existing completed-transaction balance calculation for both report balances and `get_account_balances`. Reject tool requests for balances after the investigation cutoff.

## Reports, recovery, and compatibility

- Keep report schema 1.1, CLI/request interfaces, filenames, idempotency rules, and `verified: false`.
- Record toolset version and source-group metadata in new traces. Resume uses saved messages, balances, source metadata, and results without reopening databases.
- Continue resuming eligible legacy SQL-tool traces by recognizing their two tool names. Existing saved reports remain reusable.
- Preserve standalone tool compatibility while keeping its synthetic-ID behavior isolated from Bookkeeper.
- Update documentation to describe the 11 tools, historical limitations, pagination, and unchanged local-snapshot data sources.

## Validation

- Test every tool with deterministic fixtures, including missing records, invalid arguments, empty results, and unavailable historical state.
- Test pagination beyond 50 matches, stable ordering, full-match totals, and bounded supplementary results.
- Test future credit memos embedded in descriptions, dispute outcomes, related records, partial delivery weeks, and unknown payment statuses.
- Verify native IDs and sanitized memos throughout tool results and traces; ensure no grader data is accessed.
- Verify completed-only balances, opening dates, internal transfers, and consistency between tool and report balances.
- Test request isolation across roots, tags, cutoffs, and source changes between runs.
- Run scripted end-to-end investigations covering both source groups, finalization, immutable reports, export, and resume for both new and legacy traces.
- Run the Bookkeeper suite and standalone tool checks, plus an installed-package check outside the repository directory. Use existing snapshots read-only; no live ASI run is required for acceptance.
