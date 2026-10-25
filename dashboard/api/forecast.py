"""What-ifs and charts for the dashboard, on top of nessie_lab/forecaster.py.

Cash runs out on paydays (payroll is the biggest outflow), so the dashboard reads the forecast as paydays: for each payroll
day in the Forecaster's calendar, the share of its 1,000 futures still at or above $0 after payroll goes out, and the
lowest balance between that payday and the next. The charts add the same low points for the past year (from the bank),
how much cash grew or shrank each month, and where the money goes.
This reaches into the Forecaster's internals (prepare, _run, apply_scenario, compare). A public paydays field in
forecast_detail() would remove that coupling.
"""
import threading
import time
from datetime import date, timedelta

import numpy as np
import pandas as pd

import core  # noqa: F401  (puts nessie_lab on sys.path)
import contracts as K
import forecaster as F
import tracelog

LOCK = threading.RLock()        # the Forecaster caches per module, and the pipeline rewrites its inputs: one at a time
FRIDGE = K.OneOff(cents=480000, day=date(2026, 11, 15), label="walk-in fridge compressor")
WINDOW_MONTHS = 15              # the charts show the past year and the next 15 months
SPEND_GROUPS = {"Labor": "Staff", "Food Supply - Protein": "Food", "Food Supply - Dairy": "Food",
                "Food Supply - Dry Goods": "Food", "Food Supply - Produce": "Food", "Beverage": "Drinks",
                "Occupancy": "Rent", "Sales Tax": "Sales tax", "Direct Operating": "Supplies", "Owner Draw": "Your draw",
                "Utilities": "Utilities", "Insurance": "Insurance", "Debt Service": "Loan payments",
                "Marketing": "Marketing"}                       # any other category is "Other"
FOOD_PARTS = {"Food Supply - Protein": "protein", "Food Supply - Dairy": "dairy", "Food Supply - Dry Goods": "dry goods",
              "Food Supply - Produce": "produce"}
RISKS = [  # what could go wrong: (id, title, detail, contracts.Scenario fields)
    ("fridge", "The walk-in fridge breaks", "$4,800 repair on Nov 15", {"one_off_costs": [FRIDGE]}),
    ("slow_january", "January is slow", "15% fewer guests than usual", {"cover_change": {"2027-01": -0.15}}),
    ("road_work", "Road work out front", "20% fewer guests in March and April",
     {"cover_change": {"2027-03": -0.2, "2027-04": -0.2}}),
]
_RESULTS, _MENU, _HISTORY = {}, {}, {}
_ADJUSTMENTS = []      # the latest check's verified Bookkeeper fixes; every forecast and what-if uses them


def use_adjustments(adjustments):
    """Set by each check: the Bookkeeper adjustments the verifier passed (contracts.passed_adjustments)."""
    global _ADJUSTMENTS
    _ADJUSTMENTS = list(adjustments or [])


def _dollars(cents):
    return f"${cents / 100:,.0f}" if round(cents) % 100 == 0 else f"${cents / 100:,.2f}"


def _changes(prep):
    """What the owner could change, built from this restaurant's own numbers."""
    dishes, out = prep["dishes"], []
    mains = dishes[dishes.course == "main"]
    if len(mains):
        dish = (mains.avg_price - mains.plate_cost).idxmin()
        r = mains.loc[dish]
        out.append(("drop_dish", f"Take {dish} off the menu",
                    f"It makes {_dollars(r.avg_price - r.plate_cost)} a plate after ingredients", {"remove_items": [dish]}))
    lenient = sorted(s for s, days in prep["supplier_tolerance"].items() if days is not None and days >= 10)
    if lenient:
        out.append(("pay_later", f"Pay {len(lenient)} suppliers 10 days later",
                    "Each one usually allows it. You still owe the same", {"supplier_stretch_days": {s: 10 for s in lenient}}))
    pizza = "Margherita Pizza"
    if pizza in dishes.index:
        old = F._dinner_price(pizza, prep["h"]["as_of"])
        month = pd.Period(prep["dishes_month"], "M").strftime("%B")
        out.append(("raise_price", f"Raise {pizza} by $2",
                    f"From {_dollars(old)} to {_dollars(old + 200)}. Ordered {int(dishes.loc[pizza].units)} times in {month}",
                    {"price_changes_cents": {pizza: old + 200}}))
    return out


