"""Tests of the orchestrator's core (session, tools, controller, brief, cli) on tmp roots, built the way the sibling
packages' conftests build theirs: a small books.db, a fake Nessie sync DB, cost_price.db built from them with a
made-up seed list, and menu pages saved through an httpx.MockTransport (then read offline). The Forecaster is
replaced by a small deterministic fake (the real one needs the full generated books); the `realdata` test runs the
real one on a copy of nessie_lab/data.

These tests live outside tests/ on purpose: tests/conftest.py installs fake core modules for the LLM-side tests."""
import json
import os
import socket
import sqlite3
from datetime import date, timedelta
from pathlib import Path

import httpx
import pytest

from cost_price.build import build
from market_scout.pages import PageStore

HERE = dict(location="CHS", name="Demo Bistro Chestnut", street_number="0", street_name="Chestnut St (SIMULATED)",
            zip="94123", neighborhood="Marina", cuisine="Italian trattoria", lat=37.8008, lng=-122.438, seats=60,
            closed_dow="[]")
MENU = [("Margherita Pizza", "main", "dine_in", "dinner", "2025-09-10", 1750),
        ("Lobster Ravioli", "main", "dine_in", "dinner", "2026-04-01", 2750),
        ("Tiramisu", "dessert", "dine_in", "dinner", "2025-09-10", 1300),
        ("Add Grilled Chicken", "modifier", "dine_in", "dinner", "2025-09-10", 600)]
PAGES = {
    "https://a.example/menu": "<h2>Pizze</h2><p>Margherita</p><p>tomato, mozzarella, basil</p><p>$21</p>",
    "https://b.example/menu": "<li>Pizza Margherita 19</li><li>Tiramisu 12</li>",
    "https://c.example/menu": "<div>MARGHERITA &mdash; 23</div><div>Tiramisu &ndash; 11</div>",
}
SEED = {"retrieved_at": "2026-10-24T21:16:13+00:00", "restaurants": [
    dict(name="Alpha Trattoria", cuisine="Italian", price_level="$$", approx_distance_miles=0.4,
         menu_url="https://a.example/menu",
         items=[dict(our_item="Margherita Pizza", their_item="Margherita", price_cents=2100,
                     quote="Margherita tomato, mozzarella, basil $21")]),
    dict(name="Beta Pizzeria", cuisine="Pizza", price_level="$$", approx_distance_miles=0.9,
         menu_url="https://b.example/menu", items=[]),
    dict(name="Gamma Osteria", cuisine="Italian", price_level="$$", approx_distance_miles=1.6,
         menu_url="https://c.example/menu", items=[]),
]}
BANK_TX = [  # nessie_id, account, nessie_type, direction, date, amount, counterparty, memo, bank_id, status
    ("n-1", "CHS", "deposit", "in", "2026-10-20", 500000, None, "TABLETAP SETTLEMENT", "T0000000A1", "completed"),
    ("n-2", "CHS", "purchase", "out", "2026-10-21", 34900, "TableTap", "TABLETAP POS HARDWARE", "T0000000A2", "completed"),
    ("n-3", "RES", "deposit", "in", "2026-10-22", 100000, None, "TRANSFER IN", "T0000000A3", "completed"),
]
ACCOUNTS = [("CHS", "acct-chs", "Checking", "Chestnut Operating | cents", 1000000),
            ("RES", "acct-res", "Savings", "Reserve | cents", 2500000)]


def bills(today: date):
    d = [today + timedelta(days=n) for n in (2, 6, 30)]
    return [("CHS", "b-1", "pending", "Mission Dry Goods [SIM]", f"MDG-CHS-1 due {d[0]}", str(d[0]), 1, None, 48000, "x"),
            ("CHS", "b-2", "pending", "Bay Produce Co [SIM]", f"BPC-CHS-9 due {d[1]}", str(d[1]), 1, None, 125050, "x"),
            ("CHS", "b-3", "pending", "Mission Dry Goods [SIM]", f"MDG-CHS-2 due {d[2]}", str(d[2]), 1, None, 9900, "x"),
            ("CHS", "b-4", "recurring", "Mission Dry Goods [SIM]", "autopay", str(d[1]), 1, None, 7000, "x")]


class Resolver:
    def __call__(self, host, port):
        return ["93.184.216.34"]


def _handler(request):
    url = f"https://{request.headers['host']}{request.url.path}"
    if url not in PAGES:
        return httpx.Response(404)
    return httpx.Response(200, content=PAGES[url].encode(), headers={"content-type": "text/html; charset=utf-8"})


