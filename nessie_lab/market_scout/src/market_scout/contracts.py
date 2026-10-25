"""Scout request/response shapes. Per-price and per-dish shapes come from the shared nessie_lab/contracts.py."""
from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from .shared import CompetitorPrice, MarketSummary

Service = Literal["lunch", "dinner"]
Position = Literal["below_market", "at_par", "above_market", "insufficient_data"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MarketRequest(Contract):
    """ASI -> scout. Empty items = every dish on our menu that competitors are likely to list."""
    request_id: str = Field(pattern=r"^[A-Za-z0-9._-]{1,64}$")
    items: list[Annotated[str, Field(min_length=1, max_length=80)]] = Field([], max_length=50)
    service: Service = "dinner"
    as_of: date | None = None                             # our menu on this date; None = today (picks up CH changes)
    refresh: bool = False                                 # True = re-fetch pages, but only snapshots over 6 h old
    # True = saved menu pages only, no Apify, no ASI. A sender can ask for offline but never turn it off: when the
    # operator started the scout offline (--offline, MARKET_SCOUT_OFFLINE=1) false here changes nothing.
    offline: bool = False
    par_band: float = Field(0.05, ge=0, le=0.5)           # within ±5% of the median counts as at par


class Competitor(Contract):
    """A restaurant the scout considered, and why it was kept or dropped."""
    name: str
    address: str | None = None
    neighborhood: str | None = None
    cuisine: str | None = None
    price_level: str | None = None                        # "$", "$$", "$$$", "$$$$"
    distance_miles: float | None = Field(None, ge=0)
    menu_url: str | None = None
    found_via: Literal["seed", "apify_google_maps"]
    relevant: bool
    reason: str                                           # "Italian, $$, 0.4 mi" or "dropped: $$$$ fine dining"


class ItemPosition(Contract):
    """Where one of our dishes sits against the market. Status only: the decision belongs to ASI with the forecast."""
    our_item: str
    service: Service
    our_price_cents: int = Field(gt=0)
    n_prices: int = Field(ge=0)                           # one price per competitor at most
    competitor_median_cents: int | None
    competitor_low_cents: int | None
    competitor_high_cents: int | None
    price_gap_cents: int | None                           # ours minus the median: negative = we are cheaper
    gap_pct: float | None                                 # price_gap / median
    position: Position
    restaurants: list[str] = []
    n_unreviewed: int = Field(0, ge=0)                    # prices listed under MarketReport.unreviewed, not counted
    # approved menu changes that take effect after the scan's date (CH schedules a reprice from tomorrow): `position`
    # stays today's menu; these say where the approved price will sit against the same median
    upcoming_price_cents: int | None = Field(None, gt=0)
    upcoming_from: date | None = None
    upcoming_position: Position | None = None
    leaving_from: date | None = None                      # an approved removal: the dish leaves the menu on this date


class RejectedPrice(Contract):
    """A price the scout read but did not trust, kept so the checker and judges can see what was thrown out."""
    restaurant: str
    our_item: str
    quote: str
    reason: str


class MarketReport(Contract):
    report_id: str
    request_id: str
    created_at: datetime
    menu_as_of: date                                      # the scan's date: our prices are the menu in force on it
    our_restaurant: str
    service: Service
    par_band: float
    competitors: list[Competitor]
    positions: list[ItemPosition]
    summaries: list[MarketSummary]                        # shared contract shape, for the Controller
    prices: list[CompetitorPrice]                         # every accepted price that counts (trusted)
    unreviewed: list[CompetitorPrice] = []                # verified on the page but untrusted: never in a median
    rejected: list[RejectedPrice] = []
    limitations: list[str] = []
    agent_version: str


class MarketResult(Contract):
    """What the scout sends back over chat: where the saved report is, plus the headline statuses."""
    request_id: str
    report_id: str
    filename: str
    status: Literal["ok"] = "ok"
    positions: dict[str, Position]                        # our_item -> position, so ASI can triage without opening the file
    # our_item -> {"price_cents", "from", "position"} and/or {"leaving_from"}: approved changes after menu_as_of.
    # Only dishes with one are listed; `positions` is always today's menu.
    upcoming: dict[str, dict] = {}
