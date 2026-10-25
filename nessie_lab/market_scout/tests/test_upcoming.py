"""cost_price schema v4: every market_summary names the menu date it compared (menu_as_of), latest_market only returns
today's comparison, and an approved change CH schedules from tomorrow shows up as `upcoming` instead of vanishing."""
import inspect
import json
from datetime import date, timedelta

import pytest

from conftest import FakePages
from cost_price import api
from cost_price.db import connect, transaction
from market_scout import runner
from market_scout.cli import table
from market_scout.contracts import MarketReport, MarketRequest
from market_scout.extract import rule_reader
from market_scout.runner import MarketScout

TODAY = date.today()
TOMORROW = TODAY + timedelta(days=1)
PASS = "scout test passphrase"


def scout(root):
    return MarketScout(root, readers=[rule_reader], pages=FakePages(), use_apify=False)


def report_of(root, result) -> MarketReport:
    return MarketReport.model_validate_json((root / result.filename).read_text(encoding="utf-8"))


def owner_key(db):
    from cost_price.owner import init_owner, unlock
    with connect(db, "owner") as con:
        init_owner(con, PASS)
        return unlock(con, PASS)


def approve(db, key, sid, subject):
    with connect(db, "changes") as con, transaction(con):
        return api.record_approval(con, key, suggestion_id=sid, subject=subject, decision="approved",
                                   decided_by="owner", decided_at=f"{TODAY.isoformat()}T08:00:00Z")


def reprice(db, key, item="Margherita Pizza", old=1750, new=1950, on=TOMORROW, sid="S1"):
    """The cost_price v3+ signed subject: it names the exact price_change row it allows."""
    row = dict(item=item, channel="dine_in", service="dinner", effective_from=on.isoformat(), price_cents=new)
    aid = approve(db, key, sid, {"suggestion_id": sid, "action": "reprice_item", "target": item,
                                 "new_price_cents": new, "planned_change": api.planned_change("price_change", row)})
    with connect(db, "changes") as con, transaction(con):
        api.log_change(con, change_id=f"C-{sid}-1", suggestion_id=sid, approval_id=aid, system="menu_prices", ref=item,
                       field="price_cents", old_value=old, new_value=new)
        api.add_price_change(con, change_id=f"C-{sid}-1", item=item, effective_from=on.isoformat(), price_cents=new)


def remove(db, key, item="Tiramisu", on=TOMORROW, sid="S2"):
    row = dict(item=item, effective_from=on.isoformat(), available=False)
    aid = approve(db, key, sid, {"suggestion_id": sid, "action": "remove_item", "target": item,
                                 "planned_change": api.planned_change("availability_change", row)})
    with connect(db, "changes") as con, transaction(con):
        api.log_change(con, change_id=f"C-{sid}-1", suggestion_id=sid, approval_id=aid, system="menu_prices", ref=item,
                       field="on_menu", old_value="yes", new_value="no")
        api.add_availability_change(con, change_id=f"C-{sid}-1", item=item, effective_from=on.isoformat(),
                                    available=False)


def test_scan_date_is_the_local_date_and_matches_latest_market(root, db, monkeypatch):
    """One date per scan, the machine's local date.today() (never UTC), the same default api.latest_market uses."""
    assert "date.today()" in inspect.getsource(api.latest_market)
    seen = []
    real = api.upcoming_changes
    monkeypatch.setattr(runner.api, "upcoming_changes", lambda con, on: seen.append(on) or real(con, on))
    menu = runner.api.menu
    monkeypatch.setattr(runner.api, "menu", lambda con, on, **kw: seen.append(on) or menu(con, on, **kw))
    report, _, _ = scout(root).scan(MarketRequest(request_id="d-1"), "M-d-1")
    assert seen == [TODAY.isoformat()] * 2 and report.menu_as_of == TODAY


async def test_scan_writes_menu_as_of_and_latest_market_returns_it(root, db):
    result = await scout(root).run(MarketRequest(request_id="m-1"))
    with connect(db) as con:
        rows = api.latest_market(con, "dinner")                  # default: today
        assert rows and {r["menu_as_of"] for r in rows} == {TODAY.isoformat()}
        assert {r["report_id"] for r in rows} == {result.report_id}
        assert rows == api.latest_market(con, "dinner", on=TODAY.isoformat())


