"""The one money formatter, plus the pass that gives every tool result its display strings.

Tool results carry money as integer cents under keys ending in `_cents`; `displayed()` adds a sibling `<base>_usd`
string made by `usd()`. The brief prints money only from those strings, so every dollar figure in it is, character
for character, a display string some tool result already holds. The shares in SHARE_KEYS (fractions, 0.14 = 14%) get
a `<key>_display` the same way; other `*_pct` keys (the Bookkeeper's rate_pct = 2.6 means 2.6%) are left alone."""
from datetime import date, datetime

SHARE_KEYS = {"gap_pct", "food_cost_pct", "running_out_pct"}


def usd(cents: int) -> str:
    """-1234567 -> '-$12,345.67'."""
    cents = int(cents)
    return ("-" if cents < 0 else "") + f"${abs(cents) / 100:,.2f}"


def pct(share: float) -> str:
    """0.1432 -> '14%', -0.0612 -> '-6%'."""
    return f"{round(float(share) * 100):d}%"


def jsonable(obj):
    """Dates to YYYY-MM-DD, tuples to lists, pydantic models to their JSON dump; everything else as is."""
    if hasattr(obj, "model_dump"):
        return jsonable(obj.model_dump(mode="json"))
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (date, datetime)):
        return obj.isoformat()
    if hasattr(obj, "item") and callable(obj.item):           # numpy scalars
        return obj.item()
    return obj


def displayed(obj):
    """A JSON-able copy with `<base>_usd` next to every integer `<base>_cents` and `<key>_display` next to every
    share in SHARE_KEYS."""
    obj = jsonable(obj)
    if isinstance(obj, list):
        return [displayed(v) for v in obj]
    if not isinstance(obj, dict):
        return obj
    out = {}
    for k, v in obj.items():
        out[k] = displayed(v)
        if k.endswith("_cents") and isinstance(v, int) and not isinstance(v, bool):
            out[k[: -len("_cents")] + "_usd"] = usd(v)
        elif k in SHARE_KEYS and isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k + "_display"] = pct(v)
    return out
