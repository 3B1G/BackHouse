import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from cost_price.build import build
from cost_price.db import connect
from cost_price.owner import init_owner, unlock
from cost_price.verify import verify

from changes.shared import Approval, Suggestion

PASS = "correct horse battery"
APPROVED_AT = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)     # changes take effect 2026-10-25
NOW = datetime(2026, 10, 24, 20, 0, tzinfo=timezone.utc)             # the executor's clock in tests
BILLS = [("b-1", "pending", "Mission Dry Goods [SIM]", "MDG-CHS-1 due 2026-10-31", "2026-10-31"),
         ("b-2", "pending", "Mission Dry Goods [SIM]", "MDG-CHS-2 due 2026-11-07", "2026-11-07"),
         ("b-3", "recurring", "Mission Dry Goods [SIM]", "Mission Dry Goods autopay", "2026-11-09"),
         ("b-4", "pending", "Bay Produce Co [SIM]", "BPC-CHS-9 due 2026-11-02", "2026-11-02")]
SEED = {"retrieved_at": "2026-10-24T21:16:13+00:00", "restaurants": [
    {"name": "Alpha Trattoria", "menu_url": "https://alpha.example/menu", "price_level": "$$",
     "items": [{"our_item": "Margherita Pizza", "their_item": "Margherita", "price_cents": 2100, "service": "dinner",
                "quote": "Margherita $21", "match_confidence": 0.95}]}]}


class FakeNessie:
    def __init__(self):
        self.bills = {b: d for b, _, _, _, d in BILLS}
        self.status = {b: s for b, s, _, _, _ in BILLS}
        self.puts = []

    def get_bill(self, bill_id):
        return {"_id": bill_id, "payment_date": self.bills[bill_id], "status": self.status[bill_id]}

    def set_payment_date(self, bill_id, day):
        self.puts.append((bill_id, day))
        self.bills[bill_id] = day


@pytest.fixture
def root(tmp_path):
    """A small nessie_lab: books.db, a fake Nessie sync DB, cost_price.db built from them, and an owner."""
    (tmp_path / "data").mkdir()
    (tmp_path / "config.py").write_text("USAGE_VARIANCE = 1.08\n", encoding="utf-8")
    con = sqlite3.connect(tmp_path / "data" / "books.db")
    con.executescript("""
        CREATE TABLE locations (location, name, street_number, street_name, zip, neighborhood, cuisine, lat, lng, seats, closed_dow);
        INSERT INTO locations VALUES ('CHS','Demo Bistro','0','Chestnut St','94123','Marina','Italian',37.8008,-122.438,60,'[]');
        CREATE TABLE menu_prices (item, course, channel, service, effective_from, price_cents);
        INSERT INTO menu_prices VALUES ('Margherita Pizza','main','dine_in','dinner','2025-09-10',1750),
            ('Lobster Ravioli','main','dine_in','dinner','2026-04-01',2750),
            ('Tiramisu','dessert','dine_in','dinner','2025-09-10',1300);
        CREATE TABLE ingredients (ingredient, unit, supplier, pack_size);
        INSERT INTO ingredients VALUES ('flour','lb','Mission Dry Goods',50);
        CREATE TABLE invoices (invoice_no, supplier, category, location, invoice_date, due_date, terms_days, total);
        INSERT INTO invoices VALUES ('I1','x','x','CHS','2026-09-03','2026-10-03',30,0);
        CREATE TABLE invoice_lines (invoice_no, ingredient, unit, qty, unit_price, line_total);
        INSERT INTO invoice_lines VALUES ('I1','flour','lb',100,0.50,5000);
        CREATE TABLE recipes (item, ingredient, qty, unit);
        INSERT INTO recipes VALUES ('Margherita Pizza','flour',0.5,'lb');
    """)
    con.close()
    con = sqlite3.connect(tmp_path / "data" / "nessie_sync_boh-1loc.db")
    con.execute("CREATE TABLE bank_bills (nessie_id, status, payee, nickname, payment_date)")
    con.executemany("INSERT INTO bank_bills VALUES (?,?,?,?,?)", BILLS)
    con.execute("CREATE TABLE bank_tx (bank_id, amount, memo)")
    con.execute("INSERT INTO bank_tx VALUES ('T68E9A4CFB', 34900, 'TABLETAP POS HARDWARE PROTECTION')")
    con.commit()
    con.close()
    (tmp_path / "seed.json").write_text(json.dumps(SEED), encoding="utf-8")
    build(tmp_path, seed=tmp_path / "seed.json")
    with connect(tmp_path / "data" / "cost_price.db", "owner") as con:
        init_owner(con, PASS)
    return tmp_path


@pytest.fixture(autouse=True)
def injected_clock_allowed(monkeypatch):
    """Tests drive the executor with now=/today=; outside tests a live run refuses an injected clock."""
    import changes.executor
    monkeypatch.setattr(changes.executor, "ALLOW_INJECTED_CLOCK", True)


@pytest.fixture(autouse=True)
def database_still_verifies(request):
    """After every test that built a root: `cost-price verify` finds no problem, unless the test forges on purpose."""
    root = request.getfixturevalue("root") if "root" in request.fixturenames else None
    yield
    if root is not None and "tampered" not in request.keywords:
        assert verify(root / "data" / "cost_price.db", require_seal=False)["problems"] == []


@pytest.fixture
def db(root):
    return root / "data" / "cost_price.db"


@pytest.fixture
def key(db):
    with connect(db) as con:
        return unlock(con, PASS)


def dump_hash(db):
    con = sqlite3.connect(db)
    try:
        return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()
    finally:
        con.close()


def suggestion(**kw):
    """A Suggestion in the contracts v2 shape. Reprices default to $19.50; other actions carry no price."""
    base = dict(suggestion_id="S1", action="reprice_item", title="Raise Margherita Pizza from $17.50 to $19.50",
                target="Margherita Pizza", assumptions=["5% fewer orders"])
    if kw.get("action", "reprice_item") == "reprice_item":
        base["new_price_cents"] = 1950
        if "new_price_cents" in kw and kw["new_price_cents"]:           # a title that names only the new price
            base["title"] = f"Reprice {kw.get('target', base['target'])} to ${kw['new_price_cents'] / 100:.2f}"
    return Suggestion(**{**base, **kw})


def approval(sid="S1", decision="approved"):
    return Approval(suggestion_id=sid, decision=decision, decided_at=APPROVED_AT)
