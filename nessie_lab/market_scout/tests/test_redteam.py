"""Regression tests for the red-team findings (PoCs p1_ip.py .. p6_asi.py), fully offline: MockTransport, the fake
resolver and tmp roots. Each test failed before its fix."""
import gzip
import io
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from conftest import PAGES, make_root, store
from cost_price import api
from cost_price.db import connect
from market_scout import agent, competitors, pages
from market_scout.analyze import analyze, in_band, mad_filter
from market_scout.cli import table
from market_scout.contracts import MarketReport, MarketRequest
from market_scout.extract import Accepted, ASIReader, read_menu, rule_reader
from market_scout.pages import FetchError, Snapshot, page_text, pdf_text, public_address
from market_scout.runner import MarketScout
from market_scout.shared import CompetitorPrice
from market_scout.text import clean, safe

HTML = {"content-type": "text/html"}
SEED_C = SimpleNamespace(name="R", found_via="seed")


def snap(html, url="https://r.example/m"):
    return Snapshot(url, None, page_text(html), datetime(2026, 10, 24, tzinfo=timezone.utc), "a" * 64,
                    page_text(html, include_hidden=True))


def price(restaurant, cents, item="Margherita Pizza"):
    return CompetitorPrice(our_item=item, restaurant=restaurant, their_item="Margherita", price_cents=cents,
                           service="dinner", source_url="https://x.example/", source_type="restaurant_site",
                           quote=f"Margherita {cents / 100:g}", match_confidence=0.9,
                           retrieved_at=datetime(2026, 10, 24, tzinfo=timezone.utc))


# ---------------------------------------------------------------- p1: addresses that carry a private IPv4
@pytest.mark.parametrize("address", ["64:ff9b::7f00:1", "64:ff9b::a9fe:a9fe", "64:ff9b::a00:1", "::7f00:1",
                                     "::5db8:d822", "64:ff9b:1::5db8:d822", "2002:7f00:1::"])
def test_nat64_and_ipv4_compatible_addresses_are_refused(address):
    with pytest.raises(FetchError, match="non-public"):
        public_address("h.example", 80, lambda h, p: [address])


def test_nat64_of_a_public_ipv4_is_allowed():
    assert public_address("h.example", 80, lambda h, p: ["64:ff9b::5db8:d822"]) == "64:ff9b::5db8:d822"


# ---------------------------------------------------------------- p2: hidden-text tricks
@pytest.mark.parametrize("html", [
    "<style>.x{display:none}</style><p>Margherita</p><p class=x>$43</p><p>$21</p>",
    "<style>#x { visibility: hidden }</style><p>Margherita</p><p id=x>$43</p><p>$21</p>",
    "<style>.a, .x{display:none !important}</style><p>Margherita</p><p class='y x'>$43</p><p>$21</p>",
    "<p>Margherita</p><p style='font-size:0'>$43</p><p>$21</p>",
    "<p>Margherita</p><p style='font-size: 0px;'>$43</p><p>$21</p>",
    "<p>Margherita</p><p style='opacity:0'>$43</p><p>$21</p>",
    "<p>Margherita</p><p style='opacity: 0.0'>$43</p><p>$21</p>",
    "<p>Margherita</p><p style='position:absolute;left:-9999px'>$43</p><p>$21</p>",
    "<p>Margherita</p><p style='position:absolute; top: -1000px'>$43</p><p>$21</p>",
    "<p>Margherita</p><p style='position:absolute;clip:rect(0,0,0,0)'>$43</p><p>$21</p>",
    "<p>Margherita</p><p style='clip-path: inset(50%)'>$43</p><p>$21</p>",
    "<p>Margherita</p><details><summary>more</summary><p>$43</p></details><p>$21</p>",
])
def test_hidden_text_tricks_are_dropped(html):
    got = [p["price_cents"] for p in rule_reader(snap(html), "R", {"Margherita Pizza"})]
    assert got == [2100]


def test_closed_details_keep_the_summary_and_open_details_show():
    assert page_text("<details><summary>Margherita 21</summary><p>Calzone 43</p></details>") == "Margherita 21"
    assert "Calzone 43" in page_text("<details open><summary>x</summary><p>Calzone 43</p></details>")
    assert "Calzone 43" in page_text("<details><summary>x</summary><p>Calzone 43</p></details>", include_hidden=True)


def test_tabbed_menus_shown_by_a_compound_rule_stay_visible():
    html = "<style>.tab{display:none}.tab.active{display:block}</style><div class='tab active'>Margherita 21</div>"
    assert page_text(html) == "Margherita 21"
    assert page_text("<p style='left:-10px;opacity:0.5;font-size:0.9em'>Margherita 21</p>") == "Margherita 21"


