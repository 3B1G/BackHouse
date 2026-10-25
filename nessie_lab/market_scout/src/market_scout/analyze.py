"""Turn accepted competitor prices into a position per dish. Pure functions; no I/O.

A median is only as good as the prices under it. Only trusted readings count: the seed list or the rule reader, on a
competitor from the committed seed list. Readings from competitors Google Maps found (anyone can list a restaurant
there and publish a page the rule reader trusts) and ASI-only readings are reported as `unreviewed` and never move a
median until someone reviews them. Of the trusted ones, prices far from ours (outside 0.6x-1.7x) are dropped, then
outliers by the median absolute deviation (or, when the MAD is 0, anything more than 25% from the median), and what is
left must be at least 3 prices from 3 different competitors. Every dropped price is reported."""
from statistics import median

from .contracts import ItemPosition
from .extract import Accepted
from .shared import MarketSummary

MIN_PRICES = 3                      # trusted prices, each from a different competitor (shared contract: never fewer)
MIN_MATCH = 0.7                     # below this a dish match is too loose to compare (e.g. lasagna for bolognese)
LOW_RATIO, HIGH_RATIO = 0.6, 1.7    # a "competitor price" outside this band around ours is a misread or a plant
MAD_K = 3 * 1.4826                  # 3 standard deviations, for normally distributed prices
ZERO_MAD_SPREAD = 0.25              # MAD 0 (most prices equal): still drop anything more than 25% from the median
COMPARABLE = {"dinner": {"dinner", "all_day", "unknown"}, "lunch": {"lunch", "all_day"}}


def _accepted(x) -> Accepted:
    return x if isinstance(x, Accepted) else Accepted(x, "asi", None)    # a bare price has no provenance: untrusted


def one_per_restaurant(prices, our_item, service):
    """Best-matching comparable price per restaurant, so one place with three pizzas can't outvote the street."""
    best = {}
    for a in map(_accepted, prices):
        p = a.price
        if p.our_item != our_item or p.service not in COMPARABLE[service] or p.match_confidence < MIN_MATCH:
            continue
        rank = (p.match_confidence, p.service == service, a.trusted)       # ties: this service, then trusted
        held = best.get(p.restaurant)
        if held is None or rank > (held.price.match_confidence, held.price.service == service, held.trusted):
            best[p.restaurant] = a
    return sorted(best.values(), key=lambda a: a.price.restaurant)


def position(our_cents, med, band):
    if med is None:
        return "insufficient_data"
    gap = (our_cents - med) / med
    return "below_market" if gap < -band else "above_market" if gap > band else "at_par"


def in_band(ours: int, candidates: list[Accepted]):
    """-> (kept, [(Accepted, reason)]): prices within LOW_RATIO..HIGH_RATIO of ours."""
    kept, dropped = [], []
    for a in candidates:
        ratio = a.price.price_cents / ours
        if LOW_RATIO <= ratio <= HIGH_RATIO:
            kept.append(a)
        else:
            dropped.append((a, f"{ratio:.2f}x our price, outside {LOW_RATIO}x-{HIGH_RATIO}x"))
    return kept, dropped


def mad_filter(candidates: list[Accepted]):
    """-> (kept, [(Accepted, reason)]): drop |x - median| > 3 x 1.4826 x MAD; when the MAD is 0 (and there are at
    least 3 prices), drop |x - median| > 25% of the median instead."""
    if not candidates:
        return [], []
    med = median(a.price.price_cents for a in candidates)
    mad = median(abs(a.price.price_cents - med) for a in candidates)
    if mad == 0:
        if len(candidates) < MIN_PRICES:
            return list(candidates), []
        out = [a for a in candidates if abs(a.price.price_cents - med) > ZERO_MAD_SPREAD * med]
        return [a for a in candidates if a not in out], [
            (a, f"outlier: {abs(a.price.price_cents - med) / 100:.2f} from the median {med / 100:.2f}, more than "
                f"{ZERO_MAD_SPREAD:.0%} of it (MAD is 0)") for a in out]
    out = [a for a in candidates if abs(a.price.price_cents - med) > MAD_K * mad]
    return [a for a in candidates if a not in out], [
        (a, f"outlier: {abs(a.price.price_cents - med) / 100:.2f} from the median {med / 100:.2f}, "
            f"more than 3 x 1.4826 x MAD ({mad / 100:.2f})") for a in out]


