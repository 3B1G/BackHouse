"""What every piece of Back of House hands to the next. Build to these shapes; change them only together.

Flow:
  baseline_reconciler.py --SuspiciousItem--> Bookkeeper agent --report--> from_bk_report() --Finding, Adjustment-->
  checks.py --CheckResult--> passed_adjustments() --Adjustment--> Forecaster
  Forecaster: run_forecast(Scenario, adjustments, ForecastSettings) --> ForecastResult
  Market scout --CompetitorPrice--> MarketSummary
  Controller --Suggestion--> owner --Approval--> executor.py (the only code that writes) --> ExecutionResult
  every step --AuditEntry--> audit.py (append-only)

Rule
  Adjustments reach the Forecaster only from findings that passed checks.py, and code passes them along
  (passed_adjustments). The Controller (AI) never writes or edits an adjustment or a money figure: it refers to
  findings and forecasts by id, and every number it shows comes from a Finding or a ForecastResult.

Conventions
  money   integer cents; field names end in _cents (except SuspiciousItem, which mirrors the reconciler's JSON)
  dates   datetime.date, YYYY-MM-DD in JSON; months YYYY-MM
  ids     bank rows: bank_id (T + 9 hex), the only bank id in these shapes. A report that names Nessie's own ids is
          converted at the hand-off with bookkeeper_tools.bank_id_map(). Book documents: the books' own ids
          (MDG-CHS-10053, RENT-CHS-2026-10, ...)
Unknown fields are rejected, so a typo fails at the hand-off instead of during the demo.
JSON: Model.model_validate_json(text) and obj.model_dump_json(). Between uagents, send model_dump_json() in a chat
message, or subclass uagents.Model with the same fields (uagents also uses pydantic 2).
The examples in __main__ are made up. Never put real answers (ids of planted errors) into an agent prompt.
"""
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

BANK_ID = r"^T[0-9A-F]{9}$"
MONTH = r"^\d{4}-(0[1-9]|1[0-2])$"
ToolName = Literal["get_bank_transaction", "search_bank_transactions", "get_payee_history", "get_bank_bills",
                   "get_account_balances", "search_costs", "get_cost", "get_agreements", "get_card_batches",
                   "get_cash_deposit_slips", "get_delivery_payouts",                 # bookkeeper_tools.py
                   "query_nessie", "query_books"]                                    # the bk Bookkeeper's SQL tools


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _by_month(v, lo, hi, what):
    for m, x in v.items():
        if not re.fullmatch(MONTH, m) or not lo <= x <= hi:
            raise ValueError(f"{what} wants {{'YYYY-MM': {lo}..{hi}}}, got {m}: {x}")
    return v


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
    """One thing a finding rests on. checks.py looks for `ref` in the tool outputs recorded in the agent's trace."""
    type: Literal["bank_row", "invoice", "agreement", "document", "search", "computed"]   # same values as the bk report
    ref: str                                              # bank_id, document id, or what was searched or computed
    tool: ToolName | None = None                          # the tool call it came from, when known
    fact: str = ""                                        # "no Book cost within $10 of $349.00 for this payee"
    amount_cents: int | None = None