# ---------------------------------------------------------------- p2: collusion through untrusted competitors
def collusion(found_via):
    accepted = []
    for name, html in [("Real Trattoria", "<p>Margherita 21</p>"), ("Fake One", "<p>Margherita 43</p>"),
                       ("Fake Two", "<p>Margherita 43</p>")]:
        via = "seed" if name.startswith("Real") else found_via
        accepted += read_menu(snap(html, f"https://{name[:4].lower()}.example/"),
                              SimpleNamespace(name=name, found_via=via), {"Margherita Pizza"}, [rule_reader]).accepted
    return analyze({"Margherita Pizza": 1750}, accepted)


def test_apify_found_competitors_never_move_a_median():
    pos, _, _, unreviewed = collusion("apify_google_maps")
    assert pos[0].competitor_median_cents is None and pos[0].position == "insufficient_data"
    assert sorted(p.restaurant for p in unreviewed) == ["Fake One", "Fake Two"] and pos[0].n_unreviewed == 2
    assert pos[0].restaurants == ["Real Trattoria"]


def test_rule_readings_are_trusted_only_on_seed_competitors():
    t = lambda via, reader="rule": Accepted(price("A", 2000), reader, via).trusted
    assert t("seed") and t("seed", "seed")
    assert not t("apify_google_maps") and not t(None) and not t("seed", "asi")


def test_median_needs_three_trusted_prices_from_three_competitors():
    menu = {"Margherita Pizza": 1750}
    three = [Accepted(price(n, c), "rule", "seed") for n, c in [("A", 2000), ("B", 2100), ("C", 2200)]]
    assert analyze(menu, three)[0][0].competitor_median_cents == 2100
    two_plus_apify = three[:2] + [Accepted(price("C", 2200), "rule", "apify_google_maps")]
    assert analyze(menu, two_plus_apify)[0][0].position == "insufficient_data"
    same_place = three[:2] + [Accepted(price("A", 2200), "seed", "seed")]
    assert analyze(menu, same_place)[0][0].position == "insufficient_data"


def test_band_is_0_6x_to_1_7x():
    kept, dropped = in_band(1000, [Accepted(price(n, c), "seed", "seed")
                                   for n, c in [("A", 590), ("B", 600), ("C", 1700), ("D", 1710)]])
    assert [a.price.price_cents for a in kept] == [600, 1700] and "outside 0.6x-1.7x" in dropped[0][1]


def test_zero_mad_still_drops_prices_far_from_the_median():
    same = [Accepted(price(n, 2000), "seed", "seed") for n in "ABC"] + [Accepted(price("D", 2600), "seed", "seed"),
                                                                       Accepted(price("E", 2400), "seed", "seed")]
    kept, dropped = mad_filter(same)
    assert sorted(a.price.price_cents for a in kept) == [2000, 2000, 2000, 2400]
    assert [a.price.price_cents for a, _ in dropped] == [2600] and "25%" in dropped[0][1]


# ---------------------------------------------------------------- p3: scans with fake Apify finds
def fake_apify(calls, extra=()):
    row = lambda n, u: dict(name=n, address="1 St", neighborhood="Marina", cuisine="Italian restaurant",
                            price_level="$$", distance_miles=0.3, menu_url=u, found_via="apify_google_maps", closed=False)

    def f(here, token, **kw):
        calls.append(1)
        return [row("Fake One Trattoria", "https://fake1.example/"), row("Fake Two Osteria", "https://fake2.example/"),
                *[row(n, u) for n, u in extra]]
    return f


def page_handler(extra):
    site = {**PAGES, **extra}

    def handler(req):
        url = f"https://{req.headers['host']}{req.url.raw_path.decode()}"
        return httpx.Response(200, headers=HTML, text=site[url]) if url in site else httpx.Response(404)
    return handler


FAKES = {"https://fake1.example/": "<p>Margherita 43</p><p>Tiramisu 32</p>",
         "https://fake2.example/": "<p>Margherita 43</p><p>Tiramisu 32</p>"}


def apify_scout(root, monkeypatch, calls, extra=()):
    monkeypatch.setattr(competitors, "from_apify", fake_apify(calls, extra))
    monkeypatch.setenv("APIFY_TOKEN", "x")
    return MarketScout(root, readers=[rule_reader], pages=store(root, page_handler(FAKES)), use_apify=None)


def report_of(root, result):
    return MarketReport.model_validate_json((root / result.filename).read_text(encoding="utf-8"))


