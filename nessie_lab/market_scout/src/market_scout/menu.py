"""Our side of the comparison: the restaurant and its dine-in prices on a date, read-only from cost_price.db. Prices
come from the database's timeline, so a price change CH applied after the owner approved it is picked up."""
from datetime import date
from pathlib import Path

from cost_price import api
from cost_price.db import connect


def our_restaurant(db: Path) -> dict:
    with connect(db, "reader") as con:
        return api.restaurant(con)


def our_menu(db: Path, service="dinner", items=(), on: str | date | None = None) -> tuple[dict[str, int], str]:
    """{item: price_cents} in force on `on` (default today), and the latest effective_from among them. Unknown
    requested items raise ValueError."""
    on = (on or date.today()).isoformat() if not isinstance(on, str) else on
    with connect(db, "reader") as con:
        menu, latest = api.menu(con, on, service=service, items=list(items))
    return menu, latest or on