class Finding(Contract):
    """Bookkeeper -> checks.py -> Controller. One finding per root cause; many bank rows can share one."""
    finding_id: str                                       # "F1", "F2", ... unique within a run
    issue_type: Literal["duplicate_charge", "overcharge", "fee_rate_change", "commission_overcharge", "short_deposit",
                        "missed_payment", "undocumented_charge", "timing_only", "documented", "other"]
    verdict: Literal["error", "ok", "needs_owner"]
    root_cause: str                                       # one line: "card fee charged at 2.9% instead of 2.6% since Aug 1"
    counterparty: str | None = None
    first_date: date | None = None
    last_date: date | None = None
    bank_ids: list[str] = []
    books_refs: list[str] = []
    amount_at_issue_cents: int = Field(ge=0)              # money affected so far
    monthly_impact_cents: int | None = Field(None, ge=0)  # if it keeps happening until fixed
    evidence: list[Evidence] = Field(min_length=1)
    explanation: str                                      # plain English for the owner; its numbers must be in evidence
    suggested_action: str | None = None                   # "dispute with the processor", "pay invoice X"
    assumption_if_unconfirmed: str | None = None          # what the forecast assumes until the owner answers
    confidence: float = Field(ge=0, le=1)

    @field_validator("bank_ids")
    @classmethod
    def _bank_ids(cls, v):
        bad = [b for b in v if not re.fullmatch(BANK_ID, b)]
        if bad:
            raise ValueError(f"not bank_ids (T + 9 hex): {bad}")
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
    """Bookkeeper -> Forecaster, only for findings that passed checks.py: how a finding changes the cash forecast."""
    kind: Literal["exclude_row", "correct_row", "ongoing_overcharge", "unpaid_bill"]
    finding_id: str
    bank_id: str | None = Field(None, pattern=BANK_ID)    # exclude_row, correct_row: the bank row
    correct_amount_cents: int | None = Field(None, gt=0)  # correct_row: what the row should have been
    since: date | None = None                             # ongoing_overcharge: when it started (is it in the history already?)
    monthly_cents: int | None = Field(None, gt=0)         # ongoing_overcharge: extra cost per month until fixed
    recoverable: bool = False                             # ongoing_overcharge: Scenario.fix_findings can stop it
    cost_id: str | None = None                            # unpaid_bill: the Book cost still owed
    amount_cents: int | None = Field(None, gt=0)          # unpaid_bill: amount still to pay
    due_date: date | None = None                          # unpaid_bill: when it should go out (past = pay now)

    @model_validator(mode="after")
    def _fields_for_kind(self):
        need = {"exclude_row": ["bank_id"], "correct_row": ["bank_id", "correct_amount_cents"],
                "ongoing_overcharge": ["monthly_cents", "since"],
                "unpaid_bill": ["cost_id", "amount_cents", "due_date"]}[self.kind]
        missing = [f for f in need if getattr(self, f) is None]
        if missing:
            raise ValueError(f"{self.kind} needs {', '.join(missing)}")
        return self


# ---------------------------------------------------------------- forecaster (Monte Carlo)
class OneOff(Contract):
    cents: int = Field(gt=0)
    day: date | None = None                               # None = the first forecast day
    label: str                                            # "walk-in fridge compressor", "refund of a duplicate charge"


class Scenario(Contract):
    """A what-if for run_forecast. Scenario() is the base case. Every assumption used must appear in
    ForecastResult.assumptions, so the owner sees what the numbers rest on."""
    name: str = "base"
    one_off_costs: list[OneOff] = []                      # e.g. a $4,800 fridge repair
    one_off_income: list[OneOff] = []                     # e.g. a supplier refunding a duplicate charge
    cover_change: dict[str, float] = {}                   # {"2027-01": -0.15} = 15% fewer covers that month; -1..+1
    price_changes_cents: dict[str, int] = {}              # {"Margherita Pizza": 2000}: new dine-in dinner price
    remove_items: list[str] = []                          # dishes taken off the menu
    changes_start: date | None = None                     # when price and menu changes take effect; None = first forecast day
    price_response: float | dict[str, float] = 0.05       # share of a dish's orders lost when its price rises (gained on a cut):
                                                          # one value for every dish, or {dish: share}
    substitution: float = Field(0.7, ge=0, le=1)          # share of a removed dish's diners who order another dish instead
    fix_findings: list[str] = []                          # finding_ids whose recoverable ongoing overcharges stop
    supplier_stretch_days: dict[str, int] = {}            # {"Mission Dry Goods": 15}: pay this supplier N days later

    @field_validator("cover_change")
    @classmethod
    def _covers(cls, v):
        return _by_month(v, -1, 1, "cover_change")

    @field_validator("price_response")
    @classmethod
    def _response(cls, v):
        if any(not 0 <= x <= 1 for x in (v.values() if isinstance(v, dict) else [v])):
            raise ValueError("price_response is a share of orders, 0..1")
        return v

    @field_validator("price_changes_cents", "supplier_stretch_days")
    @classmethod
    def _positive(cls, v):
        if any(x <= 0 for x in v.values()):
            raise ValueError("values must be positive")
        return v


class ForecastSettings(Contract):
    trials: int = Field(1000, gt=0)
    horizon_months: int = Field(24, gt=0)
    seed: int = 13
    as_of: date | None = None                             # forecast as if it were this day (backtests); None = the day the books close


