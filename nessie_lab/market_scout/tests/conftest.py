"""A tmp nessie_lab root: a small books.db, built into cost_price.db with a made-up seed list, plus fake menu pages
and an offline DNS resolver. Never touches the real data/ directory."""
import hashlib
import json
import sqlite3
from datetime import datetime, timezone

import httpx
import pytest

from cost_price.build import build
from market_scout.pages import PageStore, Snapshot, page_text

HERE = dict(location="CHS", name="Demo Bistro Chestnut", street_number="0", street_name="Chestnut St (SIMULATED)",
            zip="94123", neighborhood="Marina", cuisine="Italian trattoria", lat=37.8008, lng=-122.438, seats=60,
            closed_dow="[]")
MENU = [("Margherita Pizza", "main", "dine_in", "dinner", "2025-09-10", 1750),
        ("Lobster Ravioli", "main", "dine_in", "dinner", "2025-09-10", 2700),
        ("Lobster Ravioli", "main", "dine_in", "dinner", "2026-04-01", 2750),
        ("Lobster Ravioli", "main", "dine_in", "lunch", "2026-04-01", 2200),
        ("Tiramisu", "dessert", "dine_in", "dinner", "2025-09-10", 1300),
        ("Add Grilled Chicken", "modifier", "dine_in", "dinner", "2025-09-10", 600)]

# made-up competitors and pages near us, plus one that must be dropped
PAGES = {
    "https://a.example/menu": "<h2>Pizze</h2><p>Margherita</p><p>tomato, mozzarella, basil</p><p>$21</p>"
                              "<p>Lobster Ravioli brown butter 32</p><script>var p = 99;</script>",
    "https://b.example/menu": "<li>Pizza Margherita 19</li><li>Tiramisu 12</li><li>Lobster Ravioli 29</li>",
    "https://c.example/menu": "<div>MARGHERITA &mdash; 23</div><div>Tiramisu &ndash; 11</div><div>Lobster ravioli 30</div>",
    "https://d.example/menu": "<div>Margherita 45</div>",
}
SEED = {"retrieved_at": "2026-10-24T21:16:13+00:00", "restaurants": [
    dict(name="Alpha Trattoria", cuisine="Italian", price_level="$$", approx_distance_miles=0.4,
         menu_url="https://a.example/menu",
         items=[dict(our_item="Margherita Pizza", their_item="Margherita", price_cents=2100,
                     quote="Margherita tomato, mozzarella, basil $21")]),
    dict(name="Beta Pizzeria", cuisine="Pizza", price_level="$$", approx_distance_miles=0.9,
         menu_url="https://b.example/menu", items=[]),
    dict(name="Gamma Osteria", cuisine="Italian", price_level="$$", approx_distance_miles=1.6,
         menu_url="https://c.example/menu",
         items=[dict(our_item="Tiramisu", their_item="Tiramisu", price_cents=1500, quote="Tiramisu 15")]),  # wrong on purpose
    dict(name="Delta Fine Dining", cuisine="Italian", price_level="$$$$", approx_distance_miles=1.0,
         menu_url="https://d.example/menu", items=[]),
]}
PUBLIC = "93.184.216.34"


def sha(html: str) -> str:
    return hashlib.sha256(html.encode("utf-8")).hexdigest()


class FakePages:
    def __init__(self, pages=PAGES):
        self.pages, self.calls, self.offline_calls = pages, [], []

    def get(self, url, refresh=False, offline=False):
        self.calls.append(url)
        self.offline_calls.append(offline)
        if url not in self.pages:
            raise ValueError("blocked")
        html = self.pages[url]
        return Snapshot(url, None, page_text(html), datetime(2026, 10, 24, tzinfo=timezone.utc), sha(html))


class Resolver:
    """Offline DNS: host -> addresses; anything unknown resolves to one public address."""

    def __init__(self, table=None):
        self.table, self.calls = table or {}, []

    def __call__(self, host, port):
        self.calls.append(host)
        return self.table.get(host, [PUBLIC])


def make_root(tmp_path, seed=SEED, menu=MENU):
    (tmp_path / "data").mkdir()
    (tmp_path / "config.py").write_text("USAGE_VARIANCE = 1.08\n", encoding="utf-8")
    con = sqlite3.connect(tmp_path / "data" / "books.db")
    con.execute(f"CREATE TABLE locations ({', '.join(HERE)})")
    con.execute(f"INSERT INTO locations VALUES ({', '.join('?' * len(HERE))})", list(HERE.values()))
    con.execute("CREATE TABLE menu_prices (item, course, channel, service, effective_from, price_cents)")
    con.executemany("INSERT INTO menu_prices VALUES (?,?,?,?,?,?)", menu)
    con.executescript("""
        CREATE TABLE ingredients (ingredient, unit, supplier, pack_size);
        INSERT INTO ingredients VALUES ('flour','lb','Mission Dry Goods',50);
        CREATE TABLE invoices (invoice_no, supplier, category, location, invoice_date, due_date, terms_days, total);
        INSERT INTO invoices VALUES ('I1','x','x','CHS','2026-09-03','2026-10-03',30,0);
        CREATE TABLE invoice_lines (invoice_no, ingredient, unit, qty, unit_price, line_total);
        INSERT INTO invoice_lines VALUES ('I1','flour','lb',100,0.50,5000);
        CREATE TABLE recipes (item, ingredient, qty, unit);
        INSERT INTO recipes VALUES ('Margherita Pizza','flour',0.5,'lb');
    """)
    con.commit()
    con.close()
    (tmp_path / "seed.json").write_text(json.dumps(seed), encoding="utf-8")
    build(tmp_path, seed=tmp_path / "seed.json")
    return tmp_path


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path)


@pytest.fixture
def db(root):
    return root / "data" / "cost_price.db"


@pytest.fixture
def resolver():
    return Resolver()


def store(tmp_path, handler, resolver=None, clock=None):
    """A real PageStore whose network is an httpx.MockTransport and whose DNS is the fake resolver."""
    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    kwargs = dict(clock=clock) if clock else {}
    return PageStore(tmp_path / "pages", client=client, resolver=resolver or Resolver(), **kwargs)
