"""Write one scan's results to cost_price.db in a single transaction (role "scout"): a check for every seed
observation tested against a saved page, a new observation plus check for every accepted rule or ASI price, a
competitor row for every relevant restaurant Google Maps found, and a market summary per dish.

Everything that came off a web page is cleaned before it is stored: NFKC, no terminal escapes, no control, format,
surrogate, private-use or unassigned characters except newlines, and the schema's length caps. Every row is built and
validated before the transaction starts; a row that still fails inside it is rolled back to its own savepoint,
reported under `skipped` and left out, so one bad row can't abort the scan."""
import logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from cost_price import api
from cost_price.db import connect, transaction

from .extract import OFF_SITE, same_site
from .text import clean

LOG = logging.getLogger("market_scout")
CAPS = dict(quote=300, their_item=120, name=120, notes=1000, address=200, neighborhood=80, cuisine=120,
            our_item=80, reason=200)


HEX = frozenset("0123456789abcdef")


def _encodable(values) -> bool:
    """Every string is valid UTF-8 (no lone surrogate survived): SQLite would refuse it mid-transaction."""
    try:
        for v in values:
            if isinstance(v, str):
                v.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def stored_url(url) -> str | None:
    if not isinstance(url, str) or len(url) > 500 or urlsplit(url).scheme not in ("http", "https"):
        return None
    domain = api.domain_of(url)
    if not (3 <= len(domain) <= 253) or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789.-" for c in domain):
        return None
    return url


def stamp(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _plan(report, candidates, checks):
    """-> (competitors to add, seed checks, observations, summaries, skipped reasons). No database access."""
    skipped, adds, seed_checks, observations, summaries = [], [], [], [], []
    competitor = {c.name: c for c, _ in candidates}
    for c, row in candidates:
        if row.get("id") is not None or not (c.relevant and c.found_via == "apify_google_maps"):
            continue
        name = clean(c.name, CAPS["name"])
        values = dict(name=name, menu_url=stored_url(c.menu_url), found_via="apify_google_maps",
                      address=clean(c.address, CAPS["address"]), neighborhood=clean(c.neighborhood, CAPS["neighborhood"]),
                      cuisine=clean(c.cuisine, CAPS["cuisine"]),
                      price_level=c.price_level if c.price_level in ("$", "$$", "$$$", "$$$$") else None,
                      distance_miles=c.distance_miles if c.distance_miles is None or c.distance_miles >= 0 else None)
        if not (name and values["menu_url"]) or not _encodable(values.values()):
            skipped.append(f"competitor {clean(c.name, 60) or '?'}: unusable name or menu URL")
            continue
        adds.append((c.name, values))
    for ch in checks:
        sha = ch.page.sha256
        if not (sha is None or (isinstance(sha, str) and len(sha) == 64 and all(c in HEX for c in sha))):
            skipped.append(f"{clean(ch.restaurant, 60) or '?'}: page hash is not SHA-256 hex")
            continue
        if ch.reader == "seed":
            obs_id = ch.proposal.get("obs_id")
            reason = clean(ch.reason, CAPS["reason"])
            if isinstance(obs_id, int) and ch.status in ("verified", "rejected") and _encodable([reason, sha]):
                seed_checks.append((obs_id, ch.status, reason, sha))
            elif isinstance(obs_id, int):
                skipped.append(f"seed check for observation {obs_id}: unusable values")
            continue
        if ch.price is None or ch.status != "verified":
            continue                                      # rejected or duplicate rule/ASI proposals aren't stored
        p = ch.price
        if ch.restaurant not in competitor or not same_site(ch.page, competitor[ch.restaurant]):
            skipped.append(f"{clean(ch.restaurant, 60) or '?'} {clean(p.our_item, 80) or '?'}: {OFF_SITE}, unreviewed")
            continue
        values = dict(our_item=clean(p.our_item, CAPS["our_item"]), their_item=clean(p.their_item, CAPS["their_item"]),
                      price_cents=p.price_cents, service=p.service, source_url=stored_url(p.source_url),
                      source_type=p.source_type, quote=clean(p.quote, CAPS["quote"]),
                      match_confidence=min(max(float(p.match_confidence), 0.0), 1.0), reader=ch.reader,
                      retrieved_at=stamp(p.retrieved_at), report_id=report.report_id)
        if not (values["their_item"] and values["quote"] and values["our_item"] and values["source_url"]) \
                or not 1 <= p.price_cents <= 100_000 or not _encodable([*values.values(), sha]):
            skipped.append(f"{clean(ch.restaurant, 60) or '?'} {values['our_item'] or '?'}: unusable observation")
            continue
        observations.append((ch.restaurant, values, sha))
    for pos in report.positions:
        summaries.append(dict(report_id=report.report_id, our_item=pos.our_item, service=pos.service,
                              menu_as_of=report.menu_as_of.isoformat(),      # the scan's date, as our_menu used
                              our_price_cents=pos.our_price_cents, competitor_median_cents=pos.competitor_median_cents,
                              price_gap_cents=pos.price_gap_cents, n_prices=pos.n_prices, position=pos.position))
    return adds, seed_checks, observations, summaries, skipped


def _row(con, label, skipped, write) -> bool:
    """Run one row's writes under its own savepoint; on failure roll just that row back and report it."""
    con.execute("SAVEPOINT scout_row")
    try:
        write()
    except Exception as exc:
        con.execute("ROLLBACK TO scout_row")
        con.execute("RELEASE scout_row")
        skipped.append(f"{label}: {type(exc).__name__}: {str(exc)[:200]}")
        LOG.warning("cost_price.db: skipped %s (%s)", label, type(exc).__name__)
        return False
    con.execute("RELEASE scout_row")
    return True


def write_scan(db: Path, report, candidates, checks) -> dict:
    """-> counts of what was written, plus `skipped`: rows left out and why."""
    adds, seed_checks, observations, summaries, skipped = _plan(report, candidates, checks)
    counts = dict(seed_verified=0, seed_rejected=0, observations=0, summaries=0, competitors_added=0)
    ids = {c.name: row["id"] for c, row in candidates if row.get("id") is not None}
    with connect(db, "scout") as con, transaction(con):
        before = con.execute("SELECT count(*) FROM competitor").fetchone()[0]
        for name, values in adds:
            _row(con, f"competitor {values['name']}", skipped,
                 lambda: ids.__setitem__(name, api.add_competitor(con, **values)))
        counts["competitors_added"] = con.execute("SELECT count(*) FROM competitor").fetchone()[0] - before
        for obs_id, status, reason, sha in seed_checks:
            if _row(con, f"seed check {obs_id}", skipped,
                    lambda: api.add_check(con, obs_id, status, reason, sha, report.report_id)):
                counts[f"seed_{status}"] += 1
        for restaurant, values, sha in observations:
            if restaurant not in ids:
                continue                                  # its competitor wasn't stored
            def write():
                obs = api.add_observation(con, competitor_id=ids[restaurant], **values)
                api.add_check(con, obs, "verified", None, sha, report.report_id)
            if _row(con, f"{values['our_item']} at {clean(restaurant, 60)}", skipped, write):
                counts["observations"] += 1
        for values in summaries:
            if _row(con, f"summary {values['our_item']}", skipped, lambda: api.add_market_summary(con, **values)):
                counts["summaries"] += 1
    counts["skipped"] = skipped
    return counts
