"""Regression tests for the second red-team round: quadratic CSS and text parsing, a bad Google Maps menu link
crashing every scan, a seed site's redirect lending its trust to another domain, the bind host, and PDF limits.
Each PoC from the round (p1_css, p3_apify, p4_redirect, p5_css_fetch) is one of these tests."""
import asyncio
import io
import json
import time
import tracemalloc
from datetime import datetime, timezone
from types import SimpleNamespace

import httpx
import pytest

from conftest import HERE, FakePages, make_root, store
from cost_price.db import connect
from market_scout import competitors, extract, pages
from market_scout.contracts import MarketReport, MarketRequest
from market_scout.extract import read_menu, rule_reader
from market_scout.pages import Snapshot, hidden_selectors, page_text, pdf_text
from market_scout.runner import MarketScout

MB2 = 2 * 1024 * 1024
T = datetime(2026, 10, 24, tzinfo=timezone.utc)


def timed(f, *a):
    start = time.perf_counter()
    f(*a)
    return time.perf_counter() - start


# ---------------------------------------------------------------- p1 / p5: CSS and text parsing are linear
@pytest.mark.parametrize("unit", ["a", "/*", "a{", "}", "a{b", "{", "*/", " ", ";", ".a,", "display:none;",
                                  "font-size:0", ";clip:rect(0", "{display:none}"])