def analyze(menu: dict[str, int], prices, service="dinner", band=0.05):
    """-> (positions, summaries, dropped [(CompetitorPrice, reason)], unreviewed [CompetitorPrice]). Medians are
    rounded to whole cents. Unreviewed prices (see the module docstring) are listed, never counted."""
    positions, summaries, filtered, unreviewed = [], [], [], []
    candidates = [_accepted(x) for x in prices]
    for item, ours in sorted(menu.items()):
        comparable = [a for a in candidates if a.price.our_item == item and a.price.service in COMPARABLE[service]]
        unreviewed += [a.price for a in comparable if not a.trusted]
        # the ratio band first, over every trusted candidate, so a wild price can't displace a sane one
        mine = [a for a in comparable if a.trusted and a.price.match_confidence >= MIN_MATCH]
        sane, band_drop = in_band(ours, mine)
        kept, mad_drop = mad_filter(one_per_restaurant(sane, item, service))
        filtered += [(a.price, why) for a, why in band_drop + mad_drop]
        cents = [a.price.price_cents for a in kept]
        enough = len({a.price.restaurant for a in kept}) >= MIN_PRICES
        med = int(median(cents) + 0.5) if enough else None          # half-cents round up, not to even
        gap = ours - med if med is not None else None
        positions.append(ItemPosition(
            our_item=item, service=service, our_price_cents=ours, n_prices=len(cents), competitor_median_cents=med,
            competitor_low_cents=min(cents) if cents else None, competitor_high_cents=max(cents) if cents else None,
            price_gap_cents=gap, gap_pct=round(gap / med, 4) if med else None, position=position(ours, med, band),
            restaurants=[a.price.restaurant for a in kept],
            n_unreviewed=sum(not a.trusted for a in comparable)))
        summaries.append(MarketSummary(our_item=item, our_price_cents=ours, service=service,
                                       prices=[a.price for a in kept], competitor_median_cents=med,
                                       price_gap_cents=gap))
    return positions, summaries, filtered, unreviewed


def with_upcoming(positions: list[ItemPosition], changes: list[dict], service="dinner", band=0.05) -> list[ItemPosition]:
    """Add each dish's next approved change after the scan date (from cost_price.api.upcoming_changes): the earliest
    dine-in price for this service, placed against the dish's current median, and the earliest removal. `position`
    itself is untouched: it is today's menu."""
    out = []
    for p in positions:
        price = next((c for c in changes if c.get("kind") == "price" and c.get("item") == p.our_item
                      and c.get("channel", "dine_in") == "dine_in" and c.get("service") == service
                      and isinstance(c.get("price_cents"), int) and c["price_cents"] > 0), None)
        gone = next((c for c in changes if c.get("kind") == "availability" and c.get("item") == p.our_item
                     and c.get("available") is False), None)
        update = {}
        if price:
            update.update(upcoming_price_cents=price["price_cents"], upcoming_from=price["effective_from"],
                          upcoming_position=position(price["price_cents"], p.competitor_median_cents, band))
        if gone:
            update["leaving_from"] = gone["effective_from"]
        out.append(p.model_validate({**p.model_dump(), **update}) if update else p)
    return out


def upcoming_summary(positions: list[ItemPosition]) -> dict[str, dict]:
    """our_item -> the upcoming fields that are set, for the chat reply."""
    out = {}
    for p in positions:
        entry = {}
        if p.upcoming_price_cents is not None:
            entry.update(price_cents=p.upcoming_price_cents, **{"from": p.upcoming_from.isoformat()},
                         position=p.upcoming_position)
        if p.leaving_from is not None:
            entry["leaving_from"] = p.leaving_from.isoformat()
        if entry:
            out[p.our_item] = entry
    return out