class MonthBand(Contract):
    month: str = Field(pattern=MONTH)
    worst_cents: int                                      # month-end cash, 1 in 10 trials lower
    likely_cents: int                                     # median
    best_cents: int                                       # 1 in 10 trials higher

    @model_validator(mode="after")
    def _ordered(self):
        if not self.worst_cents <= self.likely_cents <= self.best_cents:
            raise ValueError("need worst_cents <= likely_cents <= best_cents")
        return self


class VsBase(Contract):
    """How a scenario differs from the base case, so the Controller quotes numbers instead of computing them."""
    base_forecast_id: str | None = None
    days_moved_worst: int | None = None                   # positive = runs out later than the base case;
    days_moved_likely: int | None = None                  # None = beyond the horizon in one of the two runs
    days_moved_best: int | None = None
    days_moved_average: float | None = None
    cash_change_cents: int | None = None                  # cash difference on the base case's likely run-out day


class ForecastResult(Contract):
    """run_forecast(scenario: Scenario, adjustments: list[Adjustment] | None = None,
                    settings: ForecastSettings | None = None) -> ForecastResult
    Run-out dates are percentiles over trials. None = beyond the horizon (that share of trials never hits $0)."""
    forecast_id: str | None = None                        # lets checks.py and the audit log trace every number shown
    scenario: str
    as_of: date                                           # the day the books close (or settings.as_of); the forecast starts the next day
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
    assumptions: list[str] = []                           # every assumption behind these numbers, in plain English
    monthly_bands: list[MonthBand] = []                   # month-end cash for the dashboard chart
    vs_base: VsBase | None = None                         # None for the base case itself
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
    """Controller -> owner. Nothing is applied without an Approval. Numbers come from ForecastResults, never the AI."""
    suggestion_id: str
    action: Literal["reprice_item", "remove_item", "dispute_charge", "pay_bill", "stretch_supplier", "other"]
    title: str                                            # "Raise Margherita Pizza from $18 to $20"
    target: str                                           # the dish, supplier, cost_id or bank_id it applies to
    new_price_cents: int | None = Field(None, gt=0)       # reprice_item
    extra_days: int | None = Field(None, gt=0)            # stretch_supplier
    monthly_cash_effect_cents: int | None = None          # positive = more cash each month
    one_time_cash_effect_cents: int | None = None
    run_out_likely_before: date | None = None             # from the base and scenario ForecastResults (vs_base)
    run_out_likely_after: date | None = None
    assumptions: list[str] = Field(min_length=1)          # from the forecast that produced the numbers
    based_on: list[str] = []                              # finding_ids, forecast_ids, our_item of a MarketSummary

    @model_validator(mode="after")
    def _value_for_action(self):
        need = {"reprice_item": "new_price_cents", "stretch_supplier": "extra_days"}.get(self.action)
        if need and getattr(self, need) is None:
            raise ValueError(f"{self.action} needs {need}")
        return self


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


def passed_adjustments(adjustments, checks):
    """The rule in code: only adjustments whose finding passed checks.py reach the Forecaster."""
    passed = {c.ref for c in checks if c.subject == "bookkeeper" and c.status == "pass"}
    return [a for a in adjustments if a.finding_id in passed]


BK_ISSUE = {"duplicate_charge": "duplicate_charge", "supplier_overcharge": "overcharge", "commission_overcharge": "commission_overcharge",
            "card_fee_creep": "fee_rate_change", "cash_deposit_short": "short_deposit", "unrecorded_charge": "undocumented_charge",
            "missed_payment": "missed_payment"}


