"""`cost-price build`: create data/cost_price.db if needed, rebuild the reference layer from books.db, and load the
committed seed list of competitors and their prices. History rows are never touched; loading the seed again adds
nothing. Run after generate.py and build_book.py. Same inputs -> same reference layer and seed rows, byte for byte."""
import importlib.util
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

from . import api
from .db import connect, create, transaction

PACKAGE = Path(__file__).resolve().parents[2]                  # nessie_lab/cost_price/
SEED = PACKAGE / "seed" / "competitors.json"
REFERENCE_ORDER = ["plate_cost_month", "recipe_line", "ingredient_cost_month", "ingredient", "menu_price_base",
                   "restaurant"]                               # children first (foreign keys)


def usage_variance(root: Path) -> float:
    """config.USAGE_VARIANCE, the same factor features.py applies to plate cost."""
    spec = importlib.util.spec_from_file_location("backhouse_config", Path(root) / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return float(module.USAGE_VARIANCE)


def read_books(books: Path) -> dict:
    con = sqlite3.connect(Path(books).resolve().as_uri() + "?mode=ro", uri=True)
    try:
        q = lambda sql: con.execute(sql).fetchall()
        return dict(
            locations=q("SELECT location, name, zip, neighborhood, cuisine, lat, lng FROM locations ORDER BY location"),
            menu=q("""SELECT item, course, channel, service, effective_from, price_cents FROM menu_prices
                      ORDER BY item, channel, service, effective_from"""),
            ingredients=q("SELECT ingredient, unit, supplier, pack_size FROM ingredients ORDER BY ingredient"),
            purchases=q("""SELECT l.ingredient, substr(i.invoice_date, 1, 7), l.qty, l.unit_price FROM invoice_lines l
                           JOIN invoices i USING (invoice_no) ORDER BY l.ingredient, i.invoice_date, l.invoice_no"""),
            recipes=q("SELECT item, ingredient, qty, unit FROM recipes ORDER BY item, ingredient"),
        )
    finally:
        con.close()


def monthly_costs(purchases):
    """{(ingredient, month): (qty-weighted mean $/unit, qty)}: the same average features.ingredient_month takes."""
    acc = defaultdict(lambda: [0.0, 0])
    for ing, month, qty, unit_price in purchases:
        acc[(ing, month)][0] += qty * unit_price
        acc[(ing, month)][1] += qty
    return {k: (v[0] / v[1], v[1]) for k, v in acc.items() if v[1] > 0}


def plate_costs(costs, recipes, variance):
    """[(item, month, cents)] with each ingredient's price carried forward to months it wasn't bought, exactly like
    features.item_month (pivot + ffill, then round(sum(qty * price) * variance * 100))."""
    months = sorted({m for _, m in costs})
    by_item = defaultdict(list)
    for item, ing, qty, _ in recipes:
        by_item[item].append((ing, qty))
    last, out = {}, []
    for m in months:
        for (ing, month), (price, _) in costs.items():
            if month == m:
                last[ing] = price
        for item in sorted(by_item):
            lines = by_item[item]
            if all(ing in last for ing, _ in lines):
                out.append((item, m, round(sum(q * last[ing] for ing, q in lines) * variance * 100)))
    return out


def seed_ts(value: str) -> str:
    """'2026-10-24T21:16:13+00:00' -> '2026-10-24T21:16:13Z' (the schema's timestamp form)."""
    return value.replace("+00:00", "Z") if value.endswith("+00:00") else value


def build(root: Path, seed: Path = SEED) -> dict:
    root = Path(root)
    db = root / "data" / "cost_price.db"
    if not db.exists():
        create(db)
    books = read_books(root / "data" / "books.db")
    costs = monthly_costs(books["purchases"])
    variance = usage_variance(root)
    plates = plate_costs(costs, books["recipes"], variance)
    data = json.loads(Path(seed).read_text(encoding="utf-8"))
    retrieved = seed_ts(data["retrieved_at"])
    n_obs = 0
    with connect(db, "build") as con, transaction(con):
        for t in REFERENCE_ORDER:
            con.execute(f"DELETE FROM {t}")
        con.executemany("INSERT INTO restaurant VALUES (?,?,?,?,?,?,?)", books["locations"])
        con.executemany("INSERT INTO menu_price_base VALUES (?,?,?,?,?,?)", books["menu"])
        con.executemany("INSERT INTO ingredient VALUES (?,?,?,?)", books["ingredients"])
        con.executemany("INSERT INTO ingredient_cost_month VALUES (?,?,?,?)",
                        [(ing, m, round(price * 100_000), qty) for (ing, m), (price, qty) in sorted(costs.items())])
        con.executemany("INSERT INTO recipe_line VALUES (?,?,?,?)",
                        [(item, ing, round(qty * 1000), unit) for item, ing, qty, unit in books["recipes"]])
        con.executemany("INSERT INTO plate_cost_month VALUES (?,?,?,?)",
                        [(item, m, cents, variance) for item, m, cents in plates])
        for r in data["restaurants"]:
            cid = api.add_competitor(con, name=r["name"], menu_url=r["menu_url"], found_via="seed",
                                     address=r.get("address"), neighborhood=r.get("neighborhood"),
                                     cuisine=r.get("cuisine"), price_level=r.get("price_level"),
                                     distance_miles=r.get("approx_distance_miles"), notes=r.get("notes"),
                                     created_at=retrieved)
            for x in r["items"]:
                api.add_observation(con, competitor_id=cid, our_item=x["our_item"], their_item=x["their_item"],
                                    price_cents=int(x["price_cents"]), service=x.get("service", "unknown"),
                                    source_url=x.get("source_url") or r["menu_url"], source_type="restaurant_site",
                                    quote=x["quote"], match_confidence=float(x.get("match_confidence", 0.8)),
                                    reader="seed", retrieved_at=retrieved, created_at=retrieved)
                n_obs += 1
    return dict(db=str(db), menu_rows=len(books["menu"]), ingredients=len(books["ingredients"]),
                ingredient_months=len(costs), plate_costs=len(plates), competitors=len(data["restaurants"]),
                seed_observations=n_obs)


def compare_with_features(root: Path) -> list[str]:
    """Plate costs that differ from features.db's feat_item_month by more than 1 cent ([] = all agree)."""
    feats = Path(root) / "data" / "features.db"
    if not feats.exists():
        return ["features.db not found: run features.py to cross-check plate costs"]
    f = sqlite3.connect(feats.resolve().as_uri() + "?mode=ro", uri=True)
    want = {(i, m): int(c) for i, m, c in f.execute("SELECT item, month, plate_cost FROM feat_item_month")}
    f.close()
    with connect(Path(root) / "data" / "cost_price.db") as con:
        have = {(r[0], r[1]): r[2] for r in con.execute("SELECT item, month, plate_cost_cents FROM plate_cost_month")}
    return [f"{k}: features {v}, cost_price {have.get(k)}" for k, v in sorted(want.items())
            if have.get(k) is None or abs(have[k] - v) > 1]
