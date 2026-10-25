"""One market scan: our menu -> relevant competitors -> saved menu pages -> verified prices -> position per dish.
Inputs come from cost_price.db; results go back to it (checks, new observations, a summary per dish) and to an
immutable report file. The same (sender, request_id) always returns the same report.

A scan has a 120 s budget: once it is spent, the remaining pages are skipped and listed under limitations, so a slow
site (each fetch has its own 20 s deadline and 5 s per read) can hold the scan lock for at most about 120 s plus one
page."""
import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import date, datetime, timezone
from pathlib import Path
from uuid import uuid4

from cost_price import api
from cost_price.db import connect

from . import __version__
from .analyze import analyze, upcoming_summary, with_upcoming
from .competitors import find_competitors
from .contracts import MarketReport, MarketRequest, MarketResult, RejectedPrice
from .extract import ASIReader, read_menu, rule_reader, same_site
from .pages import OfflineMiss, PageStore
from .store import write_scan
from .text import clean

LOG = logging.getLogger("market_scout")
SCAN_BUDGET = 120.0                 # seconds for all page fetches and reads of one scan


TRUE, FALSE = ("1", "true", "yes", "on"), ("", "0", "false", "no", "off")


def env_offline() -> bool:
    """MARKET_SCOUT_OFFLINE: 1/true/yes/on = offline. Anything unrecognised is refused rather than guessed."""
    value = os.getenv("MARKET_SCOUT_OFFLINE", "").strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise ValueError(f"MARKET_SCOUT_OFFLINE must be 1 or 0 (or true/false), not {value[:20]!r}")


class Busy(RuntimeError):
    """A scan is already running; the caller asked not to wait."""


