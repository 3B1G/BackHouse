"""What every piece of Back of House hands to the next. Build to these shapes; change them only together.

Flow:
  baseline_reconciler.py --SuspiciousItem--> Bookkeeper agent (bookkeeper_tools) --Finding--> checks.py --CheckResult-->
  Finding --Adjustment--> Forecaster: run_forecast(Scenario, adjustments, ForecastSettings) --> ForecastResult
  Market scout --CompetitorPrice--> MarketSummary
  Controller --Suggestion--> owner --Approval--> executor.py (the only code that writes) --> ExecutionResult
  every step --AuditEntry--> audit.py (append-only)

Conventions
  money   integer cents; field names end in _cents (except SuspiciousItem, which mirrors the reconciler's JSON)
  dates   datetime.date, YYYY-MM-DD in JSON
  ids     bank rows: bank_id (T + 9 hex). Book documents: the books' own ids (MDG-CHS-10053, RENT-CHS-2026-10, ...)
Unknown fields are rejected, so a typo fails at the hand-off instead of during the demo.
JSON: Model.model_validate_json(text) and obj.model_dump_json(). Between uagents, send model_dump_json() in a chat
message, or subclass uagents.Model with the same fields (uagents also uses pydantic 2).
The examples in __main__ are made up. Never put real answers (ids of planted errors) into an agent prompt.
"""
import json
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BANK_ID = r"^T[0-9A-F]{9}$"
MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"
ToolName = Literal["get_bank_transaction", "search_bank_transactions", "get_payee_history", "get_bank_bills",
                   "get_account_balances",                                                    # bank (Nessie)
                   "search_costs", "get_cost", "get_agreements", "get_card_batches", "get_cash_deposit_slips",
                   "get_delivery_payouts"]                                                    # book (books.db)
BANK_TOOLS = {"get_bank_transaction", "search_bank_transactions", "get_payee_history", "get_bank_bills", "get_account_balances"}


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- stage one: bookkeeping
class SuspiciousItem(Contract):
    """baseline_reconciler.py -> Bookkeeper agent. One row of data/flags_baseline.json, same field names."""
    type: Literal["missing", "unexpected", "variance"]
    bank_id: str | None = Field(None, pattern=BANK_ID)    # None for "missing": the bank never saw it
    books_ref: str | None = None                          # None for "unexpected": no document behind it
    expected: int                                         # cents the books imply (0 for "unexpected")
    actual: int                                           # cents the bank shows (0 for "missing")
    memo: str | None = None


class Evidence(Contract):
    """One fact a finding rests on. checks.py looks for `ref` in the output of a `tool` call in the agent's trace."""
    tool: ToolName
    ref: str                                              # the bank_id or cost_id the fact is about
    fact: str                                             # "no Book cost within $10 of $349.00 for this payee"
    amount_cents: int | None = None

    @property
    def source(self):
        return "bank" if self.tool in BANK_TOOLS else "book"


class Finding(Contract):
    """Bookkeeper agent -> checks.py -> Controller. One finding per root cause; many bank rows can share one."""
    finding_id: str                                       # "F1", "F2", ... unique within a run
    issue_type: Literal["duplicate_charge", "overcharge", "fee_rate_change", "commission_overcharge", "short_deposit",
                        "missed_payment", "undocumented_charge", "timing_only", "documented", "other"]
    verdict: Literal["error", "ok", "needs_owner"]
    root_cause: str                                       # one line: "card fee charged at 2.9% instead of 2.6% since Aug 1"
    bank_ids: list[str] = []
    books_refs: list[str] = []
    amount_at_issue_cents: int = Field(ge=0)              # money affected so far
    monthly_impact_cents: int | None = Field(None, ge=0)  # if it keeps happening until fixed
    evidence: list[Evidence] = Field(min_length=1)
    explanation: str                                      # plain English for the owner; its numbers must be in evidence
    suggested_action: str | None = None                   # "dispute with the processor", "pay invoice X"
    confidence: float = Field(ge=0, le=1)

    @field_validator("bank_ids")
    @classmethod
    def _bank_ids(cls, v):
        bad = [b for b in v if not (len(b) == 10 and b[0] == "T" and all(c in "0123456789ABCDEF" for c in b[1:]))]
        if bad:
            raise ValueError(f"not bank_ids: {bad}")
        return v

    @model_validator(mode="after")
    def _names_something(self):
        if not self.bank_ids and not self.books_refs:
            raise ValueError("a finding must name at least one bank_id or books_ref")
        return self