def test_two_mb_of_pathological_css_parses_in_under_a_second(unit):
    css = unit * (MB2 // len(unit))
    assert timed(hidden_selectors, css) < 1.0


def test_css_rules_still_hide_and_show_as_before():
    css = """/* a comment .x{display:none} */ .gone { display : none } #off{visibility:hidden}
             @media screen { .tiny{font-size:0} } .tab{display:none} .tab.active{display:block} .clip{clip:rect(0 0 0 0)}
             .side{left:-9999px} .fine{opacity:0.5} /* unclosed .late{display:none}"""
    classes, ids = hidden_selectors(css)
    assert classes == {"gone", "tiny", "clip", "side"} and ids == {"off"}


def test_css_is_capped_per_block_and_per_page():
    late = "a" * pages.MAX_CSS + " .late{display:none}"
    assert hidden_selectors(late) == (set(), set())                    # past the per-block cap: not read
    html = "".join(f"<style>{'x' * (pages.MAX_CSS - 100)}</style>" for _ in range(3))
    html += "<style>.p{display:none}</style><p class='p'>Margherita 21</p>"
    assert "Margherita 21" in page_text(html)                          # past the page cap: not read


def test_poc_p1_style_block_without_a_brace():
    """p1: a 40 KB <style> with no "{" took 10.6 s in page_text; 2 MB of it is now well under a second each."""
    assert timed(page_text, "<style>" + "a" * 40_000 + "</style><p>Margherita 21</p>") < 0.5
    assert timed(hidden_selectors, "/*" * (MB2 // 2)) < 1.0


def test_poc_p5_a_61_kb_page_fetches_and_rereads_fast(tmp_path):
    """p5: a 61 KB page took 24 s to fetch and 24 s again for every cached re-read."""
    body = b"<style>" + b"a" * 60_000 + b"</style><p>Margherita 21</p>"
    st = store(tmp_path, lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=body))
    assert timed(st.get, "https://a.example/menu") < 1.0
    assert timed(st.get, "https://a.example/menu") < 1.0
    assert "Margherita 21" in st.get("https://a.example/menu").text


ADVERSARIAL = ["1", "$", " ", "a", "<div", "0", ";font-size:0", ";left:-", "$1", "1.", "for ", "(", "\n",
               "margherita 21\n", "margherita\n", ";clip:rect(0", "clip-path:circle(0", "1 ", "0.", "$ "]


@pytest.mark.parametrize("unit", ADVERSARIAL)
def test_text_regexes_are_linear_on_two_mb(unit):
    s = unit * (MB2 // len(unit))
    snap = Snapshot("https://a.example/menu", None, s, T, None, s)
    competitor = SimpleNamespace(name="X", menu_url="https://a.example/menu", found_via="seed")
    budget = {pages._hides: 1.0, pages.normalize: 1.0, extract.price_tokens: 2.0, extract._another_dish: 2.0,
              lambda x: extract.heading(x, "margherita"): 1.0,
              lambda x: extract.names_for("Margherita Pizza", x): 1.0,
              lambda x: extract.nearest_price(x, "Margherita Pizza"): 3.0,
              lambda x: extract.verify(snap, "Margherita 21", 2100, "Margherita Pizza"): 2.0,
              lambda x: read_menu(snap, competitor, set(extract.DISHES), [rule_reader]): 4.0}
    for f, limit in budget.items():
        assert timed(f, s) < limit, (f, unit)


@pytest.mark.parametrize("unit", ["<a ", "<div", "<", "<!--", "<details>", "<svg>", "<p style='x", "<a>"])
def test_html_parsing_stays_linear(unit):
    """html.parser rescans to the end for every unclosed "<a " (20 KB took 13 s), and nested closed <details> were
    quadratic: both now grow linearly (4x the input, well under 16x the time)."""
    small, big = unit * (50_000 // len(unit)), unit * (200_000 // len(unit))
    a, b = timed(page_text, small + "<p>Margherita 21</p>"), timed(page_text, big + "<p>Margherita 21</p>")
    assert b < 3.0 and b < max(a, 0.01) * 8


def test_unclosed_tags_become_text_and_comments_still_drop():
    assert pages.close_tags("<a <a <b>x</b>") == "&lt;a &lt;a <b>x</b>"
    assert pages.close_tags("<p>1 < 2</p>") == "<p>1 &lt; 2</p>"
    assert pages.close_tags("<!-- <p>hidden</p> --><p>Tiramisu 9</p>") == "<!-- <p>hidden</p> --><p>Tiramisu 9</p>"
    assert page_text("<p>Margherita 21</p><!-- <p>Calzone 9</p>") == "Margherita 21"
    assert page_text("<p>a < b</p><p>Margherita 21</p>").splitlines() == ["a < b", "Margherita 21"]


def test_rule_reader_takes_a_few_proposals_per_dish():
    text = "Margherita 21\n" * 50_000
    got = rule_reader(Snapshot("u", None, text, T), "X", ["Margherita Pizza"])
    assert len(got) == extract.RULE_PER_ITEM


def test_nearest_price_matches_the_old_definition():
    def old(quote, item):
        low, tokens = quote.lower(), extract.price_tokens(quote)
        exact, loose = extract.DISHES[item]
        spans = [(m.start(), m.end()) for n in [*exact, *loose] for m in __import__("re").finditer(n, low)]
        if not tokens or not spans:
            return None
        d = lambda t: min((max(t[0] - s[1], s[0] - t[1], 0), t[0] < s[0]) for s in spans)
        return min(tokens, key=lambda t: (d(t), t[0]))[2]
    for q in ["Margherita 18 Calzone 24", "24 Calzone Margherita 18", "12 Margherita", "Margherita 12 Margherita 30",
              "19 Pizza Margherita, basil 21", "Tiramisu 9 Margherita 14 Tiramisu 9 Margherita"]:
        assert extract.nearest_price(q, "Margherita Pizza") == old(q, "Margherita Pizza"), q


async def test_scan_budget_is_checked_after_each_page_is_parsed(root):
    """A page that took the whole budget to fetch and parse is not read, and no further page is fetched."""
    t = [0.0]

    class SlowParse:
        calls = []

        def get(self, url, refresh=False):
            self.calls.append(url)
            t[0] += 200                                   # this one page's parse ran past the 120 s budget
            return FakePages().get(url)

    p = SlowParse()
    s = MarketScout(root, readers=[rule_reader], pages=p, use_apify=False, clock=lambda: t[0])
    rep = MarketReport.model_validate_json((root / (await s.run(MarketRequest(request_id="b-1"))).filename)
                                           .read_text(encoding="utf-8"))
    assert p.calls == ["https://a.example/menu"] and rep.prices == []
    [note] = [x for x in rep.limitations if "scan budget" in x]
    assert "a.example/menu; fetched, not read" in note and "b.example" in note


# ---------------------------------------------------------------- p3: a bad Google Maps menu link
BAD_LINKS = ["www.nohost-example.com/menu", "http://a..b/menu", "ftp://x.example/menu", "http://x.example:8080/m",
             "https://" + "a" * 600 + ".example/", "http://user@x.example/", "https:///menu", 42, None]


@pytest.mark.parametrize("bad", BAD_LINKS)
def test_tidy_drops_an_unusable_menu_link(bad):
    row = competitors.tidy(dict(name="Evil Pizza", cuisine="Pizza", price_level="$$", distance_miles=0.3,
                                menu_url=bad))
    assert row["menu_url"] is None and competitors.judge(row) == (False, "dropped: no menu page")


@pytest.mark.parametrize("bad", ["www.nohost-example.com/menu", "http://a..b/menu"])
def test_poc_p3_a_cached_bad_menu_link_does_not_crash_the_scan(tmp_path, monkeypatch, bad):
    root = make_root(tmp_path)
    monkeypatch.setenv("APIFY_TOKEN", "dummy-not-used")
    monkeypatch.setattr(competitors, "from_apify", lambda *a: pytest.fail("the cache is fresh; no Apify run"))
    priv = root / "data" / "market_scout"
    priv.mkdir(parents=True, exist_ok=True)
    (priv / "apify_cache.json").write_text(json.dumps({
        "at": datetime.now(timezone.utc).isoformat(), "here": [HERE["lat"], HERE["lng"]],
        "rows": [dict(name="Evil Pizza", cuisine="Pizza", price_level="$$", distance_miles=0.3, menu_url=bad)]}),
        encoding="utf-8")
    result = asyncio.run(MarketScout(root, readers=[rule_reader], pages=FakePages()).run(MarketRequest(request_id="x1")))
    rep = MarketReport.model_validate_json((root / result.filename).read_text(encoding="utf-8"))
    [evil] = [c for c in rep.competitors if c.name == "Evil Pizza"]
    assert not evil.relevant and evil.reason == "dropped: no menu page" and evil.menu_url is None


@pytest.mark.parametrize("content", ["{not json", "[]", '{"at": 5, "rows": []}', '{"at": "2026-10-24", "rows": {}}',
                                     '{"at": "yesterday", "rows": []}', '{"rows": []}', '"x"', "null",
                                     '{"at": "2026-10-24T00:00:00+00:00", "here": [1, 2], "rows": [5, "x", null]}'])
def test_a_bad_apify_cache_is_ignored_and_refreshed(tmp_path, content):
    cache = tmp_path / "apify_cache.json"
    cache.write_text(content, encoding="utf-8")
    fresh = [dict(name="Fresh Pizza", cuisine="Pizza", price_level="$$", distance_miles=0.4,
                  menu_url="https://f.example/menu")]
    from unittest.mock import patch
    with patch.object(competitors, "from_apify", lambda *a: fresh):
        rows = competitors.apify_rows(dict(lat=1.0, lng=3.0), "token", cache)
    assert [r["name"] for r in rows] == ["Fresh Pizza"]
    assert json.loads(cache.read_text(encoding="utf-8"))["rows"][0]["name"] == "Fresh Pizza"


def test_one_row_that_breaks_is_skipped_not_the_scan(monkeypatch):
    real = competitors.tidy

    def tidy(r):
        if r.get("name") == "Boom":
            raise RuntimeError("boom")
        return real(r)
    monkeypatch.setattr(competitors, "tidy", tidy)
    rows = competitors.tidy_rows([dict(name="Boom"), dict(name="Fine", menu_url="https://f.example/m"), "x"])
    assert [r["name"] for r in rows] == ["Fine"]


# ---------------------------------------------------------------- p4: a seed site's redirect
def redirecting(seen):
    def handler(req):
        host = req.headers["host"]
        seen.append((host, req.url.path))
        if host == "b.example":                           # seed Beta Pizzeria's menu_url redirects off-domain
            return httpx.Response(302, headers={"location": "https://attacker.example/x"})
        if host == "attacker.example":
            return httpx.Response(200, headers={"content-type": "text/html"},
                                  content=b"<p>Margherita 29</p><p>Tiramisu 19</p><p>Lobster Ravioli 39</p>")
        pg = {"a.example": b"<p>Margherita</p><p>tomato, mozzarella, basil</p><p>$21</p>",
              "c.example": b"<div>MARGHERITA 23</div><div>Tiramisu 11</div>"}
        return httpx.Response(200, headers={"content-type": "text/html"}, content=pg.get(host, b"<p>x</p>"))
    return handler


async def test_poc_p4_off_site_redirect_is_unreviewed_not_trusted(root):
    seen = []
    s = MarketScout(root, readers=[rule_reader], pages=store(root, redirecting(seen)), use_apify=False)
    rep = MarketReport.model_validate_json((root / (await s.run(MarketRequest(request_id="r1"))).filename)
                                           .read_text(encoding="utf-8"))
    assert ("attacker.example", "/x") in seen
    assert not any(p.restaurant == "Beta Pizzeria" for p in rep.prices)            # before: 3 trusted prices
    beta = [p for p in rep.unreviewed if p.restaurant == "Beta Pizzeria"]
    assert {p.price_cents for p in beta} == {2900, 1900, 3900}
    assert all(p.source_url == "https://attacker.example/x" for p in beta)         # where it really came from
    marg = next(p for p in rep.positions if p.our_item == "Margherita Pizza")
    assert "Beta Pizzeria" not in marg.restaurants and marg.competitor_median_cents is None
    assert any("redirected to https://attacker.example/x, off the restaurant's site" in x for x in rep.limitations)
    with connect(root / "data" / "cost_price.db") as con:                          # and nothing stored under Beta
        stored = con.execute("SELECT o.source_url FROM competitor_observation o JOIN competitor c "
                             "ON c.id = o.competitor_id WHERE c.name = 'Beta Pizzeria'").fetchall()
    assert stored == []


@pytest.mark.parametrize("target", ["https://b.example/menu", "http://b.example/menu", "https://www.b.example/menu",
                                    "https://b.example/en/menu.html", "https://menu.b.example/"])
async def test_same_site_redirects_stay_trusted(root, target):
    def handler(req):
        if req.url.path == "/menu" and req.headers["host"] == "b.example" and req.url.scheme == "https" \
                and target != "https://b.example/menu":
            return httpx.Response(301, headers={"location": target})
        pg = {"a.example": PAGE_A, "c.example": PAGE_C}.get(req.headers["host"], PAGE_B)
        return httpx.Response(200, headers={"content-type": "text/html"}, content=pg)
    s = MarketScout(root, readers=[rule_reader], pages=store(root, handler), use_apify=False)
    rep = MarketReport.model_validate_json((root / (await s.run(MarketRequest(request_id="r2"))).filename)
                                           .read_text(encoding="utf-8"))
    beta = [p for p in rep.prices if p.restaurant == "Beta Pizzeria"]
    assert {p.price_cents for p in beta} == {1900, 1200, 2900}
    assert {p.source_url for p in beta} == {target}
    assert not any("off the restaurant's site" in x for x in rep.limitations)


PAGE_A = b"<p>Margherita</p><p>tomato, mozzarella, basil</p><p>$21</p>"
PAGE_B = b"<li>Pizza Margherita 19</li><li>Tiramisu 12</li><li>Lobster Ravioli 29</li>"
PAGE_C = b"<div>MARGHERITA 23</div><div>Tiramisu 11</div>"


def test_final_url_is_kept_on_fresh_and_cached_snapshots(tmp_path):
    def handler(req):
        if req.headers["host"] == "a.example":
            return httpx.Response(302, headers={"location": "https://www.a.example/carta"})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=PAGE_A)
    st = store(tmp_path, handler)
    fresh = st.get("https://a.example/menu")
    cached = st.get("https://a.example/menu")
    assert fresh.final_url == cached.final_url == "https://www.a.example/carta"
    assert fresh.url == cached.url == "https://a.example/menu"


def test_a_redirect_to_an_overlong_url_is_refused(tmp_path):
    long = "https://a.example/" + "x" * 600

    def handler(req):
        if req.url.path == "/menu":
            return httpx.Response(302, headers={"location": long})
        return httpx.Response(200, headers={"content-type": "text/html"}, content=PAGE_A)
    with pytest.raises(pages.FetchError, match="price's source"):
        store(tmp_path, handler).get("https://a.example/menu")


def test_seed_quotes_on_an_off_site_page_are_rejected():
    page = Snapshot("https://b.example/menu", None, "Tiramisu 12", T, final_url="https://attacker.example/x")
    seed = lambda *a: [dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1200, quote="Tiramisu 12",
                            service="unknown", match_confidence=0.9, obs_id=1)]
    seed.reader = "seed"
    beta = SimpleNamespace(name="Beta", menu_url="https://b.example/menu", found_via="seed")
    read = read_menu(page, beta, {"Tiramisu"}, [seed])
    assert read.accepted == [] and [c.reason for c in read.checks] == ["redirected off the restaurant's site"]


# ---------------------------------------------------------------- the bind host
async def test_effective_bind_host_after_make_agent_is_localhost(root, monkeypatch):
    """uagents 0.26.1 reads uagents.asgi.HOST inside ASGIServer.serve() (the only server it starts; the Bureau uses
    the same class), when it builds the uvicorn.Config. Run that serve() with uvicorn's server stubbed out."""
    import uagents.asgi
    from market_scout import agent as agent_mod

    monkeypatch.setenv("MARKET_SCOUT_SEED", "test seed " * 4)
    monkeypatch.delenv("MARKET_SCOUT_BIND_HOST", raising=False)
    monkeypatch.delenv("MARKET_SCOUT_MAILBOX", raising=False)
    monkeypatch.setenv("MARKET_SCOUT_LOCAL_CLIENT_SEED", "local client seed " * 3)
    monkeypatch.setattr(uagents.asgi, "HOST", "0.0.0.0")
    configs = []

    class NoServe:
        def __init__(self, config):
            configs.append(config)

        async def serve(self):
            return None
    monkeypatch.setattr(uagents.asgi.uvicorn, "Server", NoServe)
    a = agent_mod.make_agent(root, port=8993, local_only=True)
    assert isinstance(a._server, uagents.asgi.ASGIServer)
    await a._server.serve()
    assert [c.host for c in configs] == ["127.0.0.1"] and configs[0].port == 8993
    assert not a._use_mailbox


# ---------------------------------------------------------------- PDF limits
def lying_pdf(real_pages=500, claimed=1) -> bytes:
    """A PDF whose page tree says /Count 1 but whose /Kids list really holds `real_pages` pages."""
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, NumberObject

    w = PdfWriter()
    for _ in range(real_pages):
        w.add_blank_page(width=72, height=72)
    w._root_object["/Pages"].get_object()[NameObject("/Count")] = NumberObject(claimed)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def text_pdf(stream: bytes, pages_=1) -> bytes:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    w = PdfWriter()
    for _ in range(pages_):
        page = w.add_blank_page(width=612, height=792)
        font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                                 NameObject("/BaseFont"): NameObject("/Helvetica")})
        page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject(
            {NameObject("/F1"): w._add_object(font)})})
        content = DecodedStreamObject()
        content.set_data(stream)
        page[NameObject("/Contents")] = w._add_object(content)
    buf = io.BytesIO()
    w.write(buf)
    return buf.getvalue()