def immutable_json(path: Path, obj):
    """Write once. A hard link (or a rename where links aren't supported) never replaces an existing file."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    with open(tmp, "x", encoding="utf-8") as f:
        f.write(json.dumps(obj, indent=2, default=str))
        f.flush()
        os.fsync(f.fileno())
    try:
        os.link(tmp, path)
    except FileExistsError:
        raise
    except OSError:
        os.rename(tmp, path)                              # Windows rename refuses an existing target too
    finally:
        tmp.unlink(missing_ok=True)


def seed_reader(observations):
    """The seed observations in cost_price.db become proposals like any reader's; each still has to be found on the
    saved page it came from (its own source_url, e.g. a menu tab or the drinks page)."""
    def read(page, restaurant, items):
        return [dict(our_item=x["our_item"], their_item=x["their_item"], price_cents=int(x["price_cents"]),
                     quote=x["quote"], service=x["service"], match_confidence=float(x["match_confidence"]),
                     obs_id=x["id"])
                for x in observations.get(restaurant, []) if x["our_item"] in items and x["source_url"] == page.url]
    read.reader = "seed"
    return read


def pages_for(c, observations):
    """The menu page plus any other pages the seed observations quote from, in a stable order."""
    extra = sorted({x["source_url"] for x in observations.get(c.name, []) if x.get("source_url")} - {c.menu_url})
    return [c.menu_url, *extra]


class MarketScout:
    def __init__(self, root: Path, readers=None, pages: PageStore | None = None, use_apify=None, db: Path | None = None,
                 clock=time.monotonic, budget=SCAN_BUDGET, offline=None):
        self.root = Path(root)
        self.clock, self.budget = clock, budget
        # the operator's switch (--offline or MARKET_SCOUT_OFFLINE); a request can add offline, never remove it
        self.offline = env_offline() if offline is None else bool(offline)
        self.private = self.root / "data" / "market_scout"          # gitignored by its own .gitignore
        self.db = Path(db) if db else self.root / "data" / "cost_price.db"
        self.reports = self.root / "data" / "market_reports"
        self.pages = pages or PageStore(self.root / "data" / "market_pages", offline=self.offline)
        self.readers, self.use_apify = readers, use_apify
        self.lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self.lock.locked()

    def default_readers(self, offline=False):
        readers = [rule_reader]
        if os.getenv("ASI1_API_KEY") and not offline:     # ASIReader posts to api.asi1.ai
            readers.append(ASIReader())
        return readers

    def readers_for(self, offline: bool, limits: list):
        """The page readers for this scan. Offline drops ASI (given or default) and says so."""
        readers = self.readers if self.readers is not None else self.default_readers(offline)
        if offline:
            kept = [r for r in readers if getattr(r, "reader", None) != "asi"]
            if len(kept) < len(readers) or (self.readers is None and os.getenv("ASI1_API_KEY")):
                limits.append("offline: ASI menu reader skipped (it calls api.asi1.ai); rule reader and seed list only")
            readers = kept
        return readers

    def inputs(self, on: str, service="dinner", items=()):
        """-> (here, menu, upcoming, known competitors, seed observations by restaurant) from cost_price.db."""
        with connect(self.db, "reader") as con:
            here = api.restaurant(con)
            menu, _ = api.menu(con, on, service=service, items=list(items))
            upcoming = api.upcoming_changes(con, on)
            known = api.competitors(con)
            names = {c["id"]: clean(c["name"], 120) or "?" for c in known}      # as find_competitors names them
            seed = {}
            for x in api.observations(con, reader="seed"):
                seed.setdefault(names[x["competitor_id"]], []).append(x)
        return here, menu, upcoming, known, seed

    def page_status(self):
        """-> [(restaurant, url, saved metadata or None)] for every page a scan would read (relevant competitors:
        the database's, plus cached Google Maps finds when APIFY_TOKEN is set). No network at all."""
        here, _, _, known, seed = self.inputs(date.today().isoformat())
        candidates = find_competitors(here, known, self.use_apify, self.private / "apify_cache.json", offline=True)
        store = self.pages if hasattr(self.pages, "saved") else PageStore(self.root / "data" / "market_pages",
                                                                          offline=True)
        return [(c.name, url, store.saved(url)) for c, _ in candidates if c.relevant for url in pages_for(c, seed)]

    def scan(self, request: MarketRequest, report_id: str):
        """-> (report, candidates, checks): the report, plus what write_scan stores."""
        offline = self.offline or request.offline         # a request can't turn the operator's offline mode off
        # the scan's date, computed once: our_menu, upcoming changes and every market_summary.menu_as_of use it. The
        # machine's local date (as api.latest_market defaults to), never UTC: in San Francisco from 5 pm UTC is tomorrow
        on = (request.as_of or date.today()).isoformat()
        here, menu, upcoming, known, seed = self.inputs(on, request.service, request.items)
        dropped_rows, notes = [], []
        candidates = find_competitors(here, known, self.use_apify, self.private / "apify_cache.json",
                                      request.refresh, skipped=dropped_rows, offline=offline, notes=notes)
        accepted, rejected, checks, limits, skipped = [], [], [], [], []
        limits += notes
        readers = [seed_reader(seed), *self.readers_for(offline, limits)]
        limits += [f"{x[:160]}: unreadable, left out" for x in dropped_rows]
        saved_on = {}                                     # offline: retrieval date of the saved copies read -> count
        started = self.clock()
        for c, _ in candidates:
            if not c.relevant:
                continue
            for url in pages_for(c, seed):
                if self.clock() - started >= self.budget:
                    skipped.append(f"{c.name} ({url[:120]})")
                    continue
                try:
                    page = self.pages.get(url, refresh=request.refresh, **({"offline": True} if offline else {}))
                    if offline:
                        day = page.retrieved_at.date().isoformat()
                        saved_on[day] = saved_on.get(day, 0) + 1
                    if self.clock() - started >= self.budget:     # fetching and parsing this page spent it
                        skipped.append(f"{c.name} ({url[:120]}; fetched, not read)")
                        continue
                    read = read_menu(page, c, set(menu), readers, c.distance_miles)
                except OfflineMiss:
                    limits.append(f"{c.name}: menu not read (offline, no saved copy)" if url == c.menu_url else
                                  f"{c.name}: {url[:120]} not read (offline, no saved copy)")
                    continue
                except Exception as exc:                  # one blocked site or bad page must not sink the scan
                    limits.append(f"{c.name}: menu not read ({type(exc).__name__}: {str(exc)[:120]})")
                    continue
                accepted += read.accepted
                checks += read.checks
                limits += read.errors
                if not same_site(page, c) and (read.accepted or read.rejected):
                    limits.append(f"{c.name}: {url[:120]} redirected to {page.final_url[:120]}, off the restaurant's "
                                  "site; its prices are listed under unreviewed")
                rejected += [RejectedPrice(restaurant=c.name, our_item=i, quote=q[:200], reason=r)
                             for i, q, r in read.rejected]
        if saved_on:
            limits.insert(0, "offline: nothing fetched; using saved copy from " + ", ".join(
                f"{day} ({n} page{'s' if n != 1 else ''})" for day, n in sorted(saved_on.items())))
        elif offline:
            limits.insert(0, "offline: nothing fetched, and no saved copy of any menu page")
        if skipped:
            limits.append(f"scan budget of {self.budget:.0f} s spent; pages not read: " + "; ".join(skipped))
        positions, summaries, filtered, unreviewed = analyze(menu, accepted, request.service, request.par_band)
        positions = with_upcoming(positions, upcoming, request.service, request.par_band)
        rejected += [RejectedPrice(restaurant=p.restaurant, our_item=p.our_item, quote=p.quote[:200], reason=why)
                     for p, why in filtered]
        if unreviewed:
            limits.append(f"{len(unreviewed)} prices from competitors found on Google Maps, read by ASI alone, or "
                          "redirected off the restaurant's site are listed under unreviewed and not counted in any "
                          "median until someone reviews them")
        if not any(c.found_via == "apify_google_maps" for c, _ in candidates):
            limits.append("competitors from the committed seed list only (no APIFY_TOKEN, or Apify not used)")
        limits.append("prices are as printed on each restaurant's own site; delivery apps are excluded (marked up)")
        if request.service == "dinner":
            limits.append("menus that don't separate lunch and dinner are compared against our dinner prices")
        report = MarketReport(report_id=report_id, request_id=request.request_id,
                              created_at=datetime.now(timezone.utc), menu_as_of=on,
                              our_restaurant=here["name"], service=request.service, par_band=request.par_band,
                              competitors=[c for c, _ in candidates], positions=positions, summaries=summaries,
                              prices=[a.price for a in accepted if a.trusted], unreviewed=unreviewed,
                              rejected=rejected, limitations=limits,
                              agent_version=__version__)
        return report, candidates, checks

    def scan_and_store(self, request: MarketRequest, report_id: str) -> MarketReport:
        report, candidates, checks = self.scan(request, report_id)
        counts = write_scan(self.db, report, candidates, checks)
        report.limitations += [f"not stored in cost_price.db: {x}" for x in counts.get("skipped", [])]
        LOG.info("cost_price.db: %s", {k: v if k != "skipped" else len(v) for k, v in counts.items()})
        return report

    async def run(self, request: MarketRequest, sender="local-cli", wait=True) -> MarketResult:
        if not wait and self.lock.locked():
            raise Busy("a market scan is already running; try again shortly")
        key = hashlib.sha256(f"{sender}\0{request.request_id}".encode()).hexdigest()[:24]
        receipt = self.reports / "requests" / f"{key}.json"
        async with self.lock:
            if receipt.exists():
                saved = json.loads(receipt.read_text(encoding="utf-8"))
                # compared with defaults filled in, so a receipt saved before a field was added still matches
                if MarketRequest.model_validate(saved["request"]).model_dump(mode="json") != request.model_dump(mode="json"):
                    raise ValueError("request_id reused with different parameters; use a new request_id")
                return MarketResult.model_validate(saved["result"])
            report_id = f"M-{key}"
            path = self.reports / f"{report_id}.json"
            if path.exists():                             # a crash after the report, before the receipt: reuse it
                report = MarketReport.model_validate_json(path.read_text(encoding="utf-8"))
                if report.request_id != request.request_id:
                    raise ValueError(f"{path.name} belongs to another request")
            else:
                report = await asyncio.to_thread(self.scan_and_store, request, report_id)   # database first
                immutable_json(path, report.model_dump(mode="json"))
            result = MarketResult(request_id=request.request_id, report_id=report_id,
                                  filename=path.relative_to(self.root).as_posix(),
                                  positions={p.our_item: p.position for p in report.positions},
                                  upcoming=upcoming_summary(report.positions))
            immutable_json(receipt, {"request": request.model_dump(mode="json"), "result": result.model_dump(mode="json")})
            LOG.info("market report %s: %s", report_id, result.positions)
            return result
