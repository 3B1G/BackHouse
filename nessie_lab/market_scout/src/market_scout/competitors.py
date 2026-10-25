"""Who counts as a competitor. Candidates come from cost_price.db (the committed seed list is loaded there by
`cost-price build`) and, with APIFY_TOKEN, from Google Maps (compass/crawler-google-places). Relevance is a rule, so the same restaurant is always kept or dropped
for the same reason: Italian or pizza, near us, similar price level, not a chain, not closed.

Every text field from Google Maps is cleaned as it arrives. Apify runs cost money, so its results are cached for 24 h
in a gitignored file; a scan re-runs Apify only when asked to refresh and the cache is stale (or there is none)."""
import json
import logging
import math
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .contracts import Competitor
from .shared import private_dir
from .text import clean

LOG = logging.getLogger("market_scout")
APIFY_CACHE_AGE = timedelta(hours=24)

MAX_MILES = 2.5
CUISINE_WORDS = ("italian", "pizza", "trattoria", "osteria", "pasta")
CHAINS = ("olive garden", "domino", "pizza hut", "papa john", "little caesars", "round table", "mountain mike",
          "california pizza kitchen", "buca di beppo", "maggiano", "pizza my heart", "blaze pizza", "mod pizza")
OK_LEVELS = {"$$", "$$$"}           # we are $$ (mains $17–38, ~$42 average check); $ is counter service, $$$$ fine dining


def miles(lat1, lng1, lat2, lng2):
    r = 3958.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = math.sin((p2 - p1) / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lng2 - lng1) / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def level(price: str | None) -> str | None:
    """Google gives "$$" or a per-person bracket like "$20–30"; map brackets onto dollar signs."""
    if not price:
        return None
    price = price.strip()
    if set(price) == {"$"}:
        return price
    nums = [int(n) for n in re.findall(r"\d+", price)]
    if not nums:
        return None
    top = max(nums)
    return "$" if top <= 15 else "$$" if top <= 40 else "$$$" if top <= 80 else "$$$$"


def judge(c: dict) -> tuple[bool, str]:
    name, cuisine = c["name"].lower(), (c.get("cuisine") or "").lower()
    lvl, dist = level(c.get("price_level")), c.get("distance_miles")
    if c.get("closed"):
        return False, "dropped: closed"
    if any(ch in name for ch in CHAINS):
        return False, "dropped: chain"
    if not any(w in cuisine or w in name for w in CUISINE_WORDS):
        return False, f"dropped: cuisine '{c.get('cuisine')}' is not Italian or pizza"
    if dist is None or dist > MAX_MILES:
        return False, f"dropped: {dist if dist is not None else '?'} mi away (limit {MAX_MILES})"
    if lvl not in OK_LEVELS:
        return False, f"dropped: price level {lvl or 'unknown'} (we compare against {'/'.join(sorted(OK_LEVELS))})"
    if not c.get("menu_url"):
        return False, "dropped: no menu page"
    return True, f"{c.get('cuisine') or 'Italian'}, {lvl}, {dist:.1f} mi"


def from_db(rows: list[dict]) -> list[dict]:
    """Competitors already in cost_price.db (the seed list, plus restaurants an earlier scan found on Google Maps)."""
    return [dict(id=r["id"], name=clean(r["name"], 120) or "?", address=r.get("address"), neighborhood=r.get("neighborhood"),
                 cuisine=r.get("cuisine"), price_level=r.get("price_level"), distance_miles=r.get("distance_miles"),
                 menu_url=r.get("menu_url"), found_via=r["found_via"]) for r in rows]


def from_apify(here: dict, token: str, radius_km=4.0, limit=25) -> list[dict]:
    """Google Maps places around us. It returns a menu *link* (and a price bracket), never menu prices."""
    from apify_client import ApifyClient

    client = ApifyClient(token)
    run_input = {"searchStringsArray": ["italian restaurant", "pizza"],
                 "customGeolocation": {"type": "Point", "coordinates": [here["lng"], here["lat"]], "radiusKm": radius_km},
                 "maxCrawledPlacesPerSearch": limit, "categoryFilterWords": ["italian", "pizza"], "language": "en",
                 "skipClosedPlaces": True, "maxReviews": 0, "maxImages": 0, "scrapePlaceDetailPage": False}
    run = client.actor("compass/crawler-google-places").call(run_input=run_input, run_timeout=timedelta(minutes=5),
                                                             wait_duration=timedelta(minutes=5), max_items=2 * limit)
    if run is None:
        raise RuntimeError("Apify Google Maps run did not finish")
    out = []
    for x in client.dataset(run.default_dataset_id).iterate_items():
        loc = x.get("location") or {}
        try:
            dist = round(miles(here["lat"], here["lng"], float(loc["lat"]), float(loc["lng"])), 2) \
                if loc.get("lat") is not None and loc.get("lng") is not None else None
        except (TypeError, ValueError):
            dist = None
        categories = x.get("categories") if isinstance(x.get("categories"), list) else []
        out.append(dict(name=x.get("title"), address=x.get("address"), neighborhood=x.get("neighborhood"),
                        cuisine=x.get("categoryName") or ", ".join(map(str, categories)),
                        price_level=x.get("price"), distance_miles=dist, menu_url=x.get("menu") or x.get("website"),
                        found_via="apify_google_maps",
                        closed=bool(x.get("permanentlyClosed") or x.get("temporarilyClosed"))))
    return out