class CheckResult(Contract):
    """checks.py verdict on one agent output, before anything reaches the owner."""
    subject: Literal["bookkeeper", "market_scout", "controller"]
    ref: str                                              # finding_id, our_item or suggestion_id
    status: Literal["pass", "downgrade", "reject"]        # downgrade = verdict becomes needs_owner
    reasons: list[str] = []


class Adjustment(Contract):
    """Bookkeeper -> Forecaster: how a confirmed finding changes the cash forecast."""
    kind: Literal["exclude_row", "ongoing_overcharge", "unpaid_bill"]
    finding_id: str
    bank_id: str | None = Field(None, pattern=BANK_ID)    # exclude_row: the bad bank row to leave out of the history
    cost_id: str | None = None                            # unpaid_bill: the Book cost still owed
    amount_cents: int | None = Field(None, gt=0)          # unpaid_bill: amount still to pay
    due_date: date | None = None                          # unpaid_bill: when it should go out (past = pay now)
    monthly_cents: int | None = Field(None, gt=0)         # ongoing_overcharge: extra cost per month until fixed
    recoverable: bool = False                             # ongoing_overcharge: the "with fixes" scenario removes it

    @model_validator(mode="after")
    def _fields_for_kind(self):
        need = {"exclude_row": ["bank_id"], "ongoing_overcharge": ["monthly_cents"],
                "unpaid_bill": ["cost_id", "amount_cents", "due_date"]}[self.kind]
        missing = [f for f in need if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.kind} needs {', '.join(missing)}")
        return self


# ---------------------------------------------------------------- forecaster (Monte Carlo)
class Scenario(Contract):
    """A what-if for run_forecast. Scenario() is the base case."""
    name: str = "base"
    repair_cents: int = Field(0, ge=0)                    # one-off shock, e.g. 480000 for a $4,800 fridge
    repair_date: date | None = None                       # default: the first forecast day
    cover_drop: dict[str, float] = {}                     # {"2027-01": 0.15} = 15% fewer covers that month
    price_changes_cents: dict[str, int] = {}              # {"Margherita Pizza": 2000}: new dine-in dinner price
    remove_items: list[str] = []                          # dishes taken off the menu
    recover_fees: bool = False                            # card fee back to the agreed rate
    supplier_stretch_days: dict[str, int] = {}            # {"Mission Dry Goods": 15}: pay this supplier N days later

    @field_validator("cover_drop")
    @classmethod
    def _months(cls, v):
        for m, x in v.items():
            if len(m) != 7 or m[4] != "-" or not (0 <= x <= 1):
                raise ValueError(f"cover_drop wants {{'YYYY-MM': fraction 0..1}}, got {m}: {x}")
        return v


class ForecastSettings(Contract):
    trials: int = Field(1000, gt=0)
    horizon_months: int = Field(24, gt=0)
    seed: int = 13