def make_root(tmp_path: Path) -> Path:
    from orchestrator.session import Session

    (tmp_path / "data").mkdir()
    (tmp_path / "config.py").write_text("USAGE_VARIANCE = 1.08\n", encoding="utf-8")
    con = sqlite3.connect(tmp_path / "data" / "books.db")
    con.execute(f"CREATE TABLE locations ({', '.join(HERE)})")
    con.execute(f"INSERT INTO locations VALUES ({', '.join('?' * len(HERE))})", list(HERE.values()))
    con.execute("CREATE TABLE accounts (account, type, nickname, last4, opening_balance_cents INTEGER, opening_as_of)")
    con.executemany("INSERT INTO accounts VALUES (?,?,?,?,?,?)",
                    [(a, t, n.split(" |")[0], "0000", c, "2026-10-01") for a, _, t, n, c in ACCOUNTS])
    con.execute("CREATE TABLE menu_prices (item, course, channel, service, effective_from, price_cents)")
    con.executemany("INSERT INTO menu_prices VALUES (?,?,?,?,?,?)", MENU)
    con.executescript("""
        CREATE TABLE ingredients (ingredient, unit, supplier, pack_size);
        INSERT INTO ingredients VALUES ('flour','lb','Mission Dry Goods',50), ('lobster','lb','Northbay Protein Co',5),
            ('mascarpone','lb','Sonoma Dairy Supply',5);
        CREATE TABLE invoices (invoice_no, supplier, category, location, invoice_date, due_date, terms_days, total);
        INSERT INTO invoices VALUES ('I1','x','x','CHS','2026-09-03','2026-10-03',30,0);
        CREATE TABLE invoice_lines (invoice_no, ingredient, unit, qty, unit_price, line_total);
        INSERT INTO invoice_lines VALUES ('I1','flour','lb',100,0.50,5000), ('I1','lobster','lb',10,46.00,46000),
            ('I1','mascarpone','lb',10,6.00,6000);
        CREATE TABLE recipes (item, ingredient, qty, unit);
        INSERT INTO recipes VALUES ('Margherita Pizza','flour',0.5,'lb'), ('Lobster Ravioli','lobster',0.5,'lb'),
            ('Tiramisu','mascarpone',0.2,'lb');
    """)
    con.commit()
    con.close()
    con = sqlite3.connect(tmp_path / "data" / "nessie_sync_boh-1loc.db")
    con.execute("CREATE TABLE bank_tx (nessie_id, account, nessie_type, direction, date, amount INTEGER, counterparty, "
                "memo, bank_id, status)")
    con.executemany("INSERT INTO bank_tx VALUES (?,?,?,?,?,?,?,?,?,?)", BANK_TX)
    con.execute("CREATE TABLE bank_accounts (account, nessie_id, type, nickname, opening_balance_cents INTEGER)")
    con.executemany("INSERT INTO bank_accounts VALUES (?,?,?,?,?)", ACCOUNTS)
    con.execute("CREATE TABLE bank_bills (account, nessie_id, status, payee, nickname, payment_date, recurring_date, "
                "upcoming_payment_date, payment_amount INTEGER, creation_date)")
    con.executemany("INSERT INTO bank_bills VALUES (?,?,?,?,?,?,?,?,?,?)", bills(Session.today()))
    con.commit()
    con.close()
    (tmp_path / "seed.json").write_text(json.dumps(SEED), encoding="utf-8")
    build(tmp_path, seed=tmp_path / "seed.json")
    client = httpx.Client(transport=httpx.MockTransport(_handler), follow_redirects=False)
    store = PageStore(tmp_path / "data" / "market_pages", client=client, resolver=Resolver())
    for url in PAGES:
        store.get(url)
    return tmp_path


