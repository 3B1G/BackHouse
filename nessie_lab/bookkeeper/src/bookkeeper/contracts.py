"""Shared report and orchestration contracts; validation is structural only."""
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

Cents = Annotated[int, Field(strict=True)]
Count = Annotated[int, Field(strict=True, ge=0)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReportRequest(Contract):
    request_id: str = Field(min_length=1, max_length=128)
    as_of: date | None = None
    nessie_tag: str = Field(default="boh-1loc", pattern=r"^[a-zA-Z0-9_-]{1,80}$")


class ReportResult(Contract):
    request_id: str
    report_id: str
    filename: str
    as_of: date
    verified: Literal[False] = False


class Window(Contract):
    start: date
    end: date


class Sources(Contract):
    nessie_tag: str
    bank_snapshot: str
    bank_rows: Count
    books_db: str


class AgentInfo(Contract):
    version: str
    model: str
    trace_id: str


class Verification(Contract):
    passed: Literal[False] = False
    checked_at: None = None
    findings_checked: Literal[0] = 0
    findings_rejected: Literal[0] = 0
    notes: list[str] = Field(default_factory=lambda: ["Verification was not run; this is an unverified Bookkeeper report."])


class Coverage(Contract):
    bank_rows_reviewed: Count
    matched_to_documents: Count
    investigated: Count
    cleared_as_ok: Count


class Summary(Contract):
    errors: Count
    needs_owner: Count
    total_impact_cents: Cents
    recoverable_cents: Cents
    ongoing_monthly_cents: Cents


class Amounts(Contract):
    expected_cents: Cents | None = None
    actual_cents: Cents | None = None
    impact_cents: Cents
    expected_rate: float | None = None
    actual_rate: float | None = None


class Exclude(Contract):
    kind: Literal["exclude"]
    bank_ids: list[str]


class CorrectTo(Contract):
    kind: Literal["correct_to"]
    bank_id: str
    cents: Cents


class Ongoing(Contract):
    kind: Literal["ongoing"]
    since: date
    monthly_cents: Cents


class Owed(Contract):
    kind: Literal["owed"]
    cents: Cents
    due: date


class Evidence(Contract):
    type: Literal["bank_row", "invoice", "agreement", "computed", "search", "document"]
    ref: str


class Dates(Contract):
    first: date
    last: date


class Finding(Contract):
    id: str
    root_cause: Literal["duplicate_charge", "supplier_overcharge", "commission_overcharge", "card_fee_creep", "cash_deposit_short", "unrecorded_charge", "missed_payment"]
    verdict: Literal["error", "needs_owner"]
    confidence: float = Field(ge=0, le=1)
    counterparty: str
    dates: Dates
    bank_ids: list[str]
    books_refs: list[str]
    amounts: Amounts
    explanation: str
    evidence: list[Evidence]
    treatment: Annotated[Exclude | CorrectTo | Ongoing | Owed, Field(discriminator="kind")]
    recoverable: bool | Literal["unknown"]
    suggested_action: str
    assumption_if_unconfirmed: str | None = None


class Cleared(Contract):
    bank_ids: list[str]
    reason: str


class Draft(Contract):
    coverage: Coverage
    summary: Summary
    findings: list[Finding]
    cleared: list[Cleared]
    limitations: list[str]


class AccountBalance(Contract):
    account: str
    opening_as_of: date
    opening_balance_cents: Cents
    inflow_cents: Cents
    outflow_cents: Cents
    balance_cents: Cents


class Balances(Contract):
    as_of: date
    accounts: list[AccountBalance]
    total_cents: Cents
    basis: str = "Opening balances plus completed bank activity; snapshot completeness assumed. Findings do not alter cash."


class Report(Draft):
    report_id: str
    request_id: str
    schema_version: Literal["1.1"] = "1.1"
    generated_at: datetime
    as_of: date
    window: Window
    sources: Sources
    bookkeeper: AgentInfo
    verification: Verification = Field(default_factory=Verification)
    balances: Balances