class ForecastResult(Contract):
    """run_forecast(scenario: Scenario, adjustments: list[Adjustment] | None = None,
                    settings: ForecastSettings | None = None) -> ForecastResult
    Run-out dates are percentiles over trials. None = beyond the horizon (that share of trials never hits $0)."""
    scenario: str
    as_of: date                                           # last bank date the forecast starts from
    starting_cash_cents: int
    run_out_worst: date | None                            # 1 in 10 trials run out earlier than this
    run_out_likely: date | None                           # median
    run_out_best: date | None                             # 1 in 10 trials last longer than this
    share_running_out: float = Field(ge=0, le=1)          # trials that hit $0 within the horizon
    safety_floor_cents: int                               # one payroll + one month's rent
    below_floor_likely: date | None                       # median date cash first drops below the floor
    trials: int
    horizon_months: int
    seed: int
    chart_path: str | None = None

    @model_validator(mode="after")
    def _ordered(self):
        far = date.max
        w, l, b = (d or far for d in (self.run_out_worst, self.run_out_likely, self.run_out_best))
        if not w <= l <= b:
            raise ValueError("need run_out_worst <= run_out_likely <= run_out_best (None = beyond the horizon)")
        return self


# ---------------------------------------------------------------- stage two: market scout
class CompetitorPrice(Contract):
    """Market scout: one price seen on a competitor's own menu (restaurant site or Google Maps, never delivery apps)."""
    our_item: str                                         # our dish, as named in menu_prices.item
    restaurant: str
    their_item: str                                       # the dish name on their menu
    price_cents: int = Field(gt=0)
    service: Literal["lunch", "dinner", "all_day", "unknown"]
    source_url: str
    source_type: Literal["restaurant_site", "google_maps", "other"]
    quote: str                                            # exact price text; checks.py confirms it is on the saved page
    distance_miles: float | None = Field(None, ge=0)
    match_confidence: float = Field(ge=0, le=1)
    retrieved_at: datetime


class MarketSummary(Contract):
    """Market scout -> Controller: one of our dishes against the neighborhood. Fills feat_item_month.competitor_median_price."""
    our_item: str
    our_price_cents: int = Field(gt=0)
    service: Literal["lunch", "dinner", "all_day"]
    prices: list[CompetitorPrice]
    competitor_median_cents: int | None                   # None until there are at least 3 prices
    price_gap_cents: int | None                           # ours minus the median: negative = we are cheaper

    @model_validator(mode="after")
    def _enough_prices(self):
        if len(self.prices) < 3 and self.competitor_median_cents is not None:
            raise ValueError("no median from fewer than 3 prices")
        return self


# ---------------------------------------------------------------- controller, owner, executor, audit
class Suggestion(Contract):
    """Controller -> owner. Nothing is applied without an Approval."""
    suggestion_id: str
    action: Literal["reprice_item", "remove_item", "dispute_charge", "pay_bill", "stretch_supplier", "other"]
    title: str                                            # "Raise Margherita Pizza from $18 to $20"
    target: str                                           # the dish, supplier, cost_id or bank_id it applies to
    new_value: int | None = None                          # reprice_item: new price in cents; stretch_supplier: extra days
    monthly_cash_effect_cents: int | None = None          # positive = more cash each month
    one_time_cash_effect_cents: int | None = None
    run_out_likely_before: date | None = None             # from ForecastResult runs without and with this change
    run_out_likely_after: date | None = None
    assumptions: list[str] = Field(min_length=1)          # "5% of Margherita orders lost at the new price"
    based_on: list[str] = []                              # finding_ids, our_item of a MarketSummary, scenario names


class Approval(Contract):
    suggestion_id: str
    decision: Literal["approved", "rejected"]
    decided_by: str = "owner"
    decided_at: datetime
    note: str | None = None


class Change(Contract):
    system: Literal["nessie", "menu_prices", "other"]
    ref: str                                              # Nessie bill id, dish name, ...
    field: str
    old_value: str | int | None
    new_value: str | int | None


class ExecutionResult(Contract):
    """executor.py, the only code that writes: what it changed for one approved suggestion."""
    suggestion_id: str
    applied: bool
    changes: list[Change] = []
    error: str | None = None