def from_bk_report(report, id_map=None):
    """A report from the bk Bookkeeper (schema 1.1: a dict, JSON text or a file path) -> (findings, adjustments, rejected).
    Nessie ids become bank_ids through id_map ({nessie_id: bank_id}, from bookkeeper_tools.bank_id_map()).
    `rejected` maps the ids of findings that could not be converted to the reason: treat them like failed checks.
    Unverified: run checks.py on the findings, then hand passed_adjustments() to the Forecaster."""
    if isinstance(report, Path) or (isinstance(report, str) and not report.lstrip().startswith("{")):
        report = Path(report).read_text()
    if isinstance(report, str):
        report = json.loads(report)
    id_map = id_map or {}

    def bank_id(x):
        if re.fullmatch(BANK_ID, x):
            return x
        if x in id_map:
            return id_map[x]
        raise ValueError(f"bank reference {x} is neither a bank_id nor a known Nessie id")

    findings, adjustments, rejected = [], [], {}
    for raw in report.get("findings", []):
        fid = str(raw.get("id"))
        try:
            t, recoverable = raw["treatment"], raw.get("recoverable") is True
            f = Finding(finding_id=fid, issue_type=BK_ISSUE.get(raw["root_cause"], "other"), verdict=raw["verdict"],
                        root_cause=f"{raw['root_cause'].replace('_', ' ')}: {raw.get('counterparty') or 'unknown payee'}",
                        counterparty=raw.get("counterparty"), first_date=(raw.get("dates") or {}).get("first"),
                        last_date=(raw.get("dates") or {}).get("last"), bank_ids=[bank_id(b) for b in raw.get("bank_ids", [])],
                        books_refs=raw.get("books_refs", []), amount_at_issue_cents=abs(raw["amounts"]["impact_cents"]),
                        monthly_impact_cents=t.get("monthly_cents") if t["kind"] == "ongoing" else None,
                        evidence=[Evidence(type=e["type"], ref=id_map.get(e["ref"], e["ref"])) for e in raw.get("evidence", [])],
                        explanation=raw["explanation"], suggested_action=raw.get("suggested_action"),
                        assumption_if_unconfirmed=raw.get("assumption_if_unconfirmed"), confidence=raw["confidence"])
            if t["kind"] == "exclude":
                adj = [Adjustment(kind="exclude_row", finding_id=fid, bank_id=bank_id(b)) for b in t["bank_ids"]]
            elif t["kind"] == "correct_to":
                adj = [Adjustment(kind="correct_row", finding_id=fid, bank_id=bank_id(t["bank_id"]), correct_amount_cents=t["cents"])]
            elif t["kind"] == "ongoing":
                adj = [Adjustment(kind="ongoing_overcharge", finding_id=fid, since=t["since"], monthly_cents=t["monthly_cents"],
                                  recoverable=recoverable)]
            elif t["kind"] == "owed":
                adj = [Adjustment(kind="unpaid_bill", finding_id=fid, cost_id=(raw.get("books_refs") or [None])[0],
                                  amount_cents=t["cents"], due_date=t["due"])]
            else:
                raise ValueError(f"unknown treatment {t['kind']!r}")
        except (KeyError, TypeError, ValueError) as e:        # pydantic's ValidationError is a ValueError
            rejected[fid] = f"{type(e).__name__}: {str(e).splitlines()[0]}"
            continue
        findings.append(f)
        adjustments += adj
    return findings, adjustments, rejected


