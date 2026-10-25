"""Anti-poisoning (ASI proposals, outlier filters), the cost_price.db round trip, and the agent's request guards."""
import json
from datetime import datetime, timezone

import httpx
import pytest
from pydantic import ValidationError

from conftest import PAGES, FakePages, make_root, sha
from cost_price import api
from cost_price.db import connect, transaction

try:                                                      # cost_price moved verify() to its own module
    from cost_price.verify import verify as _verify

    def verify_db(db):
        return _verify(db, require_seal=False)["problems"]
except ImportError:
    from cost_price.db import verify as verify_db
from market_scout.analyze import analyze, in_band, mad_filter
from market_scout.contracts import Competitor, MarketRequest
from market_scout.extract import Accepted, ASIReader, nearest_price, parse_asi, read_menu, rule_reader
from market_scout.pages import Snapshot
from market_scout.runner import MarketScout
from market_scout.shared import CompetitorPrice
from market_scout.store import clean

BETA = Competitor(name="Beta", found_via="seed", relevant=True, reason="x")


def scout(root, pages=None, readers=(rule_reader,), **kw):
    return MarketScout(root, readers=list(readers), pages=pages or FakePages(), use_apify=False, **kw)


def asi(*items):
    def read(page, restaurant, wanted):
        return [dict(service="unknown", match_confidence=0.95, **x) for x in items]
    read.reader = "asi"
    return read


def snap(text):
    return Snapshot("https://b.example/menu", None, text, datetime(2026, 10, 24, tzinfo=timezone.utc), "a" * 64)


# ---------------------------------------------------------------- ASI proposals
def test_asi_quote_is_capped_at_160_characters():
    long = "Tiramisu 12 " + "mascarpone, cocoa, ladyfingers, espresso " * 4
    page = snap(long.strip())
    read = read_menu(page, BETA, {"Tiramisu"}, [asi(dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1200,
                                                         quote=long.strip()))])
    assert read.accepted == [] and "longer than 160" in read.rejected[0][2]


def test_asi_price_must_be_the_one_nearest_the_dish_name():
    assert nearest_price("Calzone 24 Margherita 18", "Margherita Pizza") == 1800
    assert nearest_price("Margherita 18 Calzone 24", "Margherita Pizza") == 1800
    assert nearest_price("18 Margherita | Calzone 24", "Margherita Pizza") == 1800
    page = snap("Calzone 24 Margherita 18")
    read = read_menu(page, BETA, {"Margherita Pizza"}, [asi(dict(
        our_item="Margherita Pizza", their_item="Margherita", price_cents=2400, quote="Calzone 24 Margherita 18"))])
    assert read.accepted == [] and "nearest the dish name" in read.rejected[0][2]


def test_asi_confidence_is_capped_unless_the_rule_reader_agrees():
    page = snap("Tiramisu 12\nPanna Cotta 9")
    read = read_menu(page, BETA, {"Tiramisu", "Panna Cotta"}, [rule_reader, asi(
        dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1200, quote="Tiramisu 12"),
        dict(our_item="Panna Cotta", their_item="Panna Cotta", price_cents=900, quote="Panna Cotta 9"))])
    by = {(a.price.our_item, a.reader): a.price.match_confidence for a in read.accepted}
    assert by[("Tiramisu", "rule")] == 0.75                 # ASI's agreeing Tiramisu collapses onto the rule price
    assert ("Tiramisu", "asi") not in by
    # Panna Cotta: rule reader also finds it, so ASI is a duplicate; drop the rule reader and ASI stands alone
    alone = read_menu(page, BETA, {"Panna Cotta"}, [asi(
        dict(our_item="Panna Cotta", their_item="Panna Cotta", price_cents=900, quote="Panna Cotta 9"))])
    assert [a.price.match_confidence for a in alone.accepted] == [0.65]
    agreed = read_menu(snap("Tiramisu 12"), BETA, {"Tiramisu"}, [
        lambda *a: [dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1200, quote="Tiramisu 12",
                         service="dinner", match_confidence=0.9)],     # an unnamed reader counts as untrusted
        rule_reader])
    assert {a.reader for a in agreed.accepted} == {"asi"} and agreed.accepted[0].price.match_confidence == 0.65


def test_asi_confidence_kept_when_rule_reader_proposed_the_same_price():
    page = snap("Tiramisu 12")
    rule_dinner = lambda *a: [dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1200, quote="Tiramisu 12",
                                   service="unknown", match_confidence=0.6)]
    rule_dinner.reader = "rule"
    read = read_menu(page, BETA, {"Tiramisu"}, [rule_dinner, asi(
        dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1200, quote="Tiramisu 12"))])
    assert [(a.reader, a.price.match_confidence) for a in read.accepted] == [("rule", 0.6)]
    assert [c.status for c in read.checks] == ["verified", "verified"]