async def test_colluding_apify_sites_do_not_move_the_median_and_are_listed(root, monkeypatch):
    calls = []
    s = apify_scout(root, monkeypatch, calls)
    result = await s.run(MarketRequest(request_id="a", items=["Margherita Pizza", "Tiramisu"]))
    rep = report_of(root, result)
    pos = {p.our_item: p for p in rep.positions}
    assert pos["Margherita Pizza"].competitor_median_cents == 2100          # 19, 21, 23 from the seed list
    assert "Fake One Trattoria" not in pos["Margherita Pizza"].restaurants
    assert {p.restaurant for p in rep.unreviewed} == {"Fake One Trattoria", "Fake Two Osteria"}
    assert not any(p.restaurant.startswith("Fake") for p in rep.prices)
    assert any("unreviewed" in x for x in rep.limitations)
    with connect(root / "data" / "cost_price.db") as con:                   # stored, for review
        assert {"Fake One Trattoria", "Fake Two Osteria"} <= {c["name"] for c in api.competitors(con)}


async def test_apify_results_are_cached_for_24_hours(root, monkeypatch):
    calls = []
    s = apify_scout(root, monkeypatch, calls)
    for rid in "abc":
        await s.run(MarketRequest(request_id=rid, items=["Margherita Pizza"]))
    await s.run(MarketRequest(request_id="r1", items=["Margherita Pizza"], refresh=True))     # fresh cache: reused
    assert len(calls) == 1
    cache = root / "data" / "market_scout" / "apify_cache.json"
    saved = json.loads(cache.read_text(encoding="utf-8"))
    saved["at"] = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
    cache.write_text(json.dumps(saved), encoding="utf-8")
    await s.run(MarketRequest(request_id="d", items=["Margherita Pizza"]))                   # stale, no refresh
    assert len(calls) == 1
    await s.run(MarketRequest(request_id="r2", items=["Margherita Pizza"], refresh=True))     # stale and refresh
    assert len(calls) == 2
    assert (root / "data" / "market_scout" / ".gitignore").read_text(encoding="utf-8").strip().endswith("*")


async def test_a_lone_surrogate_in_an_apify_name_does_not_kill_the_scan(root, monkeypatch):
    s = apify_scout(root, monkeypatch, [], [("Bad \ud800 Pizza", "https://bad.example/")])
    result = await s.run(MarketRequest(request_id="s1", items=["Margherita Pizza"]))
    rep = report_of(root, result)
    assert "Bad  Pizza" in {c.name for c in rep.competitors}
    assert result.positions["Margherita Pizza"] == "below_market"


async def test_terminal_escapes_never_reach_the_table(root, monkeypatch):
    evil = "Evil\x1b[2J\x1b[31mPWNED\x1b]0;title\x07 Pizza"
    s = apify_scout(root, monkeypatch, [], [(evil, "https://nowhere.example/")])
    result = await s.run(MarketRequest(request_id="e1", items=["Margherita Pizza"]))
    rep = report_of(root, result)
    assert "EvilPWNED Pizza" in {c.name for c in rep.competitors}            # cleaned when the report is built
    rep.limitations.append(evil)                                             # a tampered report file
    rep.positions[0].our_item = "Marg\x1b[31mherita\x9b2J"
    out = table(rep)
    assert "\x1b" not in out and "\x07" not in out and "\x9b" not in out
    assert safe("a\x1b]0;t\x07b\nc") == "abc"


# ---------------------------------------------------------------- p4: gzip bomb, slow drip, scan budget
class Stream(httpx.SyncByteStream):
    def __init__(self, chunks, tick=None):
        self.chunks, self.tick, self.served = chunks, tick, 0

    def __iter__(self):
        for c in self.chunks:
            if self.tick:
                self.tick()
            self.served += 1
            yield c


def test_compressed_responses_are_refused_and_identity_is_requested(tmp_path):
    bomb = gzip.compress(gzip.compress(b"\0" * (20 * 1024 * 1024), 9), 9)
    seen = []

    def handler(req):
        seen.append(req.headers.get("accept-encoding"))
        return httpx.Response(200, headers={**HTML, "content-encoding": "gzip, gzip"}, stream=Stream([bomb]))

    with pytest.raises(FetchError, match="Content-Encoding"):
        store(tmp_path, handler).get("https://a.example/")
    assert seen == ["identity"]
    for coding in ("br", "deflate", "zstd", "x-gzip"):
        with pytest.raises(FetchError, match="Content-Encoding"):
            store(tmp_path / coding, lambda r: httpx.Response(200, headers={**HTML, "content-encoding": coding},
                                                              content=b"x")).get("https://a.example/")
    page = store(tmp_path / "id", lambda r: httpx.Response(200, headers={**HTML, "content-encoding": "identity"},
                                                           content=b"<p>Margherita 21</p>")).get("https://a.example/")
    assert page.text == "Margherita 21"