async def test_an_old_as_of_scan_does_not_replace_todays_row(root, db):
    today = await scout(root).run(MarketRequest(request_id="t-1"))
    old = await scout(root).run(MarketRequest(request_id="t-2", as_of="2026-01-01"))
    with connect(db) as con:
        assert {r["report_id"] for r in api.latest_market(con, "dinner")} == {today.report_id}
        past = api.latest_market(con, "dinner", on="2026-01-01")
        assert {r["report_id"] for r in past} == {old.report_id}
        assert {r["menu_as_of"] for r in past} == {"2026-01-01"}


async def test_approved_reprice_from_tomorrow_shows_as_upcoming(root, db):
    reprice(db, owner_key(db))
    result = await scout(root).run(MarketRequest(request_id="u-1"))
    report = report_of(root, result)
    pos = {p.our_item: p for p in report.positions}["Margherita Pizza"]
    assert pos.our_price_cents == 1750 and pos.position == "below_market"          # today's menu, unchanged
    assert pos.upcoming_price_cents == 1950 and pos.upcoming_from == TOMORROW
    assert pos.competitor_median_cents == 2100 and pos.upcoming_position == "below_market"   # -7%: still below
    assert result.positions["Margherita Pizza"] == "below_market"
    assert result.upcoming == {"Margherita Pizza": {"price_cents": 1950, "from": TOMORROW.isoformat(),
                                                    "position": "below_market"}}
    others = [p for p in report.positions if p.our_item != "Margherita Pizza"]
    assert others and all(p.upcoming_price_cents is None and p.leaving_from is None for p in others)
    with connect(db) as con:                                   # the stored summary is today's menu too
        row = {r["our_item"]: r for r in api.latest_market(con, "dinner")}["Margherita Pizza"]
        assert row["our_price_cents"] == 1750 and row["menu_as_of"] == TODAY.isoformat()
    text = table(report)
    assert f"approved: $19.50 from {TOMORROW.isoformat()} -> below_market" in text


async def test_as_of_tomorrow_compares_the_approved_price_as_current(root, db):
    reprice(db, owner_key(db))
    result = await scout(root).run(MarketRequest(request_id="u-2", as_of=TOMORROW))
    pos = {p.our_item: p for p in report_of(root, result).positions}["Margherita Pizza"]
    assert pos.our_price_cents == 1950 and pos.upcoming_price_cents is None and result.upcoming == {}
    with connect(db) as con:
        assert api.latest_market(con, "dinner") == []                  # tomorrow's comparison isn't today's
        rows = {r["our_item"]: r for r in api.latest_market(con, "dinner", on=TOMORROW.isoformat())}
        assert rows["Margherita Pizza"]["our_price_cents"] == 1950


async def test_upcoming_removal_is_noted(root, db):
    remove(db, owner_key(db))
    result = await scout(root).run(MarketRequest(request_id="r-1"))
    report = report_of(root, result)
    pos = {p.our_item: p for p in report.positions}["Tiramisu"]
    assert pos.leaving_from == TOMORROW and pos.upcoming_price_cents is None
    assert result.upcoming == {"Tiramisu": {"leaving_from": TOMORROW.isoformat()}}
    assert f"leaving the menu on {TOMORROW.isoformat()}" in table(report)
    later = await scout(root).run(MarketRequest(request_id="r-2", as_of=TOMORROW))
    assert "Tiramisu" not in later.positions


def test_upcoming_ignores_other_channels_services_and_items():
    from market_scout.analyze import with_upcoming
    from market_scout.contracts import ItemPosition
    p = ItemPosition(our_item="Margherita Pizza", service="dinner", our_price_cents=1750, n_prices=3,
                     competitor_median_cents=2000, competitor_low_cents=1900, competitor_high_cents=2100,
                     price_gap_cents=-250, gap_pct=-0.125, position="below_market")
    base = dict(kind="price", item="Margherita Pizza", channel="dine_in", service="dinner", change_id="C")
    changes = [dict(base, channel="delivery", effective_from="2026-10-25", price_cents=2500),
               dict(base, service="lunch", effective_from="2026-10-25", price_cents=2400),
               dict(base, item="Tiramisu", effective_from="2026-10-25", price_cents=900),
               dict(base, effective_from="2026-10-26", price_cents=2000),
               dict(base, effective_from="2026-10-27", price_cents=2600)]
    out = with_upcoming([p], changes, "dinner", 0.05)[0]
    assert (out.upcoming_price_cents, out.upcoming_from.isoformat(), out.upcoming_position) == (2000, "2026-10-26", "at_par")
    assert out.position == "below_market"