def scenarios():
    """The what-if menu. Each item says how far it moves the day cash runs out on its own (average over the futures)."""
    with LOCK:
        stamp = (F._stamp(), tuple(a.model_dump_json() for a in _ADJUSTMENTS))
        if stamp not in _MENU:
            prep = F.prepare(None, "none", dict(F.DEFAULTS), None)
            items = []
            for group, (sid, title, detail, kw) in [("risk", r) for r in RISKS] + [("change", c) for c in _changes(prep)]:
                vs = simulate(kw, group="what-if menu")["vs_base"] or {}
                items.append(dict(id=sid, group=group, title=title, detail=detail, available=True,
                                  effect_days=vs.get("avg_days"), kw=kw))
            items.append(dict(id="fixes", group="change", title="Approve the Bookkeeper's fixes",
                              detail="Once the Bookkeeper finds something to fix", available=False, effect_days=None, kw=None))
            _MENU.clear()
            _MENU[stamp] = items
        return _MENU[stamp]


def combine(kws):
    """Several what-ifs as one set of contracts.Scenario fields: lists add up, guest changes add up month by month."""
    kw = {}
    for one in kws:
        for k, v in one.items():
            if isinstance(v, list):
                kw[k] = kw.get(k, []) + list(v)
            elif k == "cover_change":
                merged = dict(kw.get(k, {}))
                for month, change in v.items():
                    merged[month] = max(-1.0, min(1.0, merged.get(month, 0) + change))
                kw[k] = merged
            else:
                kw[k] = {**kw.get(k, {}), **v}
    return kw


def run(ids=(), adjustments=None, fixable=(), refunds=(), group=None):
    """The forecast with the chosen what-ifs (ids from scenarios()) switched on. `adjustments` (contracts.Adjustment,
    already passed by checks.py) change the history; `fixable` are finding ids whose overcharges the fixes stop;
    `refunds` are OneOffs the fixes bring in."""
    ids = list(dict.fromkeys(ids))
    menu = {s["id"]: s for s in scenarios()} if ids else {}
    unknown = [i for i in ids if i not in menu]
    if unknown:
        raise ValueError(f"There's no what-if called {', '.join(unknown)}.")
    kw = combine(menu[i]["kw"] for i in ids if menu[i]["kw"])
    if "fixes" in ids and (fixable or refunds):
        kw["fix_findings"] = list(fixable)
        kw["one_off_income"] = kw.get("one_off_income", []) + list(refunds)
    return {**simulate(kw, adjustments, group), "what_if": [i for i in ids if menu[i]["available"]]}


def _paydays(prep, paths):
    """Each payday: the share of futures where payroll clears (cash never fell below $0 up to that day), and the lowest
    balance from that payday to the day before the next one (10th, 50th and 90th percentile of the futures)."""
    first = F._first_below(paths, 0)
    dates = prep["cal"]["dates"]
    js = [j for j, *_ in prep["cal"]["payroll"]]
    out = []
    for k, j in enumerate(js):
        end = js[k + 1] if k + 1 < len(js) else paths.shape[1]
        p10, p50, p90 = np.percentile(paths[:, j:end].min(axis=1), [10, 50, 90])
        out.append({"date": str(dates[j]), "clears": round(float(np.mean(~(first <= j))), 3),
                    "p10": int(p10), "p50": int(p50), "p90": int(p90)})
    return out


def _months_ahead(prep, paths):
    """How much cash grows or shrinks in each full month ahead: each future's month-end balance minus the month before."""
    dates = prep["cal"]["dates"]
    last = {}
    for j, d in enumerate(dates):
        last[f"{d:%Y-%m}"] = j
    months, out = list(last), []
    for prev, month in zip(months, months[1:]):
        end = dates[last[month]]
        if (end + timedelta(1)).month == end.month:     # the horizon stops mid-month
            continue
        p10, p50, p90 = np.percentile(paths[:, last[month]] - paths[:, last[prev]], [10, 50, 90])
        out.append({"month": month, "p10": int(p10), "p50": int(p50), "p90": int(p90)})
    return out