def test_body_is_read_raw_never_decoded(tmp_path, monkeypatch):
    monkeypatch.setattr(httpx.Response, "iter_bytes", lambda *a, **k: (_ for _ in ()).throw(AssertionError("decoded")))
    body = Stream([b"<p>Margherita ", b"21</p>"])
    page = store(tmp_path, lambda r: httpx.Response(200, headers=HTML, stream=body)).get("https://a.example/")
    assert page.text == "Margherita 21" and body.served == 2


def test_slow_drip_is_cut_off_at_the_deadline(tmp_path):
    t = [0.0]
    drip = Stream([b"<p>x</p>"] * 200, tick=lambda: t.__setitem__(0, t[0] + 5))
    with pytest.raises(FetchError, match="longer than 20 s"):
        store(tmp_path, lambda r: httpx.Response(200, headers=HTML, stream=drip), clock=lambda: t[0]).get(
            "https://a.example/")
    assert drip.served <= 5 and t[0] <= 25


def test_every_read_has_a_short_timeout(tmp_path):
    timeouts = []

    def handler(req):
        timeouts.append(req.extensions["timeout"])
        return httpx.Response(200, headers=HTML, content=b"<p>Margherita 21</p>")

    store(tmp_path, handler).get("https://a.example/")
    assert timeouts[0]["read"] <= pages.READ_TIMEOUT == 5.0


async def test_scan_budget_skips_the_remaining_pages(root):
    t = [0.0]

    class SlowPages:
        calls = []

        def get(self, url, refresh=False):
            self.calls.append(url)
            t[0] += 50
            return snap(PAGES[url], url)

    p = SlowPages()
    from market_scout import runner
    assert runner.SCAN_BUDGET == 120
    s = MarketScout(root, readers=[rule_reader], pages=p, use_apify=False, clock=lambda: t[0], budget=100)
    result = await s.run(MarketRequest(request_id="slow-1"))
    rep = report_of(root, result)
    assert p.calls == ["https://a.example/menu", "https://b.example/menu"]       # c.example is past the budget
    note = [x for x in rep.limitations if "scan budget" in x]
    assert note and "c.example" in note[0] and "a.example" not in note[0]


# ---------------------------------------------------------------- p5: the local client's seed
def test_no_constant_local_client_seed_in_the_source():
    import inspect
    src = inspect.getsource(agent)
    assert "market-scout-local-demo-client-not-for-production" not in src and "LOCAL_CLIENT_SEED =" not in src


def test_local_client_seed_from_env_or_a_private_generated_file(tmp_path, monkeypatch):
    monkeypatch.delenv("MARKET_SCOUT_LOCAL_CLIENT_SEED", raising=False)
    first = agent.local_client_seed(tmp_path)
    path = tmp_path / "data" / "market_scout" / "local_client_seed"
    assert len(first) >= 64 and path.read_text(encoding="utf-8").strip() == first
    assert agent.local_client_seed(tmp_path) == first                                  # stable
    assert agent.local_client_seed(make_other(tmp_path)) != first                       # per machine
    assert (tmp_path / "data" / "market_scout" / ".gitignore").exists()
    if os.name == "posix":
        assert path.stat().st_mode & 0o077 == 0
    monkeypatch.setenv("MARKET_SCOUT_LOCAL_CLIENT_SEED", "s" * 40)
    assert agent.local_client_seed(tmp_path) == "s" * 40


