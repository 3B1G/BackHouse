import json

import pytest

from conftest import PAGES, FakePages
from market_scout.analyze import position
from market_scout.competitors import judge, level
from market_scout.contracts import MarketRequest
from market_scout.extract import prices_in, rule_reader, verify
from market_scout.menu import our_menu
from market_scout.pages import page_text
from market_scout.runner import MarketScout, immutable_json
from market_scout.shared import MarketSummary


def scout(root, pages=None):
    return MarketScout(root, readers=[rule_reader], pages=pages or FakePages(), use_apify=False)


def test_menu_uses_latest_price_and_skips_modifiers(db):
    menu, as_of = our_menu(db, "dinner", on="2026-10-24")
    assert menu == {"Margherita Pizza": 1750, "Lobster Ravioli": 2750, "Tiramisu": 1300}
    assert as_of == "2026-04-01"
    assert our_menu(db, "dinner", on="2026-01-01")[0]["Lobster Ravioli"] == 2700
    with pytest.raises(ValueError, match="not on our dinner menu"):
        our_menu(db, "dinner", ["Pizza Hawaii"])


def test_price_reading_ignores_sizes_years_and_scripts():
    assert prices_in('16 oz 2026 $21.50 call 415') == [2150]
    assert "99" not in page_text(PAGES["https://a.example/menu"])


def test_rule_reader_skips_ingredient_mentions_and_lookalikes():
    from market_scout.pages import Snapshot
    text = "\n".join(["Espresso Martini | 16", "add burrata $ 5", "burrata cheese, basil, sea salt 37.5",
                      "Margherita pizzetta | 10", "CAESAR 17.5", "Tiramisu $12", "Penne alla Vodka $27"])
    got = {(p["our_item"], p["price_cents"], p["match_confidence"])
           for p in rule_reader(Snapshot("u", None, text, None), "R", ["Espresso", "Burrata & Heirloom Tomato",
                                "Margherita Pizza", "Caesar Salad", "Tiramisu", "Rigatoni alla Vodka"])}
    assert got == {("Caesar Salad", 1750, 0.75), ("Tiramisu", 1200, 0.75), ("Rigatoni alla Vodka", 2700, 0.6)}


def test_verify_requires_quote_on_page_and_price_in_quote():
    page = FakePages().get("https://b.example/menu")
    assert verify(page, "Pizza Margherita 19", 1900) is None
    assert verify(page, "Pizza Margherita 17", 1700) == "quote not found on the saved page"
    assert verify(page, "Pizza Margherita", 1900) == "price is not written in the quote"


def test_relevance_rules():
    base = dict(name="X Trattoria", cuisine="Italian", price_level="$$", distance_miles=1.0, menu_url="u")
    assert judge(base)[0]
    assert not judge({**base, "price_level": "$$$$"})[0]
    assert not judge({**base, "distance_miles": 4})[0]
    assert not judge({**base, "name": "Olive Garden"})[0]
    assert not judge({**base, "name": "Sushi Place", "cuisine": "Japanese"})[0]
    assert level("$20–30") == "$$" and level("$100+") == "$$$$" and level(None) is None


def test_position_band():
    assert position(1750, 2100, 0.05) == "below_market"
    assert position(2050, 2100, 0.05) == "at_par"
    assert position(2750, 2500, 0.05) == "above_market"
    assert position(2750, None, 0.05) == "insufficient_data"


async def test_full_scan(root):
    result = await scout(root).run(MarketRequest(request_id="r1"))
    report = json.loads((root / result.filename).read_text(encoding="utf-8"))
    pos = {p["our_item"]: p for p in report["positions"]}
    assert pos["Margherita Pizza"]["competitor_median_cents"] == 2100       # 21, 19, 23; Delta ($$$$, 45) dropped
    assert pos["Margherita Pizza"]["position"] == "below_market"
    assert pos["Lobster Ravioli"]["competitor_median_cents"] == 3000
    assert pos["Tiramisu"]["position"] == "insufficient_data"                # only 2 prices
    assert any(r["quote"] == "Tiramisu 15" for r in report["rejected"])      # the bad seed quote is thrown out
    assert {c["name"]: c["relevant"] for c in report["competitors"]}["Delta Fine Dining"] is False
    for s in report["summaries"]:
        MarketSummary.model_validate(s)                                      # the shared contract accepts it
    assert result.positions["Margherita Pizza"] == "below_market"


async def test_same_request_id_returns_same_report_and_rejects_changes(root):
    s = scout(root)
    a = await s.run(MarketRequest(request_id="r2"))
    assert a == await s.run(MarketRequest(request_id="r2"))
    with pytest.raises(ValueError, match="reused"):
        await s.run(MarketRequest(request_id="r2", service="lunch"))


async def test_blocked_site_is_a_limitation_not_a_failure(root):
    pages = {k: v for k, v in PAGES.items() if k != "https://c.example/menu"}
    result = await scout(root, FakePages(pages)).run(MarketRequest(request_id="r3"))
    report = json.loads((root / result.filename).read_text(encoding="utf-8"))
    assert any("Gamma Osteria" in x for x in report["limitations"])
    assert result.positions["Margherita Pizza"] == "insufficient_data"


def test_immutable_json_never_overwrites(tmp_path):
    immutable_json(tmp_path / "a.json", {"x": 1})
    with pytest.raises(FileExistsError):
        immutable_json(tmp_path / "a.json", {"x": 2})
    assert json.loads((tmp_path / "a.json").read_text()) == {"x": 1}


def test_review_regressions_price_reading():
    assert prices_in('12" Margherita 18') == [1800]
    assert prices_in("6 pieces 14") == [1400]
    assert prices_in("open 5:30pm") == []
    assert prices_in("B12 combo") == []
    assert prices_in("2 for $20") == []


def test_review_regressions_verify():
    from market_scout.pages import Snapshot
    page = Snapshot("u", None, "Diavola 22\nMargherita 18.50\nMargherita 185g 24", None)
    assert verify(page, "22", 2200, "Margherita Pizza", "22") == "dish name is not in the quote"
    assert verify(page, "Margherita 18", 1800, "Margherita Pizza", "Margherita") == "quote not found on the saved page"
    assert verify(page, "Margherita 18.50", 1850, "Margherita Pizza", "Margherita") is None


def test_review_regressions_lookahead_and_lookalikes():
    from market_scout.pages import Snapshot
    page = Snapshot("u", None, "Margherita\nLasagna 26\nEspresso Rubbed Steak 34", None)
    assert rule_reader(page, "R", ["Margherita Pizza", "Espresso"]) == []


def test_bad_proposal_does_not_block_a_good_one():
    from market_scout.extract import read_menu
    from market_scout.contracts import Competitor
    page = FakePages().get("https://b.example/menu")
    c = Competitor(name="Beta", found_via="seed", relevant=True, reason="x")
    bad = lambda *a: [dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1200, quote="Tiramisu 12 nope",
                           service="unknown", match_confidence=0.9)]
    read = read_menu(page, c, {"Tiramisu"}, [bad, rule_reader])
    assert [a.price.price_cents for a in read.accepted] == [1200] and len(read.rejected) == 1


async def test_crash_between_report_and_receipt_recovers(root):
    s = scout(root)
    first = await s.run(MarketRequest(request_id="r9"))
    next((root / "data" / "market_reports" / "requests").glob("*.json")).unlink()     # simulate the crash
    assert await s.run(MarketRequest(request_id="r9")) == first