if __name__ == "__main__":
    from pydantic import ValidationError

    items = load_suspicious()
    print(f"reconciler output fits SuspiciousItem: {len(items)} items")
    now = datetime(2026, 10, 24, 12, 0)
    f = Finding(finding_id="F1", issue_type="overcharge", verdict="error", root_cause="Supplier debited $50 more than the invoice",
                counterparty="Example Supplier", bank_ids=["T0A1B2C3D4"], books_refs=["XYZ-CHS-00001"], amount_at_issue_cents=5000,
                evidence=[Evidence(type="bank_row", ref="T0A1B2C3D4", tool="get_bank_transaction", fact="debit of $550.00", amount_cents=55000),
                          Evidence(type="invoice", ref="XYZ-CHS-00001", tool="get_cost", fact="invoice total $500.00", amount_cents=50000)],
                explanation="Example Supplier took $550.00 for a $500.00 invoice.", suggested_action="ask for a $50 refund",
                confidence=0.9)
    adj = [Adjustment(kind="correct_row", finding_id="F1", bank_id="T0A1B2C3D4", correct_amount_cents=50000),
           Adjustment(kind="ongoing_overcharge", finding_id="F2", since=date(2026, 8, 1), monthly_cents=30000, recoverable=True)]
    r = ForecastResult(forecast_id="fc-1", scenario="fridge", as_of=date(2026, 10, 24), starting_cash_cents=10_000_000,
                       run_out_worst=date(2027, 5, 1), run_out_likely=date(2027, 7, 1), run_out_best=None, share_running_out=0.7,
                       safety_floor_cents=5_000_000, below_floor_likely=date(2027, 2, 1), trials=1000, horizon_months=24, seed=13,
                       assumptions=["5% of a dish's orders lost per price rise"],
                       monthly_bands=[MonthBand(month="2026-11", worst_cents=9_000_000, likely_cents=9_500_000, best_cents=9_900_000)],
                       vs_base=VsBase(base_forecast_id="fc-0", days_moved_likely=-28, cash_change_cents=-480_000))
    s = Suggestion(suggestion_id="S1", action="reprice_item", title="Raise Example Dish from $18 to $20", target="Example Dish",
                   new_price_cents=2000, monthly_cash_effect_cents=60000, assumptions=["5% fewer orders at the new price"],
                   based_on=["F1", "fc-1"])
    sc = Scenario(name="fridge", one_off_costs=[OneOff(cents=480000, label="walk-in fridge compressor")],
                  cover_change={"2027-01": -0.15}, price_response={"Example Dish": 0.3}, fix_findings=["F2"])
    for obj in (f, *adj, r, s, sc, ForecastSettings(as_of=date(2026, 7, 24))):
        assert type(obj).model_validate_json(obj.model_dump_json()) == obj
    checks = [CheckResult(subject="bookkeeper", ref="F1", status="pass"), CheckResult(subject="bookkeeper", ref="F2", status="reject")]
    print("examples round-trip through JSON; flags:", findings_to_flags([f]), "| passed adjustments:",
          [a.kind for a in passed_adjustments(adj, checks)])
    bk = {"findings": [{"id": "B1", "root_cause": "card_fee_creep", "verdict": "error", "confidence": 0.9, "counterparty": "Example Processor",
                        "dates": {"first": "2026-08-01", "last": "2026-10-23"}, "bank_ids": ["nessie-abc"], "books_refs": [],
                        "amounts": {"impact_cents": 1200, "expected_rate": 0.026, "actual_rate": 0.029},
                        "explanation": "Fee rate went up.", "evidence": [{"type": "bank_row", "ref": "nessie-abc"}],
                        "treatment": {"kind": "ongoing", "since": "2026-08-01", "monthly_cents": 30000}, "recoverable": True,
                        "suggested_action": "dispute the rate"},
                       {"id": "B2", "root_cause": "duplicate_charge", "verdict": "error", "confidence": 0.9, "counterparty": "X",
                        "dates": {"first": "2026-04-13", "last": "2026-04-13"}, "bank_ids": ["nessie-unknown"], "books_refs": [],
                        "amounts": {"impact_cents": 100}, "explanation": "x", "evidence": [{"type": "bank_row", "ref": "x"}],
                        "treatment": {"kind": "exclude", "bank_ids": ["nessie-unknown"]}, "recoverable": True, "suggested_action": "x"}]}
    fs, ads, bad_ids = from_bk_report(bk, {"nessie-abc": "T0A1B2C3D4"})
    print("bk report ->", [(x.finding_id, x.issue_type, x.bank_ids) for x in fs], [a.kind for a in ads], "| rejected:", bad_ids)
    bad = [lambda: Finding(**{**f.model_dump(), "bank_ids": ["349.00"]}),
           lambda: Adjustment(kind="correct_row", finding_id="F3", bank_id="T0A1B2C3D4"),
           lambda: Adjustment(kind="ongoing_overcharge", finding_id="F3", monthly_cents=100),
           lambda: Scenario(cover_change={"2027-01": -1.5}),
           lambda: Scenario(price_response=5),
           lambda: Suggestion(**{**s.model_dump(), "new_price_cents": None}),
           lambda: MonthBand(month="2026-11", worst_cents=3, likely_cents=2, best_cents=1),
           lambda: Finding(**{**f.model_dump(), "amount": 5000})]
    for make in bad:
        try:
            make()
            raise SystemExit("a bad input was accepted")
        except ValidationError as e:
            print("rejected as expected:", e.errors()[0]["msg"][:90])