@pytest.mark.parametrize("payload", [
    {}, {"choices": []}, {"choices": [{}]}, {"choices": [{"message": {}}]}, {"choices": [{"message": {"content": 5}}]},
    {"choices": [{"message": {"content": "not json"}}]}, {"choices": [{"message": {"content": "[1, 2]"}}]},
    {"choices": [{"message": {"content": json.dumps({"items": "nope"})}}]}, [], "x", None,
    {"choices": [{"message": {"content": json.dumps({"items": [
        {"our_item": "Tiramisu", "their_item": "Tiramisu", "price": 1e400, "quote": "Tiramisu 12"},
        {"our_item": "Tiramisu", "their_item": "Tiramisu", "price": "1e999", "quote": "Tiramisu 12"},
        {"our_item": "Tiramisu", "their_item": "Tiramisu", "price": float("nan"), "quote": "Tiramisu 12"},
        {"our_item": ["Tiramisu"], "their_item": "Tiramisu", "price": 12, "quote": "Tiramisu 12"},
        {"our_item": "Tiramisu", "their_item": {"x": 1}, "price": 12, "quote": "Tiramisu 12"},
        {"our_item": "Tiramisu", "their_item": "Tiramisu", "price": [12], "quote": "Tiramisu 12"},
        {"our_item": "Tiramisu", "their_item": "Tiramisu", "price": True, "quote": "Tiramisu 12"},
        {"our_item": "Tiramisu", "their_item": "Tiramisu", "price": 12, "quote": None},
        "a string", 7, None]})}}]},
])
def test_asi_parsing_never_crashes(payload):
    assert parse_asi(payload) == []


def test_asi_parsing_keeps_good_rows_next_to_bad_ones():
    content = json.dumps({"items": [{"our_item": "Tiramisu", "their_item": "Tiramisu", "price": 1e400, "quote": "q"},
                                    {"our_item": "Tiramisu", "their_item": "Tiramisu", "price": "12.5",
                                     "quote": "Tiramisu 12.5", "match_confidence": [1], "service": ["dinner"]}]})
    got = parse_asi({"choices": [{"message": {"content": content}}]})
    assert got == [dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1250, quote="Tiramisu 12.5",
                        service="unknown", match_confidence=0.7)]