def bounded(f, *a, seconds=2.0, megabytes=200):
    tracemalloc.start()
    start = time.perf_counter()
    try:
        out = f(*a)
    except ValueError as exc:
        out = exc
    took = time.perf_counter() - start
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    assert took < seconds and peak < megabytes * 1024 * 1024, (took, peak)
    return out


def test_pdf_whose_count_lies_reads_at_most_the_page_cap(monkeypatch):
    import pypdf
    calls = []
    real = pypdf.PageObject.extract_text
    monkeypatch.setattr(pypdf.PageObject, "extract_text", lambda self, *a, **k: calls.append(1) or real(self, *a, **k))
    data = lying_pdf(500, claimed=1)
    out = bounded(pdf_text, data)
    assert isinstance(out, ValueError) and "no text layer" in str(out)
    assert len(calls) <= pages.MAX_PDF_PAGES
    out = bounded(pdf_text, lying_pdf(500, claimed=200))         # a believable /Count: still the 30-page cap
    assert isinstance(out, ValueError) and len(calls) <= 2 * pages.MAX_PDF_PAGES


def test_pdf_with_a_huge_content_stream_is_skipped_fast():
    """4 MB of text operators on one page took pypdf 67 s; the page is now measured first and skipped."""
    line = TEXT_LINE
    data = text_pdf(line * 60_000)
    assert len(data) < pages.MAX_BYTES
    out = bounded(pdf_text, data)
    assert isinstance(out, ValueError) and "too large" in str(out)


