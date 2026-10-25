import json
import sqlite3

import pytest

from cost_price.build import build

SEED = {"retrieved_at": "2026-10-24T21:16:13+00:00", "restaurants": [
    {"name": "Alpha Trattoria", "menu_url": "https://www.alpha.example/menu", "price_level": "$$",
     "approx_distance_miles": 0.4, "cuisine": "Italian",
     "items": [{"our_item": "Margherita Pizza", "their_item": "Margherita", "price_cents": 2100, "service": "all_day",
                "quote": "Margherita $21", "match_confidence": 0.95},
               {"our_item": "Tiramisu", "their_item": "Tiramisu", "price_cents": 1200, "service": "unknown",
                "quote": "Tiramisu 12", "match_confidence": 0.9, "source_url": "https://www.alpha.example/dessert"}]},
    {"name": "Beta Pizzeria", "menu_url": "https://beta.example/menu", "price_level": "$$",
     "approx_distance_miles": 0.9, "cuisine": "Pizza",
     "items": [{"our_item": "Margherita Pizza", "their_item": "Pizza Margherita", "price_cents": 1900,
                "service": "dinner", "quote": "Pizza Margherita 19", "match_confidence": 0.9}]},
]}


@pytest.fixture
def root(tmp_path):
    (tmp_path / "data").mkdir()
    (tmp_path / "config.py").write_text("USAGE_VARIANCE = 1.08\n", encoding="utf-8")
    con = sqlite3.connect(tmp_path / "data" / "books.db")
    con.executescript("""
        CREATE TABLE locations (location, name, street_number, street_name, zip, neighborhood, cuisine, lat, lng, seats, closed_dow);
        INSERT INTO locations VALUES ('CHS','Demo Bistro','0','Chestnut St','94123','Marina','Italian trattoria',37.8008,-122.438,60,'[]');
        CREATE TABLE menu_prices (item, course, channel, service, effective_from, price_cents);
        INSERT INTO menu_prices VALUES ('Margherita Pizza','main','dine_in','dinner','2025-09-10',1750),
            ('Margherita Pizza','main','dine_in','lunch','2025-09-10',1500),
            ('Margherita Pizza','main','delivery','dinner','2025-09-10',2000),
            ('Tiramisu','dessert','dine_in','dinner','2025-09-10',1300),
            ('Tiramisu','dessert','dine_in','dinner','2026-04-01',1350),
            ('Add Grilled Chicken','modifier','dine_in','dinner','2025-09-10',600);
        CREATE TABLE ingredients (ingredient, unit, supplier, pack_size);
        INSERT INTO ingredients VALUES ('flour','lb','Mission Dry Goods',50), ('tomato','lb','Bay Produce Co',25),
            ('mascarpone','lb','Sonoma Dairy Supply',5);
        CREATE TABLE invoices (invoice_no, supplier, category, location, invoice_date, due_date, terms_days, total);
        INSERT INTO invoices VALUES ('I1','x','x','CHS','2026-09-03','2026-10-03',30,0),
            ('I2','x','x','CHS','2026-09-20','2026-10-20',30,0), ('I3','x','x','CHS','2026-10-02','2026-11-01',30,0);
        CREATE TABLE invoice_lines (invoice_no, ingredient, unit, qty, unit_price, line_total);
        INSERT INTO invoice_lines VALUES ('I1','flour','lb',100,0.50,5000), ('I2','flour','lb',100,0.70,7000),
            ('I1','tomato','lb',50,2.00,10000), ('I1','mascarpone','lb',10,6.00,6000), ('I3','tomato','lb',50,3.00,15000);
        CREATE TABLE recipes (item, ingredient, qty, unit);
        INSERT INTO recipes VALUES ('Margherita Pizza','flour',0.5,'lb'), ('Margherita Pizza','tomato',0.25,'lb'),
            ('Tiramisu','mascarpone',0.2,'lb');
    """)
    con.close()
    (tmp_path / "seed.json").write_text(json.dumps(SEED), encoding="utf-8")
    build(tmp_path, seed=tmp_path / "seed.json")
    return tmp_path


@pytest.fixture
def db(root):
    return root / "data" / "cost_price.db"