def make_other(tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    return other


def test_forged_old_seed_is_not_allowed(tmp_path, monkeypatch):
    from uagents_core.identity import Identity
    monkeypatch.delenv("MARKET_SCOUT_ALLOWED_SENDERS", raising=False)
    monkeypatch.delenv("MARKET_SCOUT_LOCAL_CLIENT_SEED", raising=False)
    forged = Identity.from_seed("market-scout-local-demo-client-not-for-production", 0).address
    assert forged not in agent.allowed_senders(tmp_path)


def test_serve_needs_an_allowlist_or_local_only(tmp_path, monkeypatch):
    import uagents.asgi
    monkeypatch.setenv("MARKET_SCOUT_SEED", "scout-test-seed-" + "x" * 20)
    monkeypatch.delenv("MARKET_SCOUT_ALLOWED_SENDERS", raising=False)
    monkeypatch.delenv("MARKET_SCOUT_BIND_HOST", raising=False)
    monkeypatch.setattr(uagents.asgi, "HOST", "0.0.0.0")
    with pytest.raises(ValueError, match="MARKET_SCOUT_ALLOWED_SENDERS"):
        agent.make_agent(tmp_path, 8013)
    assert agent.bind_localhost() == "127.0.0.1" and uagents.asgi.HOST == "127.0.0.1"


# ---------------------------------------------------------------- p6: surrogates and bad rows
def test_clean_drops_surrogates_private_use_and_unassigned():
    assert clean("Margherita\ud800\ue000\U000e0001\u0378 21\x1b[31m\nx", 120) == "Margherita 21\nx"


def test_asi_fields_are_cleaned_as_they_arrive():
    content = json.dumps({"items": [{"our_item": "Margherita Pizza", "their_item": "Margherita\ud800",
                                     "price": 21, "quote": "Margherita 21", "match_confidence": 1e308}]})
    t = httpx.MockTransport(lambda r: httpx.Response(200, json={"choices": [{"message": {"content": content}}]}))
    pg = snap("<p>Margherita 21</p>")
    r = read_menu(pg, SEED_C, {"Margherita Pizza"}, [ASIReader(api_key="k", transport=t)])
    assert [a.price.their_item for a in r.accepted] == ["Margherita"]
    assert not r.accepted[0].trusted                                         # ASI alone: unreviewed


async def test_one_bad_row_is_skipped_not_the_whole_write(root):
    s = MarketScout(root, readers=[rule_reader], pages=_Pages(), use_apify=False)
    real = s.scan

    def scan(request, report_id):                         # smuggle bad text past every upstream clean()
        report, candidates, checks = real(request, report_id)
        at = lambda ch, item: ch.price is not None and ch.price.our_item == item and ch.restaurant == "Beta Pizzeria"
        bad = {"Lobster Ravioli": "Lobster \ud800 Ravioli", "Tiramisu": "\u200b\ud800"}
        checks = [next((ch._replace(price=ch.price.model_copy(update={"their_item": v})) for k, v in bad.items()
                        if at(ch, k)), ch) for ch in checks]
        assert sum(at(ch, k) for ch in checks for k in bad) == 2
        return report, candidates, checks
    s.scan = scan
    result = await s.run(MarketRequest(request_id="bad-row"))
    notes = [x for x in report_of(root, result).limitations if x.startswith("not stored")]
    assert len(notes) == 1 and "Tiramisu" in notes[0]                        # nothing usable left: skipped, reported
    with connect(root / "data" / "cost_price.db") as con:
        assert con.execute("SELECT count(*) FROM market_summary").fetchone()[0] == 3        # the rest got written
        stored = {o["their_item"] for o in api.observations(con, reader="rule")}
    assert "Lobster  Ravioli" in stored                                       # the surrogate is cleaned out
    assert result.positions["Margherita Pizza"] == "below_market"


def test_a_row_that_fails_inside_the_transaction_rolls_back_alone():
    import sqlite3
    from market_scout.store import _row
    con = sqlite3.connect(":memory:", isolation_level=None)
    con.execute("CREATE TABLE t (x TEXT NOT NULL)")
    con.execute("BEGIN")
    skipped = []
    assert _row(con, "good", skipped, lambda: con.execute("INSERT INTO t VALUES ('a')"))
    assert not _row(con, "bad", skipped, lambda: (con.execute("INSERT INTO t VALUES ('b')"),
                                                  con.execute("INSERT INTO t VALUES (NULL)")))
    con.execute("COMMIT")
    assert [r[0] for r in con.execute("SELECT x FROM t")] == ["a"] and skipped[0].startswith("bad: IntegrityError")


class _Pages:
    def get(self, url, refresh=False):
        if url not in PAGES:
            raise ValueError("blocked")
        return snap(PAGES[url], url)


# ---------------------------------------------------------------- PDF page bombs
def many_page_pdf(n):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(width=72, height=72)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def test_pdf_with_too_many_pages_is_refused_before_extraction(monkeypatch):
    import pypdf
    calls = []
    monkeypatch.setattr(pypdf.PageObject, "extract_text", lambda self, *a, **k: calls.append(1) or "")
    with pytest.raises(ValueError, match="5000 pages"):
        pdf_text(many_page_pdf(5000))
    assert calls == []


def test_pdf_extraction_stops_at_the_text_cap(monkeypatch):
    import pypdf
    calls = []
    monkeypatch.setattr(pypdf.PageObject, "extract_text",
                        lambda self, *a, **k: calls.append(1) or "Margherita 21 " + "y" * 50_000)
    text = pdf_text(many_page_pdf(25))
    assert len(text) <= pages.MAX_PDF_TEXT and len(calls) == 5
