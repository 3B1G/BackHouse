# Orchestrator: owner review before forecasting

**Summary.** The orchestrator should run the steps in a fixed order: audit, owner review, forecast, suggestions. Code
enforces that order; the prompt doesn't. The forecaster becomes a tool the orchestrator calls with adjustments it built
from reviewed findings. The forecaster never reads a Bookkeeper report itself. This note describes the loop, how it
differs from `origin/orchestrator` (a5b4ff7), and the problems it raises.

## The loop

1. The orchestrator asks the Bookkeeper for an audit.
2. The orchestrator reads the report and decides what the owner needs to see. The owner either adds a note to a
   finding (explains or corrects it) or accepts it.
3. Once every finding has a decision and the decisions are committed, the orchestrator calls the forecaster.
4. After the forecast, the owner can ask for suggestions. That's where the market scout and the later steps come in.

## How this differs from the orchestrator branch today

- **Every tool is offered on every turn.** `forecast_base` and `forecast_scenario` can run before anyone has looked at
  the findings.
- **Findings are filtered silently.** The owner is never asked. `checks_interim.py` treats every finding as "waiting
  for the owner" and passes only cautious adjustments (money owed, ongoing overcharges) to the forecast.
- **The interim checks were written "until a verifier exists".** The Verifier is now on `main` (PR #14). The latest
  report, `R-06aa51d1…` (as of 2026-10-23), passed verification. It has 11 findings: 8 `error`, 3 `needs_owner`.
  `contracts.from_bk_report()` reads it (report format 1.2) cleanly: 11 findings, 11 adjustments, nothing rejected.
- **What the branch already gets right:** it builds adjustments itself and passes them to `run_forecast(...,
  adjustments=...)`, with `[]` when there is no report. On `main`, `forecaster.py` still looks for the latest
  verified report on its own when `adjustments` is `None` (`load_report()`). We're removing that (see below).

## Proposed design

| Stage | Unlocks when | What the LLM can do |
|---|---|---|
| 1. Audit | always | request a Bookkeeper report |
| 2. Review | a verified report is in | explain findings, ask the owner, propose a decision per finding |
| 3. Commit | every finding has a proposed decision | nothing: the owner confirms (card click / `changes approve`) |
| 4. Forecast | the review is committed and the data hasn't changed since the audit | base and scenario forecasts |
| 5. Suggest | a committed base forecast exists | market scan, menu, `propose_fix`, ... |

- The stage lives in the orchestrator's session state, and each stage's tools only exist in that stage. An LLM can't
  call a tool it wasn't given.
- **The LLM proposes, the owner commits.** Finding text quotes bank memos, which an outsider can write ("TABLETAP POS
  HARDWARE PROTECTION"). The LLM shouldn't be the one recording "accepted" on the owner's behalf.
- Decisions per finding:
  - **accept:** the finding's adjustment goes into the forecast.
  - **dismiss** (with the owner's note): no adjustment.
  - **revise:** the owner's note goes back to the Bookkeeper as context for a new report, which is verified and
    reviewed again.
- Plain code turns `(report, decisions)` into the adjustments. The LLM never writes an adjustment or a money figure
  (the rule in `contracts.py`).

Sketch of the new contract (a proposal; field names are open):

```python
class FindingDecision(Contract):
    report_id: str
    finding_id: str
    decision: Literal["accept", "dismiss", "revise"]
    note: str | None = None             # the owner's words, kept as written
    decided_by: str = "owner"
    decided_at: datetime

def adjustments_for(report, decisions) -> list[Adjustment]:
    """Accepted findings -> their adjustments. Refuses if any finding has no decision."""
```

`passed_adjustments()` can stay as the mechanism underneath, with accept counting as "pass" and dismiss as "reject".
`checks_interim.py` is then no longer needed.

### Forecaster changes (separate from the orchestrator)

- Remove `load_report()`, the `--report` flag, and report files from the cache key. `adjustments=None` and `[]` both
  mean "history as the bank shows it".
- Take only `contracts.Adjustment`s, applied through the existing `report_from_adjustments()` path. Drop the
  forecaster's own reader for the raw report format (`apply_report` on raw findings). `from_bk_report()` is then the
  only code that reads reports.
- The caller passes `as_of` (the report's) and a provenance label (report id + decision set). The forecaster records
  it in the result.
- The CLI takes `--adjustments file.json` for running it by hand.

## Problems the loop raises

1. **Fixing a finding can count it twice.** F8 says "pay Sonoma Dairy $1,276.62 now." If the owner pays it and the
   Nessie copy is re-synced, the bank shows the payment, and the `owed` adjustment adds it again. Adjustments are only
   valid against the data the audit read. The report's `sources` names the database file and row count, but has no
   fingerprint of the data. **Proposal:** the Bookkeeper records a snapshot hash. If the data changed since the
   audit, the forecast stage stays locked until a new audit.
2. **The dates already don't line up.** The report is as of 2026-10-23 (the last transaction). The forecaster's
   default as-of date is 2026-10-24 (the day the books close), so a default forecast covers one day the audit didn't.
   The orchestrator should always pass the report's `as_of`.
3. **Owner notes that change a number.** "The pasta increase was agreed" is a plain dismissal. "The right price is
   $3.25/lb" introduces a new dollar figure, which the LLM must not turn into an adjustment. That goes back to the
   Bookkeeper as a revision.
4. **A new audit flags dismissed items again.** Finding IDs (`F9`) are per run, so the next audit will flag the $349
   TableTap charge again. Options:
   - record the dismissal in the books (e.g. as a documented cost, written through CH), so the charge is no longer
     undocumented;
   - key decisions on something that survives a new run (counterparty + bank IDs + book refs) and give earlier
     decisions to the Bookkeeper as context.
5. **Accepting is not fixing.** Accepting F8 means "the bill is still owed", and the forecast should keep it.
   Actually paying it, or disputing the F6 duplicate charge, is a CH Suggestion (`pay_bill` and `dispute_charge`
   already exist). The forecast models "what if we fix it" with `Scenario.fix_findings`.

## Open questions

1. **Which findings need the owner?** Proposal: show the findings the Verifier confirmed as `error` as one batch the
   owner accepts in a single action, and ask about the `needs_owner` findings one at a time.
2. **Where are decisions committed?** Proposal: in CH's signed, append-only records, with one passphrase approval per
   review. Decisions change what the forecast believes, so they deserve the same protection as a price change.
3. **Who does what?** Proposal: the forecaster changes, `FindingDecision` and `adjustments_for()` in `contracts.py`,
   and the Bookkeeper's snapshot hash on one side. The stages and owner-review flow on the orchestrator branch on the
   other. Happy to discuss.