class FakeForecaster:
    """forecaster.run_forecast with the real signature and result type. Base: likely $0 on 2027-06-25. A price rise
    of N cents moves it N/100 days later and adds N*3 cents a month; a removal moves it 40 days and adds $2,500."""

    BASE = date(2027, 6, 25)

    def __init__(self):
        self.calls = []

    def __call__(self, scenario=None, adjustments=None, settings=None, *, report_id=None, as_of=None, save=True):
        import contracts as K
        import forecaster as F

        self.calls.append(dict(scenario=scenario, adjustments=list(adjustments or [])))
        F.FORECASTS.mkdir(parents=True, exist_ok=True)
        n = 1 + sum(1 for _ in F.FORECASTS.glob("FC-2026-10-24-*.json"))
        fid = f"FC-2026-10-24-{n:04d}"
        effects, days = [], 0
        if scenario is not None:
            for item, new in scenario.price_changes_cents.items():
                old = dict((m[0], m[5]) for m in MENU)[item]
                days += (new - old) // 100
                effects.append(dict(kind="price_change", item=item, delta_cents=new - old,
                                    approx_net_per_month_cents=(new - old) * 3))
            for item in scenario.remove_items:
                days += 40
                effects.append(dict(kind="remove_item", item=item, approx_net_per_month_cents=250000))
            for c in scenario.one_off_costs:
                days -= c.cents // 100_000
        likely = self.BASE + timedelta(days=days)
        (F.FORECASTS / f"{fid}.json").write_text(json.dumps({"forecast_id": fid, "scenario_effects": effects,
                                                             "report_note": "fake"}))
        vs = None if scenario is None else K.VsBase(days_moved_likely=days, days_moved_average=float(days),
                                                    days_moved_worst=days, days_moved_best=days)
        return K.ForecastResult(
            forecast_id=fid, scenario=getattr(scenario, "name", None) or "base", as_of=date(2026, 10, 24),
            starting_cash_cents=1_350_000, run_out_worst=likely - timedelta(days=90), run_out_likely=likely,
            run_out_best=None, share_running_out=0.947, safety_floor_cents=5_045_735,
            below_floor_likely=date(2027, 2, 21), trials=1000, horizon_months=24, seed=13,
            assumptions=["fake forecast: sales follow last year"], vs_base=vs)


@pytest.fixture
def fake_forecast(monkeypatch):
    import orchestrator.env  # noqa: F401  (nessie_lab on sys.path)
    import forecaster

    fake = FakeForecaster()
    monkeypatch.setattr(forecaster, "run_forecast", fake)
    return fake


@pytest.fixture
def root(tmp_path, fake_forecast):
    return make_root(tmp_path)


@pytest.fixture
def session(root):
    from orchestrator.session import Session
    return Session(root)


@pytest.fixture
def no_network(monkeypatch):
    """Any socket connect fails the test."""
    hits = []

    def boom(*a, **kw):
        hits.append(a[:2])
        raise AssertionError(f"network used: {a[:2]}")

    monkeypatch.setattr(socket.socket, "connect", boom)
    monkeypatch.setattr(socket.socket, "connect_ex", boom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    return hits


@pytest.fixture
def no_llm_key(monkeypatch):
    for name in list(os.environ):
        if name.endswith("_API_KEY") or name in ("AGENTVERSE_TOKEN", "APIFY_TOKEN"):
            monkeypatch.delenv(name, raising=False)


@pytest.fixture
def roles(monkeypatch):
    """Every cost_price.db connection's role (cost_price.db._open is the one place every connection is made). The
    in-memory scratch database cost_price builds once to compare schemas (mode "memory", role "build") is not a
    connection to any file and is left out."""
    import cost_price.db as cdb

    seen, real = [], cdb._open

    def spy(target, mode, role):
        if mode != "memory":
            seen.append(role)
        return real(target, mode, role)

    monkeypatch.setattr(cdb, "_open", spy)
    return seen


@pytest.fixture
def tripwire(monkeypatch):
    """Fail the test if anything approves, applies live, unlocks the owner key or reads a passphrase."""
    import changes.cli
    import changes.executor as ex
    import cost_price.api as api
    import cost_price.owner as owner
    import cost_price.tty as tty

    hits = []

    def boom(name):
        def fail(*a, **kw):
            hits.append(name)
            raise AssertionError(f"{name} must never be called by the orchestrator")
        return fail

    real_apply = ex.Executor.apply

    def apply(self, s, live=False):
        if live:
            hits.append("Executor.apply(live=True)")
            raise AssertionError("Executor.apply(live=True) must never be called")
        return real_apply(self, s, live=False)

    monkeypatch.setattr(ex.Executor, "apply", apply)
    monkeypatch.setattr(ex.Executor, "revert", boom("Executor.revert"))
    monkeypatch.setattr(ex.Executor, "record_approval", boom("Executor.record_approval"))
    monkeypatch.setattr(api, "record_approval", boom("api.record_approval"))
    monkeypatch.setattr(owner, "unlock", boom("owner.unlock"))
    monkeypatch.setattr(changes.cli, "unlock", boom("changes.cli.unlock"))
    monkeypatch.setattr(changes.cli, "read_key", boom("changes.cli.read_key"))
    monkeypatch.setattr(changes.cli, "ask_owner", boom("changes.cli.ask_owner"))
    for name in ("read_passphrase", "is_console"):
        monkeypatch.setattr(tty, name, boom(f"tty.{name}"))
    return hits


def dump_hash(db: Path) -> str:
    import hashlib
    con = sqlite3.connect(Path(db).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()
    finally:
        con.close()