def _history(prep):
    """The past year from the bank, leaving out transfers between the restaurant's own accounts. Same for every what-if."""
    stamp = F._stamp()
    if stamp in _HISTORY:
        return _HISTORY[stamp]
    h = prep["h"]
    as_of, bank = h["as_of"], h["bank"][h["bank"].category != "Internal"]
    flows = bank.groupby(pd.to_datetime(bank.date)).signed.sum()
    balance = int(h["books"]["accounts"].opening_balance_cents.sum()) + flows.reindex(
        pd.date_range(flows.index.min(), as_of), fill_value=0).cumsum()

    pays = sorted(pd.Timestamp(p) for p in set(h["books"]["payroll_runs"].pay_date) if p <= as_of)
    paydays = [{"date": str(p.date()), "low_cents": int(balance[p:(pays[k + 1] - timedelta(1)) if k + 1 < len(pays) else None].min())}
               for k, p in enumerate(pays)]

    month = pd.to_datetime(bank.date).dt.strftime("%Y-%m")
    first = pd.Period(bank.date.min(), "M") + (0 if bank.date.min().day == 1 else 1)
    months = []
    for m in pd.period_range(first, pd.Period(as_of, "M"), freq="M"):
        rows = bank[month == str(m)]
        row = {"month": str(m), "in_cents": int(rows.signed[rows.signed > 0].sum()),
               "out_cents": int(-rows.signed[rows.signed < 0].sum()), "net_cents": int(rows.signed.sum())}
        if m == pd.Period(as_of, "M") and as_of < m.end_time.date():
            row["through"] = str(as_of)
        months.append(row)

    recent = [pd.Period(as_of, "M") - k for k in (3, 2, 1)]
    rows = bank[month.isin([str(m) for m in recent])]
    spent = -rows[rows.signed < 0].groupby("category").signed.sum() / len(recent)
    groups = {}
    for category, cents in spent.items():
        g = groups.setdefault(SPEND_GROUPS.get(category, "Other"), {"label": SPEND_GROUPS.get(category, "Other"), "cents": 0, "parts": []})
        g["cents"] += cents
        if category in FOOD_PARTS:
            g["parts"].append({"label": FOOD_PARTS[category], "cents": int(round(cents))})
    spend = sorted(groups.values(), key=lambda g: (g["label"] == "Other", -g["cents"]))
    for g in spend:
        g["cents"] = int(round(g["cents"]))
        g["parts"].sort(key=lambda p: -p["cents"])

    _HISTORY.clear()
    _HISTORY[stamp] = {
        "paydays": paydays,
        "months": months,
        "spending": {"from": str(recent[0]), "to": str(recent[-1]), "rows": spend,
                     "in_cents": int(round(rows.signed[rows.signed > 0].sum() / len(recent))),
                     "out_cents": int(round(spent.sum()))},
    }
    return _HISTORY[stamp]


def simulate(kw, adjustments=None, group=None):
    """The base forecast, or any what-if given as contracts.Scenario fields (one_off_costs, cover_change, ...).
    `group` labels the call on the developer page (a check's run id, chat-<id>, dashboard). Without `adjustments`, it uses
    the latest check's verified Bookkeeper fixes, so the base forecast and every what-if agree."""
    adjustments = _ADJUSTMENTS if adjustments is None else adjustments
    adj_key = tuple(a.model_dump_json() for a in adjustments or [])
    scenario_key = K.Scenario(name="what if", **kw).model_dump_json() if kw else ""
    key = (scenario_key, adj_key, F._stamp())
    label = "1,000 futures, " + (", ".join(sorted(kw)) if kw else "no what-ifs")
    with LOCK:
        if key in _RESULTS:
            tracelog.Span("Forecaster", label, group).done("Answered from cache")
            return _RESULTS[key]
        span = tracelog.Span("Forecaster", label, group, scenario=kw or None)
        t0 = time.perf_counter()
        S = dict(F.DEFAULTS)
        prep = F.prepare(None, "none", S, adjustments or None)
        base_paths, base = F._run(prep, S)
        paths, summ, vs = base_paths, base, None
        if kw:
            sc = F.scenario_from_contract(K.Scenario(name="what if", **kw), prep)
            adj, _effects, _notes = F.apply_scenario(prep, sc)
            paths, summ = F._run(prep, S, adj)
            vs = F.compare(base, summ, base_paths, paths, prep["cal"]["dates"])
        as_of = prep["h"]["as_of"]
        out = {
            "as_of": str(as_of),
            "cash_today_cents": int(prep["h"]["cash_today"]),
            "floor_cents": summ["below_floor"]["floor_cents"],
            "runout": {"bad": summ["runout"]["p10"], "likely": summ["runout"]["p50"], "good": summ["runout"]["p90"]},
            "share_running_out": round(1 - summ["runout"]["never_within_horizon_pct"], 3),
            "below_floor_likely": summ["below_floor"]["p50"],
            "paydays": _paydays(prep, paths),
            "base_paydays": _paydays(prep, base_paths) if kw else None,
            "base_likely": base["runout"]["p50"],
            "months": _months_ahead(prep, paths),
            "base_months": _months_ahead(prep, base_paths) if kw else None,
            "history": _history(prep),
            "window_end": str((pd.Timestamp(as_of) + pd.DateOffset(months=WINDOW_MONTHS)).date()),
            "vs_base": None if not vs else {"avg_days": vs.get("runout_mean_days_change"),
                                             "likely_days": vs["runout_days"]["p50"],
                                             "floor_days": vs["below_floor_days"]["p50"]},
            "assumptions": F._assumptions(prep, S),
            "runtime_ms": round((time.perf_counter() - t0) * 1000),
        }
        _RESULTS[key] = out
        span.done(f"Likely out of cash {out['runout']['likely']}", runtime_ms=out["runtime_ms"])
        return out