def test_asi_reader_handles_a_non_json_http_body():
    reader = ASIReader(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"<html>")))
    assert reader(snap("Tiramisu 12"), "Beta", {"Tiramisu"}) == []
    bad = ASIReader(api_key="k", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
    with pytest.raises(RuntimeError):
        bad(snap("Tiramisu 12"), "Beta", {"Tiramisu"})


async def test_a_crashing_reader_does_not_sink_the_scan(root):
    def boom(page, restaurant, items):
        if "b.example" in page.url:
            raise OverflowError("cannot convert float infinity to integer")
        return None                                       # not a list: ignored
    boom.reader = "asi"
    junk = lambda *a: [None, 7, {"our_item": "Tiramisu"}, {"our_item": "Nope", "price_cents": 1}]
    result = await scout(root, readers=[rule_reader, boom, junk]).run(MarketRequest(request_id="crash-1"))
    report = json.loads((root / result.filename).read_text(encoding="utf-8"))
    assert result.positions["Margherita Pizza"] == "below_market"
    assert any("asi reader failed" in x and "OverflowError" in x for x in report["limitations"])
    assert any(r["reason"] == "malformed proposal" for r in report["rejected"])


async def test_a_page_that_breaks_reading_is_a_limitation(root):
    class Exploding(FakePages):
        def get(self, url, refresh=False):
            page = super().get(url, refresh)
            if "c.example" in url:
                page.text = None                          # anything that makes the readers or checks blow up
                page.all_text = None
            return page

    result = await scout(root, Exploding()).run(MarketRequest(request_id="crash-2"))
    report = json.loads((root / result.filename).read_text(encoding="utf-8"))
    assert any(x.startswith("Gamma Osteria: menu not read") for x in report["limitations"])


# ---------------------------------------------------------------- analysis filters
def price(restaurant, cents, item="Margherita Pizza", conf=0.9):
    return CompetitorPrice(our_item=item, restaurant=restaurant, their_item="Margherita", price_cents=cents,
                           service="dinner", source_url="https://x.example/", source_type="restaurant_site",
                           quote=f"Margherita {cents / 100:g}", match_confidence=conf,
                           retrieved_at=datetime(2026, 10, 24, tzinfo=timezone.utc))


def test_prices_far_from_ours_are_dropped_before_the_median():
    got = [Accepted(price(n, c), "seed") for n, c in [("A", 699), ("B", 2000), ("C", 2100), ("D", 2200), ("E", 4400)]]
    kept, dropped = in_band(1750, got)
    assert [a.price.price_cents for a in kept] == [2000, 2100, 2200]
    assert [a.price.price_cents for a, _ in dropped] == [699, 4400] and "outside 0.6x-1.7x" in dropped[0][1]
    assert len(in_band(1000, [Accepted(price("A", 600), "seed"), Accepted(price("B", 1700), "seed")])[0]) == 2


def test_mad_filter_drops_outliers_and_uses_25_percent_when_mad_is_zero():
    got = [Accepted(price(n, c), "seed") for n, c in [("A", 2000), ("B", 2100), ("C", 2150), ("D", 2200),
                                                      ("E", 3900)]]
    kept, dropped = mad_filter(got)
    assert [a.price.price_cents for a in kept] == [2000, 2100, 2150, 2200]
    assert [a.price.price_cents for a, _ in dropped] == [3900] and "MAD" in dropped[0][1]
    same = [Accepted(price(n, 2000), "seed") for n in "ABC"] + [Accepted(price("D", 3000), "seed")]
    assert len(mad_filter(same)[0]) == 3                 # MAD 0: more than 25% from the median is dropped
    assert len(mad_filter(same[:2])[0]) == 2             # fewer than 3 prices: nothing to compare against


def test_median_needs_three_trusted_prices():
    menu = {"Margherita Pizza": 1750}
    two_asi = [Accepted(price("A", 2000), "seed", "seed"), Accepted(price("B", 2100), "asi", "seed"),
               Accepted(price("C", 2200), "asi", "seed")]
    pos, summ, _, unreviewed = analyze(menu, two_asi)
    assert pos[0].position == "insufficient_data" and pos[0].n_prices == 1 and summ[0].competitor_median_cents is None
    assert [p.restaurant for p in unreviewed] == ["B", "C"] and pos[0].n_unreviewed == 2
    trusted = [two_asi[0], Accepted(price("B", 2100), "rule", "seed"), Accepted(price("C", 2200), "rule", "seed")]
    assert analyze(menu, trusted)[0][0].competitor_median_cents == 2100
    filtered = [*trusted, Accepted(price("D", 9000), "seed", "seed")]
    pos, _, dropped, _ = analyze(menu, filtered)
    assert pos[0].n_prices == 3 and [p.price_cents for p, _ in dropped] == [9000]
    assert analyze(menu, [Accepted(a.price, a.reader) for a in trusted])[0][0].position == "insufficient_data"


async def test_filtered_prices_are_listed_in_the_report(root):
    pages = {**PAGES, "https://b.example/menu": "<li>Pizza Margherita 99</li><li>Tiramisu 12</li>"}
    result = await scout(root, FakePages(pages)).run(MarketRequest(request_id="filt-1"))
    report = json.loads((root / result.filename).read_text(encoding="utf-8"))
    hit = [r for r in report["rejected"] if r["restaurant"] == "Beta Pizzeria" and r["our_item"] == "Margherita Pizza"]
    assert hit and "outside 0.6x-1.7x" in hit[0]["reason"]
    assert result.positions["Margherita Pizza"] == "insufficient_data"          # 21 and 23 left


# ---------------------------------------------------------------- cost_price.db round trip
async def test_scan_writes_checks_observations_and_summaries(root, db):
    result = await scout(root).run(MarketRequest(request_id="db-1"))
    with connect(db) as con:
        seed = {o["quote"]: o for o in api.observations(con, reader="seed")}
        checks = {r["observation_id"]: dict(r) for r in con.execute("SELECT * FROM observation_check")}
        assert seed["Margherita tomato, mozzarella, basil $21"]["status"] == "verified"
        assert seed["Tiramisu 15"]["status"] == "rejected"
        assert checks[seed["Tiramisu 15"]["id"]]["reason"] == "quote not found on the saved page"
        a = checks[seed["Margherita tomato, mozzarella, basil $21"]["id"]]
        assert a["snapshot_sha256"] == sha(PAGES["https://a.example/menu"]) and a["report_id"] == result.report_id
        rule = api.observations(con, reader="rule")
        assert {(o["our_item"], o["price_cents"]) for o in rule} >= {("Margherita Pizza", 1900),
                                                                    ("Lobster Ravioli", 3000)}
        assert all(o["status"] == "verified" and o["report_id"] == result.report_id for o in rule)
        assert not any(o["competitor_id"] == 4 for o in rule)          # Delta is not relevant: never read
        market = {m["our_item"]: m for m in api.latest_market(con)}
        assert market["Margherita Pizza"]["competitor_median_cents"] == 2100
        assert market["Margherita Pizza"]["position"] == "below_market"
        assert market["Tiramisu"]["position"] == "insufficient_data"
    assert verify_db(db) == []


async def test_rescans_add_checks_not_duplicate_observations(root, db):
    s = scout(root)
    await s.run(MarketRequest(request_id="db-2"))
    await s.run(MarketRequest(request_id="db-3"))
    with connect(db) as con:
        n_rule = con.execute("SELECT count(*) FROM competitor_observation WHERE reader='rule'").fetchone()[0]
        assert n_rule == len({(o["our_item"], o["price_cents"], o["quote"]) for o in api.observations(con, reader="rule")})
        assert con.execute("SELECT count(DISTINCT report_id) FROM market_summary").fetchone()[0] == 2
    assert verify_db(db) == []


async def test_menu_picks_up_an_approved_price_change(root, db):
    from cost_price.owner import init_owner, unlock                # cost_price's signed-approval API
    with connect(db, "owner") as con:
        init_owner(con, "scout test passphrase")
        key = unlock(con, "scout test passphrase")
    with connect(db, "changes") as con, transaction(con):
        # cost_price schema v3: the signed subject names the exact price_change row it allows
        row = dict(item="Margherita Pizza", channel="dine_in", service="dinner", effective_from="2026-10-24",
                   price_cents=2200)
        approval = api.record_approval(con, key, suggestion_id="S1", decision="approved", decided_by="owner",
                                       subject={"suggestion_id": "S1", "action": "reprice_item",
                                                "target": "Margherita Pizza", "new_price_cents": 2200,
                                                "planned_change": api.planned_change("price_change", row)},
                                       decided_at="2026-10-24T22:00:00Z")
        api.log_change(con, change_id="C-S1-1", suggestion_id="S1", approval_id=approval, system="menu_prices",
                       ref="Margherita Pizza", field="price_cents", old_value=1750, new_value=2200)
        api.add_price_change(con, change_id="C-S1-1", item="Margherita Pizza", effective_from="2026-10-24",
                             price_cents=2200)
    result = await scout(root).run(MarketRequest(request_id="ch-1", as_of="2026-10-24"))
    report = json.loads((root / result.filename).read_text(encoding="utf-8"))
    pos = {p["our_item"]: p for p in report["positions"]}
    assert pos["Margherita Pizza"]["our_price_cents"] == 2200 and pos["Margherita Pizza"]["position"] == "at_par"
    before = await scout(root).run(MarketRequest(request_id="ch-2", as_of="2026-09-01"))
    assert before.positions["Margherita Pizza"] == "below_market"


async def test_apify_competitors_are_added_to_the_database(root, db, monkeypatch):
    from market_scout import competitors
    found = [dict(name="Epsilon Pizza", address="1 Main", neighborhood="Marina", cuisine="Pizza​ restaurant",
                  price_level="$$", distance_miles=0.5, menu_url="https://e.example/menu",
                  found_via="apify_google_maps", closed=False),
             dict(name="Alpha Trattoria", menu_url="https://a.example/menu", cuisine="Italian", price_level="$$",
                  distance_miles=0.4, found_via="apify_google_maps"),             # already known: merged
             dict(name="Sushi Zen", menu_url="https://s.example/", cuisine="Japanese", price_level="$$",
                  distance_miles=0.3, found_via="apify_google_maps")]             # not relevant: not stored
    monkeypatch.setattr(competitors, "from_apify", lambda here, token: found)
    monkeypatch.setenv("APIFY_TOKEN", "t")
    pages = {**PAGES, "https://e.example/menu": "<li>Margherita 22</li>"}
    s = MarketScout(root, readers=[rule_reader], pages=FakePages(pages), use_apify=None)
    await s.run(MarketRequest(request_id="apify-1"))
    with connect(db) as con:
        rows = {c["name"]: c for c in api.competitors(con)}
        assert rows["Epsilon Pizza"]["found_via"] == "apify_google_maps" and rows["Epsilon Pizza"]["domain"] == "e.example"
        assert rows["Epsilon Pizza"]["cuisine"] == "Pizza restaurant"                     # zero-width space removed
        assert "Sushi Zen" not in rows and len(rows) == 5
        eps = [o for o in api.observations(con, competitor_id=rows["Epsilon Pizza"]["id"])]
        assert [(o["price_cents"], o["reader"]) for o in eps] == [(2200, "rule")]
    assert verify_db(db) == []


def test_text_is_sanitized_before_it_is_stored():
    assert clean("Marg​herita‮ $21\x07\nnext\ttab", 300) == "Margherita $21\nnexttab"
    assert clean("ﬁnocchiona", 120) == "finocchiona"                                    # NFKC
    assert clean("x" * 500, 300) == "x" * 300
    assert clean("​​", 120) is None and clean(None, 120) is None


async def test_long_and_dirty_quotes_are_stored_cleaned_and_capped(root, db):
    dish = "Lobster Ravioli​ " + "with brown butter, sage, lemon zest, parmigiano and black pepper, " * 6
    pages = {**PAGES, "https://b.example/menu": f"<p>{dish}</p><p>29</p><li>Pizza Margherita 19</li>"}
    weird = lambda *a: [dict(our_item="Lobster Ravioli", their_item="Lobster‮ Ravioli " + "z" * 200,
                             price_cents=2900, quote=dish + "29", service="unknown", match_confidence=0.9)]
    weird.reader = "rule"
    await scout(root, FakePages(pages), readers=[weird]).run(MarketRequest(request_id="dirty-1"))
    with connect(db) as con:
        rows = [o for o in api.observations(con, reader="rule") if o["our_item"] == "Lobster Ravioli"]
    assert rows, "the price should have verified"
    for o in rows:
        assert len(o["quote"]) <= 300 and len(o["their_item"]) <= 120
        assert "​" not in o["quote"] + o["their_item"] and "‮" not in o["their_item"]
    assert verify_db(db) == []


async def test_seed_source_url_is_honoured(tmp_path):
    seed = {"retrieved_at": "2026-10-24T21:16:13+00:00", "restaurants": [
        dict(name="Alpha Trattoria", cuisine="Italian", price_level="$$", approx_distance_miles=0.4,
             menu_url="https://a.example/menu",
             items=[dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1200, quote="Tiramisu 12",
                         source_url="https://a.example/dolci")])]}
    root = make_root(tmp_path, seed)
    pages = FakePages({**PAGES, "https://a.example/dolci": "<p>Tiramisu 12</p>"})
    await scout(root, pages).run(MarketRequest(request_id="src-1"))
    assert "https://a.example/dolci" in pages.calls
    with connect(root / "data" / "cost_price.db") as con:
        [o] = api.observations(con, reader="seed")
    assert o["status"] == "verified"