def menu_link(url) -> str | None:
    """A menu URL we can fetch and key by domain, else None (the competitor is then dropped: no menu page).
    Google Maps rows carry whatever the owner typed: "www.x.com/menu" (no scheme), "http://a..b/menu"."""
    from cost_price.api import domain_of

    from .pages import source_url

    if not isinstance(url, str) or url.strip() != clean(url, 2000):
        return None
    url = source_url(url.strip())
    if url is None:
        return None
    try:
        domain_of(url)
    except (ValueError, TypeError):
        return None
    return url


def tidy(r: dict) -> dict:
    """An Apify row with every text field cleaned (as it arrives, and again when read back from the cache)."""
    url = menu_link(r.get("menu_url"))
    dist = r.get("distance_miles")
    dist = float(dist) if isinstance(dist, (int, float)) and not isinstance(dist, bool) and dist >= 0 else None
    return dict(name=clean(r.get("name"), 120) or "?", address=clean(r.get("address"), 200),
                neighborhood=clean(r.get("neighborhood"), 80), cuisine=clean(r.get("cuisine"), 120),
                price_level=clean(r.get("price_level"), 20), distance_miles=dist, menu_url=url,
                found_via="apify_google_maps", closed=bool(r.get("closed")))


def tidy_rows(rows, skipped: list | None = None) -> list[dict]:
    """tidy() every row; a row that still breaks something is skipped (and noted), never the whole scan."""
    out = []
    for i, r in enumerate(rows if isinstance(rows, list) else []):
        try:
            if not isinstance(r, dict):
                raise TypeError("not an object")
            out.append(tidy(r))
        except Exception as exc:                          # noqa: BLE001 - one bad Google Maps row, not the scan
            LOG.warning("Apify row %d skipped (%s)", i, type(exc).__name__)
            if skipped is not None:
                skipped.append(f"Google Maps row {i}: {type(exc).__name__}")
    return out


def apify_rows(here: dict, token: str, cache: Path | None = None, refresh=False, now=None) -> list[dict]:
    """Google Maps rows, from the cache unless (refresh and the cache is over 24 h old) or there is no cache."""
    now = now or datetime.now(timezone.utc)
    saved = None
    if cache is not None and Path(cache).exists():
        try:
            saved = json.loads(Path(cache).read_text(encoding="utf-8"))
            if not isinstance(saved, dict) or not isinstance(saved.get("rows"), list) or not isinstance(saved["at"], str):
                raise TypeError("Apify cache has the wrong shape")
            saved_at = datetime.fromisoformat(saved["at"])
            if saved_at.tzinfo is None:
                raise ValueError("Apify cache time has no time zone")
            rows = tidy_rows(saved["rows"])
            if saved.get("here") != [here["lat"], here["lng"]]:
                saved = None                              # we moved: a different neighbourhood's restaurants
            elif not refresh or now - saved_at < APIFY_CACHE_AGE:
                return rows
        except (ValueError, KeyError, TypeError, OSError, RecursionError):
            LOG.warning("Apify cache %s is unreadable; running Apify again", cache)
    rows = tidy_rows(from_apify(here, token))
    if cache is not None:
        cache = private_dir(Path(cache).parent) / Path(cache).name
        tmp = cache.with_suffix(".tmp")
        tmp.write_text(json.dumps({"at": now.isoformat(), "here": [here["lat"], here["lng"]], "rows": rows}),
                       encoding="utf-8")
        os.replace(tmp, cache)
    return rows


def find_competitors(here: dict, known: list[dict], use_apify=None, cache: Path | None = None,
                     refresh=False, skipped: list | None = None) -> list[tuple[Competitor, dict]]:
    """-> [(candidate with its verdict, its row)]. A row has `id` when it is already in the database; Apify rows that
    match a known restaurant by name or menu domain are merged into it, not added twice. Apify-found competitors are
    never trusted (see analyze), even once they are in the database."""
    from cost_price.api import domain_of

    def domain(url):
        try:
            return domain_of(url) if isinstance(url, str) else None
        except (ValueError, TypeError):
            return None

    token = os.getenv("APIFY_TOKEN") if use_apify is not False else None
    rows = from_db(known)
    if token:
        names = {r["name"].lower() for r in rows}
        domains = {domain(r.get("menu_url")) for r in rows} - {None}
        for i, r in enumerate(apify_rows(here, token, cache, refresh)):
            try:                                          # one bad row is skipped (and logged), not the scan
                r = tidy(r)
                d = domain(r["menu_url"])
                if r["name"].lower() in names or (d and d in domains):
                    continue
                names.add(r["name"].lower())
                if d:
                    domains.add(d)
                rows.append(dict(r, id=None))
            except Exception as exc:                      # noqa: BLE001
                LOG.warning("Google Maps row %d skipped (%s)", i, type(exc).__name__)
                if skipped is not None:
                    skipped.append(f"Google Maps row {i} skipped ({type(exc).__name__})")
    out = []
    for r in rows:
        try:
            ok, why = judge(r)
            out.append((Competitor(name=r["name"], address=r.get("address"), neighborhood=r.get("neighborhood"),
                                   cuisine=r.get("cuisine"), price_level=level(r.get("price_level")),
                                   distance_miles=r.get("distance_miles"), menu_url=r.get("menu_url"),
                                   found_via=r["found_via"], relevant=ok, reason=why), r))
        except Exception as exc:                          # noqa: BLE001 - a row we can't judge is left out, noted
            LOG.warning("competitor %r skipped (%s)", str(r.get("name"))[:80], type(exc).__name__)
            if skipped is not None:
                skipped.append(f"competitor {clean(r.get('name'), 80) or '?'} skipped ({type(exc).__name__})")
    return out