class AuditEntry(Contract):
    """One line of the append-only audit log (audit.py). Never edited or deleted."""
    at: datetime
    actor: Literal["reconciler", "bookkeeper", "verifier", "forecaster", "market_scout", "controller", "owner", "executor"]
    action: str                                           # "finding", "check", "forecast", "suggestion", "approval", "change"
    ref: str                                              # the id it is about
    summary: str
    data: dict = {}                                       # the full object: obj.model_dump(mode="json")


# ---------------------------------------------------------------- helpers
def load_suspicious(path=Path(__file__).parent / "data" / "flags_baseline.json"):
    """The reconciler's output as SuspiciousItems (the Bookkeeper agent's to-do list)."""
    return [SuspiciousItem.model_validate(x) for x in json.loads(Path(path).read_text())]


def findings_to_flags(findings):
    """Findings -> the flags grade.py scores. Errors and needs_owner count, "ok" does not. One flag per bank row;
    a books_ref flag only when the bank never saw the item (e.g. a missed payment), so references the grader
    doesn't know never become false alarms."""
    flags = []
    for f in findings:
        if f.verdict == "ok":
            continue
        flags += [{"bank_id": b, "finding_id": f.finding_id} for b in f.bank_ids]
        if not f.bank_ids:
            flags += [{"books_ref": r, "finding_id": f.finding_id} for r in f.books_refs]
    return flags


if __name__ == "__main__":
    from pydantic import ValidationError

    items = load_suspicious()
    print(f"reconciler output fits SuspiciousItem: {len(items)} items")
    now = datetime(2026, 10, 24, 12, 0)
    f = Finding(finding_id="F1", issue_type="overcharge", verdict="error", root_cause="Supplier debited $50 more than the invoice",
                bank_ids=["T0A1B2C3D4"], books_refs=["XYZ-CHS-00001"], amount_at_issue_cents=5000,
                evidence=[Evidence(tool="get_bank_transaction", ref="T0A1B2C3D4", fact="debit of $550.00", amount_cents=55000),
                          Evidence(tool="get_cost", ref="XYZ-CHS-00001", fact="invoice total $500.00", amount_cents=50000)],
                explanation="Example Supplier took $550.00 for a $500.00 invoice.", suggested_action="ask for a $50 refund",
                confidence=0.9)
    a = Adjustment(kind="ongoing_overcharge", finding_id="F2", monthly_cents=30000, recoverable=True)
    r = ForecastResult(scenario="base", as_of=date(2026, 10, 24), starting_cash_cents=10_000_000, run_out_worst=date(2027, 5, 1),
                       run_out_likely=date(2027, 7, 1), run_out_best=None, share_running_out=0.7, safety_floor_cents=5_000_000,
                       below_floor_likely=date(2027, 2, 1), trials=1000, horizon_months=24, seed=13)
    s = Suggestion(suggestion_id="S1", action="reprice_item", title="Raise Example Dish from $18 to $20", target="Example Dish",
                   new_value=2000, monthly_cash_effect_cents=60000, assumptions=["5% fewer orders at the new price"], based_on=["F1"])
    for obj in (f, a, r, s, Scenario(name="fridge", repair_cents=480000, cover_drop={"2027-01": 0.15})):
        assert type(obj).model_validate_json(obj.model_dump_json()) == obj
    print("examples round-trip through JSON; flags for grade.py:", findings_to_flags([f]))
    bad = [lambda: Finding(**{**f.model_dump(), "bank_ids": ["349.00"]}),
           lambda: Adjustment(kind="unpaid_bill", finding_id="F3"),
           lambda: Scenario(cover_drop={"January": 15}),
           lambda: Suggestion(**{**s.model_dump(), "assumptions": []}),
           lambda: Finding(**{**f.model_dump(), "amount": 5000})]
    for make in bad:
        try:
            make()
            raise SystemExit("a bad input was accepted")
        except ValidationError as e:
            print("rejected as expected:", e.errors()[0]["msg"][:90])