def test_seed_quotes_may_sit_in_a_hidden_menu_tab_but_reader_quotes_may_not():
    from market_scout.pages import page_text
    html = '<div>Tiramisu 12</div><div style="display:none">Panna Cotta 9</div>'
    page = Snapshot("u", None, page_text(html), datetime(2026, 10, 24, tzinfo=timezone.utc), None,
                    page_text(html, include_hidden=True))
    seed = lambda *a: [dict(our_item="Panna Cotta", their_item="Panna Cotta", price_cents=900, quote="Panna Cotta 9",
                            service="unknown", match_confidence=0.9, obs_id=1)]
    seed.reader = "seed"
    rule = lambda *a: [dict(our_item="Panna Cotta", their_item="Panna Cotta", price_cents=900, quote="Panna Cotta 9",
                            service="unknown", match_confidence=0.9)]
    rule.reader = "rule"
    assert [c.status for c in read_menu(page, BETA, {"Panna Cotta"}, [seed]).checks] == ["verified"]
    assert [c.status for c in read_menu(page, BETA, {"Panna Cotta"}, [rule]).checks] == ["rejected"]
    assert rule_reader(page, "Beta", ["Panna Cotta"]) == []


# ---------------------------------------------------------------- request validation
@pytest.mark.parametrize("bad", [dict(request_id=""), dict(request_id="a" * 65), dict(request_id="a b"),
                                 dict(request_id="../x"), dict(request_id="x\n"), dict(request_id="ü"),
                                 dict(request_id="r", items=["x"] * 51), dict(request_id="r", items=[""]),
                                 dict(request_id="r", items=["x" * 81])])
def test_request_limits(bad):
    with pytest.raises(ValidationError):
        MarketRequest(**bad)


def test_request_limits_allow_the_edges():
    MarketRequest(request_id="A-z_0.9" + "a" * 57, items=["x" * 80] * 50)