def test_pdf_flate_bomb_is_measured_without_inflating_it():
    import zlib
    from pypdf import PdfWriter
    from pypdf.generic import NameObject, StreamObject

    w = PdfWriter()
    page = w.add_blank_page(width=72, height=72)
    bomb = StreamObject()
    z = zlib.compressobj(9)
    chunk = b" " * (1024 * 1024)
    bomb._data = b"".join(z.compress(chunk) for _ in range(200)) + z.flush()   # 200 MB of spaces in ~200 KB
    bomb[NameObject("/Filter")] = NameObject("/FlateDecode")
    page[NameObject("/Contents")] = w._add_object(bomb)
    buf = io.BytesIO()
    w.write(buf)
    out = bounded(pdf_text, buf.getvalue(), megabytes=50)
    assert isinstance(out, ValueError) and "too large" in str(out)


TEXT_LINE = b"BT /F1 12 Tf 72 720 Td (Margherita 21 tomato mozzarella basil) Tj ET\n"


def test_pdf_just_under_the_content_cap_reads_in_time_and_text_is_capped():
    line = TEXT_LINE
    data = text_pdf(line * (pages.MAX_PDF_CONTENT // len(line) - 10), pages_=5)
    out = bounded(pdf_text, data, seconds=pages.PDF_DEADLINE + 1)
    assert isinstance(out, str) and "Margherita 21" in out and len(out) <= pages.MAX_PDF_TEXT
