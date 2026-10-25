"""Forecaster: Monte Carlo cash runway. When does the bank balance hit $0, and how sure are we?

  python forecaster.py                          # base forecast as of the last bank day, latest verified report
  python forecaster.py --as-of 2026-07-24       # forecast from an earlier day (uses only data up to then)
  python forecaster.py --report none            # ignore Bookkeeper reports
  python forecaster.py --deterministic          # no randomness: one expected path (sanity check vs scenario_check.py)
  python forecaster.py --scenario '{"name": "fridge + slow January", "one_off_costs": [{"cents": 480000, "day": "2026-11-15",
                                   "label": "walk-in fridge"}], "cover_change": {"2027-01": -0.15}}'
                                                # contracts.Scenario, or the richer dict apply_scenario() reads
  python forecaster.py --backtest               # forecast from config.TRAIN_END and score it on what happened

Interface (contracts.py): run_forecast(Scenario, list[Adjustment] | None, ForecastSettings | None) -> ForecastResult.
forecast_detail() returns everything (vs_base, scenario_effects, assumptions, monthly_bands) and backs the CLI.

Pipeline: prepare() = load -> apply the Bookkeeper report -> estimate patterns -> fixed calendar (cached per data snapshot),
then simulate -> summarize -> save. A scenario (see apply_scenario) is simulated with the same random draws as the base, and
`vs_base` says how many days it moves each run-out date. Charts are saved next to the JSON.

Inputs (all read-only, all cut at `as_of`):
  bank    Nessie copy via bookkeeper_tools.load_bank_frame() (data/nessie_sync_<tag>.db); falls back to bank_feed.csv,
          and says so in the output, when the sync hasn't been run
  books   data/books.db (POS via features.db, invoices, vendor bills, payroll, leases, loan, tax, draws, repairs, permits)
  report  data/reports/<report_id>.json written by checks.py; only reports with verification.passed are used
Output: data/forecasts/<forecast_id>.json (returned as a dict too). Amounts are integer cents, dates YYYY-MM-DD.

Statistics, not a trained model: sales follow last year's same month (dow-adjusted) times this weekday's effect, with a
year-over-year trend estimated from the months seen twice; daily noise is AR(1) on log sales; the trend is redrawn per trial
from its standard error. Money arrives the way the bank showed it (card/cash next business day, delivery weekly) at the ratios
the bank actually paid over the last 8 weeks, so live fee or commission problems are already in those ratios.
Never reads data/grader/ and never uses Nessie's |ref= tags.
"""
import argparse, json, os, sqlite3, time
from contextlib import closing
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config as C
import contracts as K
import features as F

D = Path(__file__).parent / "data"
REPORTS, FORECASTS = D / "reports", D / "forecasts"
BOOKS, FEATURES = D / "books.db", D / "features.db"
TAG = os.environ.get("BOH_NESSIE_TAG", "boh-1loc")
DEFAULTS = dict(trials=1000, horizon_months=24, horizon_days=None, seed=C.SEED, deposit_window_days=56, food_window_days=91,
                deterministic=False)          # horizon_days, when set, overrides horizon_months
BANK_HOLIDAYS = {date(2025, 11, 11), date(2025, 11, 27), date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 19),
                 date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19), date(2026, 9, 7), date(2026, 10, 12)}  # as generate.py
DAYS_PER_MONTH = 30.44


# ---------------------------------------------------------------- calendar helpers
def _bday(d):
    return d.weekday() < 5 and d not in BANK_HOLIDAYS


def next_bday(d):
    while not _bday(d):
        d += timedelta(1)
    return d


def prev_bday(d):
    while not _bday(d):
        d -= timedelta(1)
    return d


def _months(start, end):
    """(year, month) for every month from start's through end's."""
    return [(p.year, p.month) for p in pd.period_range(start, end, freq="M")]


def _dom(y, m, day):
    return date(y, m, min(day, pd.Period(f"{y}-{m:02d}").days_in_month))


# ---------------------------------------------------------------- 1. load
def _books(names):
    with closing(sqlite3.connect(BOOKS.as_uri() + "?mode=ro", uri=True)) as con:
        return {n: pd.read_sql(f"select * from {n}", con) for n in names}


def _bank():
    try:
        import bookkeeper_tools as bt
        return bt.load_bank_frame(), f"Nessie ({bt.BANK_DB.name})"
    except (FileNotFoundError, RuntimeError):
        return (pd.read_csv(D / "bank_feed.csv"),
                f"bank_feed.csv: Nessie copy not found, run python nessie_sync.py --tag {TAG}")


def load_history(as_of=None, report_id=None):
    """Everything the Forecaster needs, built from data dated on or before `as_of` (default: the last bank day)."""
    names = ["accounts", "suppliers", "vendor_bills", "leases", "repair_invoices", "misc_payables", "invoices",
             "ap_payment_plans", "payroll_runs", "loans", "loan_schedule", "sales_tax_returns", "owner_draws"]
    t = _books(names)
    bank, bank_source = _bank()
    bank["date"] = pd.to_datetime(bank.date).dt.date
    with closing(sqlite3.connect(FEATURES.as_uri() + "?mode=ro", uri=True)) as con:
        sales = pd.read_sql("select location, date, open, revenue, delivery_rev, has_event from feat_daily_sales", con)
    sales["date"] = pd.to_datetime(sales.date).dt.date
    # default: the day the books close (the bank may have no activity that day, e.g. a Saturday)
    as_of = date.fromisoformat(str(as_of)) if as_of else max(bank.date.max(), sales.date.max())
    bank = bank[bank.date <= as_of].copy()
    sales = sales[sales.date <= as_of]
    for name, df in t.items():
        for c in df.columns:
            if c in ("date", "due", "anniversary", "period_start", "period_end") or c.endswith("_date"):
                df[c] = pd.to_datetime(df[c]).dt.date
    cut = {"vendor_bills": "bill_date", "repair_invoices": "date", "misc_payables": "date", "invoices": "invoice_date",
           "payroll_runs": "pay_date", "sales_tax_returns": "due_date", "owner_draws": "date"}
    for name, col in cut.items():                   # nothing the restaurant couldn't have known on as_of
        t[name] = t[name][t[name][col] <= as_of]
    cat = F.categorize(bank.assign(date=pd.to_datetime(bank.date)), {k: t[k] for k in
                       ("suppliers", "vendor_bills", "leases", "repair_invoices", "misc_payables")})
    cat["date"] = cat.date.dt.date
    opening = int(t["accounts"].opening_balance_cents.sum())
    cash_today = opening + int(cat[cat.category != "Internal"].signed.sum())   # transfers net to zero across accounts
    report, report_note = load_report(report_id, as_of)
    return dict(as_of=as_of, bank=cat, bank_source=bank_source, books=t, sales=sales, cash_today=cash_today,
                report=report, report_note=report_note)


# ---------------------------------------------------------------- 2. the Bookkeeper report
def _bank_id_map():
    """Nessie's own ids -> our bank_ids (T + 9 hex), from the committed upload manifest, so a report may use either."""
    m = D / f"nessie_manifest_{TAG}.json"
    tx = json.loads(m.read_text()).get("tx", {}) if m.exists() else {}
    return {nid: bid for bid, nid in tx.items()}


def report_from_adjustments(adjustments, as_of):
    """contracts.Adjustment list -> the report shape apply_report() reads (adjustments come from verified findings)."""
    kinds = {"exclude_row": lambda a: {"kind": "exclude", "bank_ids": [a.bank_id]},
             "correct_row": lambda a: {"kind": "correct_to", "bank_id": a.bank_id, "cents": a.correct_amount_cents},
             "ongoing_overcharge": lambda a: {"kind": "ongoing", "monthly_cents": a.monthly_cents,
                                              "since": str(a.since) if a.since else None},
             "unpaid_bill": lambda a: {"kind": "owed", "cents": a.amount_cents, "due": str(a.due_date), "cost_id": a.cost_id}}
    adj = [a if isinstance(a, K.Adjustment) else K.Adjustment.model_validate(a) for a in adjustments]
    return dict(report_id=None, as_of=str(as_of), verification={"passed": True},
                findings=[dict(id=a.finding_id, root_cause=a.kind, verdict="error", recoverable=a.recoverable,
                               treatment=kinds[a.kind](a)) for a in adj])


def _read_report(path):
    r = json.loads(path.read_text())
    return r if r.get("verification", {}).get("passed") else None


def load_report(report_id, as_of):
    """The verified report to use: the one asked for, else the latest passed report dated on or before as_of.
    report_id="none" skips reports. Returns (report or None, note for the output)."""
    if report_id == "none":
        return None, "no Bookkeeper report used (--report none)"
    REPORTS.mkdir(parents=True, exist_ok=True)
    if report_id:
        p = REPORTS / f"{report_id}.json"
        r = _read_report(p) if p.exists() else None
        if r and date.fromisoformat(r["as_of"]) <= as_of:
            return r, f"report {report_id} (as of {r['as_of']})"
        why = "missing" if not p.exists() else "not verified by checks.py" if r is None else f"dated after {as_of}"
        fallback_note = f"requested report {report_id} is {why}; "
    else:
        fallback_note = ""
    good = [r for r in (_read_report(p) for p in sorted(REPORTS.glob("*.json"))) if r and date.fromisoformat(r["as_of"]) <= as_of]
    if good:
        r = max(good, key=lambda r: (r["as_of"], r["report_id"]))
        return r, fallback_note + f"using last verified report {r['report_id']} (as of {r['as_of']})"
    return None, fallback_note + "no verified Bookkeeper report: bookkeeping not yet reviewed, history used as the bank shows it"


def apply_report(h, settings):
    """exclude / correct_to fix the history the patterns learn from; ongoing / owed become future items.
    Today's cash is never touched: money that left by mistake is gone until it's refunded."""
    bank, items, assumptions = h["bank"].copy(), [], []
    r = h["report"]
    if r is None:
        return bank, items, assumptions
    win = settings["deposit_window_days"]
    ids = _bank_id_map()
    for f in r.get("findings", []):
        tr, label = f.get("treatment") or {}, f"{f.get('root_cause', 'finding')} ({f.get('id')})"
        kind = tr.get("kind")
        if kind == "exclude":
            bank = bank[~bank.bank_id.isin([ids.get(b, b) for b in tr.get("bank_ids", [])])]
        elif kind == "correct_to":
            sel = bank.bank_id == ids.get(tr["bank_id"], tr["bank_id"])
            bank.loc[sel, "amount"] = int(tr["cents"])
            bank.loc[sel, "signed"] = np.where(bank.loc[sel, "direction"] == "in", int(tr["cents"]), -int(tr["cents"]))
        elif kind == "ongoing":
            # the deposit ratios come from the last `win` days: the part of the problem inside that window is already
            # in the forecast; add only the rest. Without a start date, assume it started before the window.
            since = date.fromisoformat(str(tr["since"])) if tr.get("since") else None
            inside = 1.0 if since is None else max(0, (h["as_of"] - max(since, h["as_of"] - timedelta(win))).days) / win
            items.append(dict(kind="ongoing", finding=f.get("id"), monthly_cents=int(tr["monthly_cents"]),
                              extra_monthly_cents=int(round(int(tr["monthly_cents"]) * (1 - min(1, inside)))), label=label,
                              recoverable=f.get("recoverable") is True))
            assumptions.append(f"{label}: continues at ${int(tr['monthly_cents']) / 100:,.0f}/month until fixed"
                               + ("" if since else " (no start date given: assumed already in the last 8 weeks of bank data)"))
        elif kind == "owed":
            items.append(dict(kind="owed", finding=f.get("id"), cents=int(tr["cents"]),
                              due=date.fromisoformat(str(tr["due"])), label=label + (f" {tr['cost_id']}" if tr.get("cost_id") else "")))
            pay = next_bday(max(date.fromisoformat(tr["due"]), h["as_of"] + timedelta(1)))
            assumptions.append(f"{label}: ${int(tr['cents']) / 100:,.2f} still owed (due {tr['due']}), assumed paid {pay}")
        if f.get("verdict") == "needs_owner":
            assumptions.append(f"{label}: " + f.get("assumption_if_unconfirmed", "awaiting owner; treated as the report says"))
    return bank, items, assumptions


# ---------------------------------------------------------------- 3. patterns
def _revenue_model(s, as_of):
    """Per location: weekday effect, dow-adjusted level by (year, month), YoY trend + standard error, AR(1) noise, opening rules."""
    s = s.copy()
    s["dow"] = [d.weekday() for d in s.date]
    s["ym"] = [f"{d.year}-{d.month:02d}" for d in s.date]
    op = s[(s.open == 1) & (s.revenue > 0) & (s.has_event == 0)].copy()      # holidays/events would skew the monthly levels
    dow = pd.Series(1.0, index=range(7))
    for _ in range(2):                                         # alternate weekday and month effects
        lvl = op.groupby("ym").apply(lambda g: (g.revenue / dow[g.dow].values).mean(), include_groups=False)
        rel = op.revenue / lvl[op.ym].values
        dow = rel.groupby(op.dow).mean().reindex(range(7)).fillna(1.0)
        dow /= dow[op.dow.unique()].mean()
    op["adj"] = op.revenue / dow[op.dow].values
    lvl = op.groupby("ym").agg(level=("adj", "mean"), n=("adj", "size"))
    resid = np.log(op.revenue.values / (lvl.level[op.ym].values * dow[op.dow].values))
    sd = float(resid.std()) if len(resid) > 1 else 0.1
    phi = float(np.corrcoef(resid[:-1], resid[1:])[0, 1]) if len(resid) > 3 else 0.0
    rates, weights = [], []
    for m in range(1, 13):                                      # months seen in two different years
        rows = lvl[[k[5:] == f"{m:02d}" for k in lvl.index]]
        if len(rows) >= 2:
            y1, y2 = int(rows.index[0][:4]), int(rows.index[-1][:4])
            rates.append(np.log(rows.level.iloc[-1] / rows.level.iloc[0]) / (y2 - y1))
            weights.append((int(rows.n.iloc[0]), int(rows.n.iloc[-1])))
    if rates:
        w = [1 / (1 / a + 1 / b) for a, b in weights]
        trend = float(np.average(rates, weights=w))
        trend_se = float(sd / np.sqrt(sum(w)))
    else:
        trend, trend_se = 0.0, 0.05
    recent = op[op.date > as_of - timedelta(56)]
    recent_ym = (as_of.year, as_of.month)
    recent_level = float(recent.adj.mean()) if len(recent) else float(op.adj.mean())
    last12 = s[s.date > as_of - timedelta(84)]
    open_p = last12.groupby("dow").open.mean().reindex(range(7)).fillna(1.0)
    closed = {(d.month, d.day) for d in s[(s.open == 0) & (open_p[s.dow].values > 0.5)].date}   # holidays seen closed
    rec = s[s.date > as_of - timedelta(56)]
    dshare = float(rec.delivery_rev.sum() / rec.revenue.sum()) if rec.revenue.sum() else 0.0
    return dict(dow=dow.to_dict(), levels={k: float(v) for k, v in lvl.level.items()},
                recent_level=recent_level, recent_ym=recent_ym, trend=trend, trend_se=trend_se, sd=sd, phi=max(0.0, phi),
                open_by_dow=open_p.to_dict(), closed_md=sorted(closed), delivery_share=dshare, trend_from_months=len(rates))


def estimate_patterns(h, bank, settings):
    as_of, t, s = h["as_of"], h["books"], h["sales"]
    by_day = s.groupby("date")[["revenue", "delivery_rev"]].sum()
    rev = lambda a, b: int(by_day.revenue[(by_day.index > a) & (by_day.index <= b)].sum())
    dlv = lambda a, b: int(by_day.delivery_rev[(by_day.index > a) & (by_day.index <= b)].sum())
    w, wf = settings["deposit_window_days"], settings["food_window_days"]
    a0 = as_of - timedelta(w)
    inwin = bank[(bank.date > a0)]
    card_cash = int(inwin[inwin.category.isin(["Sales - Card", "Sales - Cash"])].amount.sum())
    dlv_paid = int(inwin[inwin.category == "Sales - Delivery"].amount.sum())
    card_ratio = card_cash / max(1, rev(a0 - timedelta(1), as_of - timedelta(1)) - dlv(a0 - timedelta(1), as_of - timedelta(1)))
    dlv_ratio = dlv_paid / max(1, dlv(a0 - timedelta(8), as_of - timedelta(8)))
    # food & beverage: paid on supplier terms after the sale that used it
    inv = t["invoices"][t["invoices"].invoice_date > as_of - timedelta(wf)]
    lag = int(round(np.average([(r.due_date - r.invoice_date).days for r in inv.itertuples()], weights=inv.total))) if len(inv) else 14
    food_out = bank[(bank.date > as_of - timedelta(wf)) & (bank.direction == "out")
                    & (bank.category.str.startswith("Food Supply") | (bank.category == "Beverage"))]
    food_ratio = int(food_out.amount.sum()) / max(1, rev(as_of - timedelta(wf + lag), as_of - timedelta(lag)))
    with closing(sqlite3.connect(FEATURES.as_uri() + "?mode=ro", uri=True)) as con:
        ing = pd.read_sql("select ingredient, month, avg_unit_price, mom_change from feat_ingredient_month", con)
    with closing(sqlite3.connect(BOOKS.as_uri() + "?mode=ro", uri=True)) as con:
        il = pd.read_sql("select l.ingredient, l.line_total, i.invoice_date from invoice_lines l join invoices i using(invoice_no)", con)
    il = il[pd.to_datetime(il.invoice_date).dt.date <= as_of]
    ing = ing[ing.month < f"{as_of:%Y-%m}"]                    # complete months only (no prices after as_of)
    wts = il.groupby("ingredient").line_total.sum()
    mom = ing.dropna().groupby("month").apply(lambda g: np.average(g.mom_change, weights=wts.reindex(g.ingredient).fillna(0) + 1),
                                               include_groups=False).sort_index().tail(6)
    drift = float(np.median(mom)) if len(mom) else 0.0
    drift_sd = float(1.4826 * np.median(np.abs(mom - np.median(mom)))) if len(mom) > 2 else 0.01
    # payroll = wages (fixed + variable x sales in its pay period) + tips passed through (they came in with card deposits)
    pr = t["payroll_runs"].tail(26).copy()
    pr["period_rev"] = [rev(r.period_start - timedelta(1), r.period_end) for r in pr.itertuples()]
    pr["wages"] = pr.total_debit - pr.tips_paid
    tip_ratio = float(pr.tips_paid.sum() / max(1, pr.period_rev.sum()))
    if len(pr) >= 6 and pr.period_rev.std() > 0:
        b, a = np.polyfit(pr.period_rev, pr.wages, 1)
        if b < 0:
            a, b = pr.wages.mean(), 0.0
    else:
        a, b = pr.wages.mean(), 0.0
    pres = pr.total_debit - (a + (b + tip_ratio) * pr.period_rev)
    pay_sd = float((pres / pr.total_debit).std()) if len(pr) > 2 else 0.02
    last_pr = t["payroll_runs"].iloc[-1]
    # sales tax per $ of POS sales, from returns filed by as_of whose whole month is in the sales data
    first_sale = by_day.index.min()
    tr = t["sales_tax_returns"]
    tr = tr[[pd.Period(p).start_time.date() >= first_sale for p in tr.period]].tail(6)
    per_rev = [(r.tax_due, rev(pd.Period(r.period).start_time.date() - timedelta(1), pd.Period(r.period).end_time.date()))
               for r in tr.itertuples()]
    tax_rate = sum(x for x, _ in per_rev) / max(1, sum(y for _, y in per_rev))
    # repairs: how often and how much
    rp = t["repair_invoices"]
    span = max(1, (as_of - min(rp.date.min(), C.START)).days) if len(rp) else 365
    rep = dict(per_day=len(rp) / span, log_mu=float(np.log(rp.amount).mean()) if len(rp) else 0.0,
               log_sd=float(np.log(rp.amount).std()) if len(rp) > 1 else 0.5,
               mean=float(rp.amount.mean()) if len(rp) else 0.0)
    locs = {loc: _revenue_model(g, as_of) for loc, g in s.groupby("location")}
    return dict(locations=locs, card_ratio=card_ratio, dlv_ratio=dlv_ratio, food_ratio=food_ratio, food_lag_days=lag,
                price_drift=drift, price_drift_sd=drift_sd, payroll=dict(a=float(a), b=float(b) + tip_ratio, wage_b=float(b),
                                                         tip_ratio=tip_ratio, sd=pay_sd,
                last_period_end=last_pr.period_end, recent_mean=float(t["payroll_runs"].total_debit.tail(6).mean())),
                tax_rate=tax_rate, repairs=rep, by_day=by_day)


# ---------------------------------------------------------------- 4. fixed calendar
def _rent(lease, d):
    a, steps = lease["anniversary"], 0
    while a <= d:
        steps += 1
        a = date(a.year + 1, a.month, a.day)
    return int(round(lease["base"] * (1 + lease["escalator_pct"]) ** steps))


def build_calendar(h, P, items, days):
    """Known future money, day by day. Deterministic amounts go in fixed_in / fixed_out; amounts that depend on simulated
    sales (payroll, sales tax) or vary (vendor bills) are listed for simulate()."""
    as_of, t = h["as_of"], h["books"]
    dates = [as_of + timedelta(i + 1) for i in range(days)]
    idx = {d: i for i, d in enumerate(dates)}
    fixed_in, fixed_out, upcoming = np.zeros(days), np.zeros(days), []

    def out(d, cents, label, kind, payee=None):
        d = max(d, as_of + timedelta(1))
        if d in idx:
            fixed_out[idx[d]] += cents
            upcoming.append(dict(date=str(d), cents=int(round(cents)), label=label, kind=kind, **({"payee": payee} if payee else {})))

    end = dates[-1]
    for loc, L in t["leases"].set_index("location").iterrows():
        for y, m in _months(as_of, end):
            d = next_bday(date(y, m, 1))
            if d > as_of:
                out(d, _rent(L, date(y, m, 1)), f"rent {loc}", "rent")
    ls, loan = t["loan_schedule"], t["loans"].iloc[0]
    fut = ls[ls.due > as_of]
    for r in fut.itertuples():
        out(next_bday(r.due), r.payment, "loan payment", "loan")
    bal = int(ls.balance_after.iloc[-1]) if len(ls) else int(loan.balance_at_start_cents)
    y, m = (ls.due.iloc[-1].year, ls.due.iloc[-1].month) if len(ls) else (as_of.year, as_of.month)
    while bal > 0:                                              # schedule ends before the loan does: keep paying
        m, y = (1, y + 1) if m == 12 else (m + 1, y)
        d = next_bday(date(y, m, int(loan.pay_day)))
        if d > end:
            break
        pay = min(int(loan.monthly_cents), bal + int(round(bal * loan.rate / 12)))
        bal = bal + int(round(bal * loan.rate / 12)) - pay
        out(d, pay, "loan payment", "loan")
    dr = t["owner_draws"]
    if len(dr):
        draw_day = int(pd.Series([d.day for d in dr.date]).mode().iloc[0])      # e.g. the 15th
        for y, m in _months(as_of, end):
            out_d = next_bday(date(y, m, draw_day))
            if out_d > as_of:
                out(out_d, int(dr.amount.iloc[-1]), "owner draw", "owner_draw")
    for r in t["misc_payables"].itertuples():                   # annual permits and licenses
        for k in (1, 2):
            out_d = next_bday(date(r.date.year + k, r.date.month, r.date.day))
            if as_of < out_d <= end:
                out(out_d, int(r.amount), r.description, "permit")
    # supplier invoices already received but not yet due (payment plans pay in installments)
    plans = t["ap_payment_plans"]
    for r in t["invoices"].itertuples():
        if r.invoice_no in set(plans.invoice_no):
            for p in plans[plans.invoice_no == r.invoice_no].itertuples():
                if next_bday(p.due) > as_of:
                    out(next_bday(p.due), int(p.amount), f"{r.supplier} {r.invoice_no} installment", "open_invoice", r.supplier)
        elif next_bday(r.due_date) > as_of:
            out(next_bday(r.due_date), int(r.total), f"{r.supplier} {r.invoice_no}", "open_invoice", r.supplier)
    # monthly vendor bills (utilities, linen, insurance, ...): a bill already received is exact, later months vary
    vb, vendor_sim = t["vendor_bills"], []
    for (vendor, loc), g in vb.groupby(["vendor", "location"]):
        g = g.sort_values("due_date")
        if len(g) < 3 or (as_of - g.bill_date.iloc[-1]).days > 62:
            continue
        due_day = int(pd.Series([d.day for d in g.due_date]).mode().iloc[0])
        mean, cv = float(g.amount.tail(6).mean()), float(g.amount.tail(6).std() / g.amount.tail(6).mean())
        known = {(d.year, d.month): int(a) for d, a in zip(g.due_date, g.amount)}
        for y, m in _months(as_of, end):
            d = next_bday(_dom(y, m, due_day))
            if d <= as_of or d not in idx:
                continue
            if (y, m) in known:
                out(d, known[(y, m)], vendor, "vendor_bill")
            else:
                vendor_sim.append((idx[d], mean, cv))
    for it in items:
        if it["kind"] == "owed":
            out(next_bday(max(it["due"], as_of + timedelta(1))), it["cents"], it["label"], "owed")
        elif it["kind"] == "ongoing" and it["extra_monthly_cents"]:
            fixed_out += it["extra_monthly_cents"] / DAYS_PER_MONTH
            upcoming.append(dict(date=str(as_of + timedelta(1)), cents=it["extra_monthly_cents"], label=it["label"] + " (per month)",
                                 kind="ongoing"))
    # payroll every 2 weeks: amount depends on sales in its pay period (part known, part simulated)
    by_day, payroll = P["by_day"].revenue, []
    nominal = P["payroll"]["last_period_end"] + timedelta(5)
    while True:
        nominal += timedelta(14)
        d = prev_bday(nominal)
        if d > end:
            break
        if d <= as_of:
            continue
        p0, p1 = nominal - timedelta(18), nominal - timedelta(5)
        known = int(by_day[(by_day.index >= p0) & (by_day.index <= min(p1, as_of))].sum())
        lo, hi = max(0, (p0 - as_of).days - 1), max(0, (p1 - as_of).days)
        payroll.append((idx[d], known, lo, hi))
    # sales tax: around the 24th, on the previous month's sales
    tax = []
    tr = t["sales_tax_returns"]
    tax_day = int(pd.Series([d.day for d in tr.due_date]).mode().iloc[0]) if len(tr) else 24
    for y, m in _months(as_of, end):
        d = next_bday(date(y, m, tax_day))
        if d <= as_of or d not in idx:
            continue
        pm = pd.Period(f"{y}-{m:02d}") - 1
        p0, p1 = pm.start_time.date(), pm.end_time.date()
        known = int(by_day[(by_day.index >= p0) & (by_day.index <= min(p1, as_of))].sum())
        lo, hi = max(0, (p0 - as_of).days - 1), max(0, (p1 - as_of).days)
        tax.append((idx[d], known, lo, hi))
    # sales already made but not yet in the bank (card next business day, delivery weekly)
    card_tgt, dlv_tgt = _deposit_targets([as_of + timedelta(i) for i in range(-14, 1)], as_of)
    hist = P["by_day"].reindex([as_of + timedelta(i) for i in range(-14, 1)]).fillna(0)
    for j, (r, dl) in enumerate(zip(hist.revenue, hist.delivery_rev)):
        if card_tgt[j] >= 0:
            fixed_in[card_tgt[j]] += (r - dl) * P["card_ratio"]
        if dlv_tgt[j] >= 0:
            fixed_in[dlv_tgt[j]] += dl * P["dlv_ratio"]
    upcoming.sort(key=lambda x: x["date"])
    return dict(dates=dates, fixed_in=fixed_in, fixed_out=fixed_out, vendor_sim=vendor_sim, payroll=payroll, tax=tax,
                upcoming=upcoming)


def _deposit_targets(src_days, as_of):
    """Future-day index where each source day's sales land: card/cash next business day; delivery the Tuesday 8 days after
    its week's Monday. -1 = landed on or before as_of."""
    card, dlv = [], []
    for d in src_days:
        c = next_bday(d + timedelta(1))
        w = d - timedelta(d.weekday())
        p = next_bday(w + timedelta(8))
        card.append((c - as_of).days - 1 if c > as_of else -1)
        dlv.append((p - as_of).days - 1 if p > as_of else -1)
    return np.array(card), np.array(dlv)


def expected_sales(P, dates):
    """Expected POS sales per future day (all locations), before noise and trend; plus the delivery share per day."""
    total, dshare = np.zeros(len(dates)), np.zeros(len(dates))
    for loc, M in P["locations"].items():
        lv = np.array([M["levels"].get(f"{d.year}-{d.month:02d}") or
                       next((M["levels"][k] for k in sorted(M["levels"], reverse=True) if int(k[5:]) == d.month), None) or
                       M["recent_level"] for d in dates])
        # years between the month a level was observed and the forecast day, for the trend
        obs = []
        for d in dates:
            ks = [k for k in M["levels"] if int(k[5:]) == d.month]
            k = max(ks) if ks else f"{M['recent_ym'][0]}-{M['recent_ym'][1]:02d}"
            obs.append(((d.year - int(k[:4])) * 12 + d.month - int(k[5:])) / 12)
        dow = np.array([M["dow"][d.weekday()] for d in dates])
        is_open = np.array([M["open_by_dow"][d.weekday()] > 0.5 and (d.month, d.day) not in set(map(tuple, M["closed_md"]))
                            for d in dates])
        e = lv * dow * is_open
        total += e
        dshare += e * M["delivery_share"]
        M["_years"] = np.array(obs)
        M["_expected"] = e
    return total, np.divide(dshare, total, out=np.zeros_like(total), where=total > 0)


# ---------------------------------------------------------------- 5. simulate
def simulate(P, cal, cash_today, trials, seed, deterministic=False, adj=None):
    """trials x days matrix of end-of-day cash (cents). All trials run at once.
    `adj` (from apply_scenario) changes the calendar, sales and food costs. Random numbers are drawn in the same order with or
    without it, so a scenario and its base share every draw and their difference is the scenario's effect, not noise."""
    rng = np.random.default_rng(seed)
    dates, H = cal["dates"], len(cal["dates"])
    if "_expected" not in next(iter(P["locations"].values())):
        expected_sales(P, dates)
    rev, rev_dlv = np.zeros((trials, H)), np.zeros((trials, H))
    for M in P["locations"].values():
        trend = np.full(trials, M["trend"]) if deterministic else rng.normal(M["trend"], M["trend_se"], trials)
        if deterministic:
            eps = np.zeros((trials, H))
        else:
            sd, phi = M["sd"], min(M["phi"], 0.95)
            z = rng.normal(0, sd * np.sqrt(1 - phi ** 2), (trials, H))
            eps = np.empty((trials, H))
            eps[:, 0] = rng.normal(0, sd, trials)
            for j in range(1, H):
                eps[:, j] = phi * eps[:, j - 1] + z[:, j]
            eps -= sd ** 2 / 2                                 # keep the mean, not the median, on the expected path
        r = M["_expected"][None, :] * np.exp(trend[:, None] * M["_years"][None, :] + eps)
        rev += r
        rev_dlv += r * M["delivery_share"]
    if adj is not None:
        rev *= adj["rev_mult"][None, :]
        rev_dlv *= adj["rev_mult"][None, :]
    inflow = np.tile(adj["fixed_in"] if adj is not None else cal["fixed_in"], (trials, 1))
    card_tgt, dlv_tgt = _deposit_targets(dates, dates[0] - timedelta(1))
    for tgt, vals in ((card_tgt, (rev - rev_dlv) * P["card_ratio"]), (dlv_tgt, rev_dlv * P["dlv_ratio"])):
        ok = (tgt >= 0) & (tgt < H)
        np.add.at(inflow.T, tgt[ok], vals[:, ok].T)
    outflow = np.tile(adj["fixed_out"] if adj is not None else cal["fixed_out"], (trials, 1))
    # food & beverage purchases: share of sales, paid `lag` days later (plus any stretch), at drifting ingredient prices
    lag = P["food_lag_days"]
    month_no = np.array([(d.year - dates[0].year) * 12 + d.month - dates[0].month for d in dates])
    steps = np.full((trials, month_no.max() + 1), P["price_drift"]) if deterministic else \
        rng.normal(P["price_drift"], P["price_drift_sd"], (trials, month_no.max() + 1))
    price = np.exp(np.cumsum(steps, axis=1))[:, month_no]
    rev_food = rev * adj["food_mult"][None, :] if adj is not None else rev
    stretch = adj["stretch"] if adj is not None else []
    for share, extra in [(1 - sum(s for s, _ in stretch), 0)] + stretch:
        L = lag + extra
        if share > 0 and L < H:
            outflow[:, L:] += P["food_ratio"] * share * rev_food[:, :H - L] * price[:, L:]
    pay = P["payroll"]
    for j, known, lo, hi in cal["payroll"]:
        amt = pay["a"] + pay["b"] * (known + rev[:, lo:hi].sum(axis=1))
        outflow[:, j] += amt if deterministic else amt * np.exp(rng.normal(0, pay["sd"], trials))
    for j, known, lo, hi in cal["tax"]:
        outflow[:, j] += P["tax_rate"] * (known + rev[:, lo:hi].sum(axis=1))
    for j, mean, cv in cal["vendor_sim"]:
        outflow[:, j] += mean if deterministic or not np.isfinite(cv) else mean * np.exp(rng.normal(-cv ** 2 / 2, cv, trials))
    R = P["repairs"]
    if deterministic:
        outflow += R["per_day"] * R["mean"]
    else:
        n = rng.poisson(R["per_day"], (trials, H))
        outflow += n * rng.lognormal(R["log_mu"], R["log_sd"], (trials, H))
    return cash_today + np.cumsum(inflow - outflow, axis=1)


# ---------------------------------------------------------------- 6. summarize
def _first_below(paths, level):
    below = paths < level
    return np.where(below.any(axis=1), below.argmax(axis=1), np.inf)


def _pct_date(first, q, dates):
    v = np.percentile(first, q, method="lower")
    return None if not np.isfinite(v) else str(dates[int(v)])


def summarize(paths, dates, floor, as_of):
    first0, firstf = _first_below(paths, 0), _first_below(paths, floor)
    by = {}
    for p in pd.period_range(as_of, dates[-1], freq="Q")[1:]:
        e = p.end_time.date()
        if e <= dates[-1]:
            by[str(e)] = round(float(np.mean(first0 <= (e - as_of).days - 1)), 3)
    bands = []
    for y, m in _months(dates[0], dates[-1]):
        e = min(pd.Period(f"{y}-{m:02d}").end_time.date(), dates[-1])
        j = (e - as_of).days - 1
        p10, p50, p90 = np.percentile(paths[:, j], [10, 50, 90])
        bands.append(dict(month=f"{y}-{m:02d}", p10_cents=int(p10), p50_cents=int(p50), p90_cents=int(p90)))
    low = np.median(paths, axis=0)
    return dict(runout={"p10": _pct_date(first0, 10, dates), "p50": _pct_date(first0, 50, dates),
                        "p90": _pct_date(first0, 90, dates),
                        "never_within_horizon_pct": round(float(np.mean(~np.isfinite(first0))), 3)},
                below_floor={"floor_cents": int(floor), "p10": _pct_date(firstf, 10, dates), "p50": _pct_date(firstf, 50, dates),
                             "p90": _pct_date(firstf, 90, dates)},
                prob_runout_by=by, monthly_bands=bands,
                median_low_point={"date": str(dates[int(low.argmin())]), "cents": int(low.min())})


# ---------------------------------------------------------------- 7. scenarios ("what if")
SCENARIO_KEYS = {"label", "start", "one_off_costs", "one_off_income", "sales_change", "price_changes", "price_response",
                 "remove_items", "substitution", "stretch_payments", "apply_fixes", "fix_delay_days"}


def _cash_per_sales_dollar(P):
    """Roughly how much cash one more dollar of sales leaves after deposits, sales tax and payroll (food handled separately)."""
    d = next(iter(P["locations"].values()))["delivery_share"]
    return (1 - d) * P["card_ratio"] + d * P["dlv_ratio"] - P["tax_rate"] - P["payroll"]["b"]


def apply_scenario(prep, scenario):
    """Turn a "what if" into changes to the calendar, sales and food costs. Returns (adj, effects, notes).

    one_off_costs / one_off_income  [{"date", "cents", "label"}]   e.g. a $4,800 fridge, a supplier refund
    sales_change      {"2027-01": -0.15, ...} or {"all": -0.05}   customers up/down by month
    price_changes     {"Margherita Pizza": 200}                     cents per plate, from `start`
    price_response    0.05 or {"Margherita Pizza": 0.05}           share of that dish's orders lost on a rise (gained on a cut)
    remove_items      ["Lobster Ravioli"]                           stop selling, from `start`
    substitution      0.7                                           share of those diners who order another dish of the course
    stretch_payments  {"Northbay Protein Co": 10}                   pay that supplier N days later (open invoices too)
    apply_fixes       ["F4"]                                        stop an `ongoing` finding from the Bookkeeper report
    fix_delay_days    14                                            days until a fix takes effect
    start             "2026-11-01"                                  when price / menu changes begin (default: tomorrow)
    """
    sc = dict(scenario or {})
    unknown = set(sc) - SCENARIO_KEYS
    if unknown:
        raise ValueError(f"unknown scenario keys {sorted(unknown)}; allowed: {sorted(SCENARIO_KEYS)}")
    h, P, cal = prep["h"], prep["P"], prep["cal"]
    as_of, dates = h["as_of"], cal["dates"]
    H, idx = len(dates), {d: i for i, d in enumerate(dates)}
    fixed_in, fixed_out = cal["fixed_in"].copy(), cal["fixed_out"].copy()
    rev_mult, food_mult, stretch, effects, notes = np.ones(H), np.ones(H), [], [], []
    cps = _cash_per_sales_dollar(P)

    def day(x, what):
        d = max(date.fromisoformat(str(x)), as_of + timedelta(1))
        if d not in idx:
            raise ValueError(f"{what}: {x} is outside the forecast window {dates[0]}..{dates[-1]}")
        return idx[d]

    start = day(sc.get("start", as_of + timedelta(1)), "start")
    for c in sc.get("one_off_costs", []):
        fixed_out[day(c["date"], "one_off_costs")] += int(c["cents"])
        effects.append(dict(kind="one_off_cost", label=c.get("label", "one-off cost"), date=str(c["date"]), cents=-int(c["cents"])))
    for c in sc.get("one_off_income", []):
        fixed_in[day(c["date"], "one_off_income")] += int(c["cents"])
        effects.append(dict(kind="one_off_income", label=c.get("label", "one-off income"), date=str(c["date"]), cents=int(c["cents"])))
    for m, chg in (sc.get("sales_change") or {}).items():
        mask = np.ones(H, bool) if m == "all" else np.array([f"{d:%Y-%m}" == m for d in dates])
        if not mask.any():
            raise ValueError(f"sales_change: month {m} is outside the forecast window")
        rev_mult[mask] *= 1 + float(chg)
        effects.append(dict(kind="sales_change", month=m, change=float(chg)))
    # menu changes: dish economics from the last full month, applied as sales and food-cost multipliers from `start`
    if sc.get("price_changes") or sc.get("remove_items"):
        Dm = prep["dishes"]
        R, F0 = float(Dm.revenue.sum()), P["food_ratio"] * float(Dm.revenue.sum())
        dR = dF = 0.0
        resp = sc.get("price_response", 0.05)
        for item, delta in (sc.get("price_changes") or {}).items():
            if item not in Dm.index:
                raise ValueError(f"price_changes: unknown dish {item!r}; dishes: {sorted(Dm.index)}")
            r = Dm.loc[item]
            k = float(resp.get(item, 0.05) if isinstance(resp, dict) else resp)
            u2 = r.units * (1 - k if delta > 0 else 1 + k if delta < 0 else 1)
            dRi, dFi = (r.avg_price + delta) * u2 - r.avg_price * r.units, r.plate_cost * (u2 - r.units)
            dR, dF = dR + dRi, dF + dFi
            effects.append(dict(kind="price_change", item=item, delta_cents=int(delta), orders_change=-k if delta > 0 else k,
                                units_per_month=int(r.units), sales_per_month_cents=int(dRi), food_per_month_cents=int(dFi),
                                approx_net_per_month_cents=int(dRi * cps - dFi)))
        sub = float(sc.get("substitution", 0.7))
        removed = list(sc.get("remove_items") or [])
        for item in removed:
            if item not in Dm.index:
                raise ValueError(f"remove_items: unknown dish {item!r}; dishes: {sorted(Dm.index)}")
            r = Dm.loc[item]
            peers = Dm[(Dm.course == r.course) & ~Dm.index.isin(removed)]
            pa = float(np.average(peers.avg_price, weights=peers.units)) if len(peers) else 0.0
            ca = float(np.average(peers.plate_cost, weights=peers.units)) if len(peers) else 0.0
            dRi, dFi = -r.avg_price * r.units + sub * r.units * pa, -r.plate_cost * r.units + sub * r.units * ca
            dR, dF = dR + dRi, dF + dFi
            effects.append(dict(kind="remove_item", item=item, units_per_month=int(r.units), substitution=sub,
                                sales_per_month_cents=int(dRi), food_per_month_cents=int(dFi),
                                approx_net_per_month_cents=int(dRi * cps - dFi)))
        rf = 1 + dR / R
        rev_mult[start:] *= rf
        food_mult[start:] *= (F0 + dF) / (F0 * rf)
        notes.append(f"Menu changes from {dates[start]}: sales {dR / 100:+,.0f}/month, food cost {dF / 100:+,.0f}/month "
                     f"(dish numbers from {prep['dishes_month']}; orders lost per price rise as given)")
    for sup, extra in (sc.get("stretch_payments") or {}).items():
        if sup not in prep["supplier_share"]:
            raise ValueError(f"stretch_payments: unknown supplier {sup!r}; suppliers: {sorted(prep['supplier_share'])}")
        extra = int(extra)
        stretch.append((prep["supplier_share"][sup], extra))
        moved = 0
        for u in cal["upcoming"]:
            if u["kind"] == "open_invoice" and u.get("payee") == sup:
                old = date.fromisoformat(u["date"])
                fixed_out[idx[old]] -= u["cents"]
                new = next_bday(old + timedelta(extra))
                if new in idx:
                    fixed_out[idx[new]] += u["cents"]
                moved += u["cents"]
        tol = prep["supplier_tolerance"].get(sup)
        effects.append(dict(kind="stretch_payments", supplier=sup, extra_days=extra, share_of_food_spend=round(prep["supplier_share"][sup], 3),
                            open_invoices_moved_cents=int(moved), supplier_tolerance_days=tol))
        if tol is not None and extra > tol:
            notes.append(f"{sup}: stretching {extra} days is more than the ~{tol} days this kind of supplier usually tolerates")
    for fid in sc.get("apply_fixes", []):
        hit = [it for it in prep["items"] if it["kind"] == "ongoing" and it["finding"] == fid]
        if not hit:
            raise ValueError(f"apply_fixes: {fid!r} is not an ongoing finding in report {prep['h']['report_note']}")
        j0 = min(H - 1, day(as_of + timedelta(int(sc.get("fix_delay_days", 14))), "apply_fixes"))
        fixed_in[j0:] += hit[0]["monthly_cents"] / DAYS_PER_MONTH
        effects.append(dict(kind="apply_fix", finding=fid, label=hit[0]["label"], from_date=str(dates[j0]),
                            per_month_cents=int(hit[0]["monthly_cents"])))
    return dict(fixed_in=fixed_in, fixed_out=fixed_out, rev_mult=rev_mult, food_mult=food_mult, stretch=stretch), effects, notes


def _days_between(a, b):
    """Days from date a to date b (positive = b is later); None when either is beyond the horizon."""
    return None if a is None or b is None else (date.fromisoformat(b) - date.fromisoformat(a)).days


def compare(base, scen, base_paths=None, scen_paths=None, dates=None):
    """How the scenario moves the dates. Positive days = later = more runway.
    Run-out dates land on paydays, so percentile dates move in ~2-week steps; with the paths, two smooth measures are added:
    the average shift per future (each trial against its own base: same random draws) and the extra cash on the base's
    likely run-out day."""
    smooth = {}
    if base_paths is not None and scen_paths is not None:
        fb, fs = _first_below(base_paths, 0), _first_below(scen_paths, 0)
        both = np.isfinite(fb) & np.isfinite(fs)
        smooth["runout_mean_days_change"] = round(float(np.mean(fs[both] - fb[both])), 1) if both.any() else None
        smooth["futures_saved_pct"] = round(float(np.mean(np.isfinite(fb) & ~np.isfinite(fs))), 3)
        smooth["futures_lost_pct"] = round(float(np.mean(~np.isfinite(fb) & np.isfinite(fs))), 3)
        if base["runout"]["p50"] and dates is not None:
            j = dates.index(date.fromisoformat(base["runout"]["p50"]))
            smooth["cash_change_on_base_likely_runout_cents"] = int(np.median(scen_paths[:, j] - base_paths[:, j]))
    return dict(
        **smooth,
        runout_days={q: _days_between(base["runout"][q], scen["runout"][q]) for q in ("p10", "p50", "p90")},
        below_floor_days={q: _days_between(base["below_floor"][q], scen["below_floor"][q]) for q in ("p10", "p50", "p90")},
        never_within_horizon_pct_change=round(scen["runout"]["never_within_horizon_pct"] - base["runout"]["never_within_horizon_pct"], 3),
        prob_runout_by_change={k: round(scen["prob_runout_by"][k] - base["prob_runout_by"][k], 3) for k in base["prob_runout_by"]},
        base_runout=base["runout"], base_below_floor=base["below_floor"])


# ---------------------------------------------------------------- 8. chart
def make_chart(paths, dates, floor, out_png, base_paths=None, actual=None, title=None, show_days=None, markers=()):
    """Band chart: likely path, 10-90% range, $0 and the payroll + rent line; optional base median, actual balance and
    vertical markers [(date, label, color)]. `show_days` trims the x axis so a 2-year horizon doesn't hide the story."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = min(len(dates), show_days or len(dates))
    paths, dates = paths[:, :n], dates[:n]
    base_paths = None if base_paths is None else base_paths[:, :n]
    x = pd.to_datetime(dates)
    p10, p50, p90 = np.percentile(paths, [10, 50, 90], axis=0) / 100
    fig, ax = plt.subplots(figsize=(9, 3.6))
    ax.fill_between(x, p10, p90, color="#3b6ea5", alpha=.18, lw=0, label="10-90% of futures")
    ax.plot(x, p50, color="#3b6ea5", lw=2, label="likely")
    if base_paths is not None:
        ax.plot(x, np.median(base_paths, axis=0) / 100, color="grey", ls=":", lw=1.6, label="base (no change)")
    if actual is not None:
        ax.plot(pd.to_datetime(actual.index), actual.values / 100, color="black", lw=1.4, label="actual")
    ax.axhline(0, color="#b03a2e", lw=1)
    ax.axhline(floor / 100, color="#b03a2e", ls="--", lw=.8, label="one payroll + rent")
    for k, (d, label, color) in enumerate(markers):
        if d and date.fromisoformat(d) <= dates[-1]:
            ax.axvline(pd.Timestamp(d), color=color, lw=1, ls="-.")
            ax.annotate(label, (pd.Timestamp(d), ax.get_ylim()[1]), xytext=(3, -12 - 13 * k), textcoords="offset points",
                        fontsize=8, color=color)
    lo = min(p10.min(), 0 if actual is None else actual.min() / 100)
    start = (actual.iloc[0] / 100) if actual is not None else p50[0]
    ax.set_ylim(bottom=max(lo, -0.6 * abs(start)) * 1.05)
    ax.yaxis.set_major_formatter(matplotlib.ticker.FuncFormatter(lambda v, _: f"${v / 1000:,.0f}k"))
    ax.set_title(title or "Cash in the bank"); ax.legend(frameon=False, fontsize=8, loc="lower left")
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    fig.tight_layout(); fig.savefig(out_png, dpi=130); plt.close(fig)
    return str(out_png)


# ---------------------------------------------------------------- 9. cache, save, entry point
_CACHE = {}


def _stamp():
    """Changes whenever an input file does, so the cache never serves stale data."""
    files = [BOOKS, FEATURES, D / "bank_feed.csv", D / f"nessie_sync_{TAG}.db", *sorted(REPORTS.glob("*.json"))]
    return tuple((f.name, f.stat().st_mtime_ns) for f in files if f.exists())


def prepare(as_of=None, report_id=None, settings=None, adjustments=None):
    """Steps 1-4 (load, report, patterns, calendar). They don't depend on the scenario: cached, so a "what if" only re-simulates.
    `adjustments` (contracts.Adjustment) replace the saved Bookkeeper report when given."""
    S = {**DEFAULTS, **(settings or {})}
    adj_key = None if adjustments is None else json.dumps(
        [a.model_dump(mode="json") if isinstance(a, K.Adjustment) else a for a in adjustments], sort_keys=True, default=str)
    key = (str(as_of), report_id, adj_key, S["deposit_window_days"], S["food_window_days"], S["horizon_days"],
           S["horizon_months"], _stamp())
    if key in _CACHE:
        return _CACHE[key]
    h = load_history(as_of, "none" if adjustments is not None else report_id)
    if adjustments is not None:
        h["report"] = report_from_adjustments(adjustments, h["as_of"])
        h["report_note"] = f"{len(adjustments)} adjustment(s) passed in by the caller"
    bank, items, rep_assumptions = apply_report(h, S)
    P = estimate_patterns(h, bank, S)
    days = S["horizon_days"] or (pd.Timestamp(h["as_of"]) + pd.DateOffset(months=S["horizon_months"]) - pd.Timestamp(h["as_of"])).days
    cal = build_calendar(h, P, items, days)
    expected_sales(P, cal["dates"])
    with closing(sqlite3.connect(FEATURES.as_uri() + "?mode=ro", uri=True)) as con:
        im = pd.read_sql("select item, month, units, revenue, avg_price, course, plate_cost from feat_item_month", con)
        sup = pd.read_sql("select supplier, stretch_days_available from feat_supplier", con)
    full = sorted(m for m in im.month.unique() if m < f"{h['as_of']:%Y-%m}")
    dm = full[-1] if full else im.month.max()
    inv = h["books"]["invoices"]
    inv = inv[inv.invoice_date > h["as_of"] - timedelta(S["food_window_days"])]
    share = (inv.groupby("supplier").total.sum() / inv.total.sum()).to_dict() if len(inv) else {}
    prep = dict(h=h, items=items, rep_assumptions=rep_assumptions, P=P, cal=cal,
                dishes=im[im.month == dm].set_index("item"), dishes_month=dm, supplier_share=share,
                supplier_tolerance={r.supplier: int(r.stretch_days_available) for r in sup.itertuples()}, runs={})
    if len(_CACHE) > 8:
        _CACHE.clear()
    _CACHE[key] = prep
    return prep


def _run(prep, S, adj=None):
    trials = 1 if S["deterministic"] else S["trials"]
    run_key = (trials, S["seed"], S["deterministic"])
    if adj is None and run_key in prep["runs"]:
        return prep["runs"][run_key]
    paths = simulate(prep["P"], prep["cal"], prep["h"]["cash_today"], trials, S["seed"], S["deterministic"], adj)
    out = (paths, summarize(paths, prep["cal"]["dates"], _floor(prep), prep["h"]["as_of"]))
    if adj is None:
        prep["runs"][run_key] = out
    return out


def _floor(prep):
    rent_now = sum(_rent(L, prep["h"]["as_of"]) for _, L in prep["h"]["books"]["leases"].iterrows())
    return prep["P"]["payroll"]["recent_mean"] + rent_now


def _assumptions(prep, S):
    P = prep["P"]
    M = next(iter(P["locations"].values()))
    return list(prep["rep_assumptions"]) + [
        f"Sales follow last year's same month, adjusted by weekday, with a {M['trend'] * 100:+.1f}%/yr trend (±{M['trend_se'] * 100:.1f}%)"
        + ("" if M["trend_from_months"] else ": no month seen twice yet, so the trend is assumed flat"),
        f"Card/cash deposits arrive at {P['card_ratio']:.3f} of dine-in sales and delivery payouts at {P['dlv_ratio']:.3f} of "
        f"delivery sales, as the bank paid over the last {S['deposit_window_days']} days (any live fee problem continues)",
        f"Food & beverage costs {P['food_ratio'] * 100:.1f}% of sales, paid ~{P['food_lag_days']} days later; ingredient prices drift "
        f"{P['price_drift'] * 100:+.2f}%/month",
        f"Payroll every 2 weeks: ${P['payroll']['a'] / 100:,.0f} + {P['payroll']['wage_b'] * 100:.1f}% of the period's sales in wages "
        f"+ tips at {P['payroll']['tip_ratio'] * 100:.1f}% of sales",
        "Rent, loan, owner draw, permits and monthly vendor bills follow their schedules; repairs at last year's rate; "
        "holiday/event sales bumps, chargebacks, refunds and new borrowing are not modeled",
    ]


def save_forecast(result, paths=None, base_paths=None, chart=True):
    """Writes data/forecasts/<forecast_id>.json (+ .png chart) and returns the result with its id."""
    FORECASTS.mkdir(parents=True, exist_ok=True)
    n = 1 + sum(1 for _ in FORECASTS.glob(f"FC-{result['as_of']}-*.json"))
    result["forecast_id"] = f"FC-{result['as_of']}-{n:04d}"
    if chart and paths is not None:
        title = "Cash in the bank" + (f": {result['scenario'].get('label', 'scenario')}" if result.get("scenario") else "")
        result["chart_png"] = _chart_for(result, paths, base_paths, title)
    (FORECASTS / f"{result['forecast_id']}.json").write_text(json.dumps(result, indent=1, default=str))
    return result


def _chart_for(result, paths, base_paths, title):
    as_of = date.fromisoformat(result["as_of"])
    dates = [as_of + timedelta(i + 1) for i in range(paths.shape[1])]
    marks = [(result["runout"]["p50"], f"likely $0: {result['runout']['p50']}", "#3b6ea5")]
    if result.get("vs_base"):
        marks.append((result["vs_base"]["base_runout"]["p50"], f"base: {result['vs_base']['base_runout']['p50']}", "grey"))
    last = [date.fromisoformat(d) for d in (result["runout"]["p50"], (result.get("vs_base") or {}).get("base_runout", {}).get("p50")) if d]
    show = min(len(dates), max(365, (max(last) - as_of).days + 75)) if last else 460
    return make_chart(paths, dates, result["below_floor"]["floor_cents"], FORECASTS / f"{result['forecast_id']}.png", base_paths,
                      title=title, show_days=show, markers=marks)


def _dinner_price(item, as_of):
    with closing(sqlite3.connect(BOOKS.as_uri() + "?mode=ro", uri=True)) as con:
        r = con.execute("select price_cents from menu_prices where item = ? and channel = 'dine_in' and service = 'dinner' "
                        "and effective_from <= ? order by effective_from desc limit 1", (item, str(as_of))).fetchone()
    if r is None:
        raise ValueError(f"price_changes_cents: {item!r} has no dine-in dinner price in menu_prices")
    return int(r[0])


def scenario_from_contract(sc, prep):
    """contracts.Scenario -> the scenario dict apply_scenario() reads.
    A new dinner price moves the lunch and delivery prices by the same percentage (the menus are priced off dinner)."""
    first = prep["cal"]["dates"][0]
    out = {"label": sc.name, "price_response": sc.price_response, "substitution": sc.substitution}
    for field in ("one_off_costs", "one_off_income"):
        if getattr(sc, field):
            out[field] = [{"date": str(o.day or first), "cents": o.cents, "label": o.label} for o in getattr(sc, field)]
    if sc.cover_change:
        out["sales_change"] = dict(sc.cover_change)
    if sc.changes_start:
        out["start"] = str(sc.changes_start)
    if sc.price_changes_cents:
        dm, ch = prep["dishes"], {}
        for item, new in sc.price_changes_cents.items():
            if item not in dm.index:
                raise ValueError(f"price_changes_cents: unknown dish {item!r}; dishes: {sorted(dm.index)}")
            old = _dinner_price(item, prep["h"]["as_of"])
            ch[item] = int(round(dm.loc[item].avg_price * (new / old - 1)))
        out["price_changes"] = ch
    if sc.remove_items:
        out["remove_items"] = list(sc.remove_items)
    if sc.supplier_stretch_days:
        out["stretch_payments"] = dict(sc.supplier_stretch_days)
    if sc.fix_findings:
        ongoing = {it["finding"]: it for it in prep["items"] if it["kind"] == "ongoing"}
        for fid in sc.fix_findings:
            if fid not in ongoing:
                raise ValueError(f"fix_findings: {fid!r} is not an ongoing overcharge in the adjustments or report "
                                 f"(ongoing: {sorted(ongoing) or 'none'})")
            if not ongoing[fid].get("recoverable"):
                raise ValueError(f"fix_findings: {fid!r} is not recoverable (or is still waiting for the owner)")
        out["apply_fixes"] = list(sc.fix_findings)
    return out


def run_forecast(scenario=None, adjustments=None, settings=None, *, report_id=None, as_of=None, save=True):
    """contracts.py entry point: run_forecast(Scenario, list[Adjustment] | None, ForecastSettings | None) -> ForecastResult.
    The full result (comparison with the base, effects, assumptions, monthly bands) is saved as JSON next to the chart;
    use forecast_detail() to get it as a dict."""
    st = settings if isinstance(settings, K.ForecastSettings) else K.ForecastSettings.model_validate(settings or {})
    sc = scenario if isinstance(scenario, (K.Scenario, type(None))) else K.Scenario.model_validate(scenario)
    d = forecast_detail(sc, report_id=report_id, as_of=as_of or st.as_of, save=save, adjustments=adjustments,
                        settings=dict(trials=st.trials, horizon_months=st.horizon_months, seed=st.seed))
    return to_forecast_result(d)


def to_forecast_result(d):
    """forecast_detail() dict -> contracts.ForecastResult."""
    return K.ForecastResult(
        scenario=(d["scenario"] or {}).get("label") or "base", as_of=date.fromisoformat(d["as_of"]),
        starting_cash_cents=d["cash_today_cents"],
        run_out_worst=d["runout"]["p10"], run_out_likely=d["runout"]["p50"], run_out_best=d["runout"]["p90"],
        share_running_out=round(1 - d["runout"]["never_within_horizon_pct"], 3),
        safety_floor_cents=d["below_floor"]["floor_cents"], below_floor_likely=d["below_floor"]["p50"],
        trials=d["settings"]["trials"], horizon_months=d["settings"]["horizon_months"], seed=d["settings"]["seed"],
        forecast_id=d["forecast_id"], assumptions=d["assumptions"],
        monthly_bands=[K.MonthBand(month=b["month"], worst_cents=b["p10_cents"], likely_cents=b["p50_cents"],
                                   best_cents=b["p90_cents"]) for b in d["monthly_bands"]],
        vs_base=None if not d["vs_base"] else K.VsBase(
            base_forecast_id=d["vs_base"].get("base_forecast_id"),
            days_moved_worst=d["vs_base"]["runout_days"]["p10"], days_moved_likely=d["vs_base"]["runout_days"]["p50"],
            days_moved_best=d["vs_base"]["runout_days"]["p90"], days_moved_average=d["vs_base"].get("runout_mean_days_change"),
            cash_change_cents=d["vs_base"].get("cash_change_on_base_likely_runout_cents"),
            floor_days_moved_likely=d["vs_base"]["below_floor_days"]["p50"]),
        chart_path=d["chart_png"])


def forecast_detail(scenario=None, report_id=None, as_of=None, settings=None, save=True, adjustments=None):
    """Everything the Forecaster knows about one run (dict). `scenario`: a contracts.Scenario or the richer dict apply_scenario()
    reads. With a scenario, the base runs too, with the same random draws, and `vs_base` says how far the dates move."""
    S = {**DEFAULTS, **(settings or {})}
    t0 = time.perf_counter()
    prep = prepare(as_of, report_id, S, adjustments)
    h, P, cal = prep["h"], prep["P"], prep["cal"]
    base_paths, base = _run(prep, S)
    paths, summ, effects, notes, vs = base_paths, base, [], [], None
    if isinstance(scenario, K.Scenario):
        scenario = scenario_from_contract(scenario, prep) if scenario != K.Scenario(name=scenario.name) else None
    if scenario:
        adj, effects, notes = apply_scenario(prep, scenario)
        paths, summ = _run(prep, S, adj)
        vs = compare(base, summ, base_paths, paths, cal["dates"])
    result = dict(
        forecast_id=None, schema_version="1.1", generated_at=datetime.now().astimezone().isoformat(timespec="seconds"),
        as_of=str(h["as_of"]), report_id=(h["report"] or {}).get("report_id"), report_note=h["report_note"],
        bank_source=h["bank_source"], scenario=scenario or None, scenario_effects=effects,
        cash_today_cents=int(h["cash_today"]), **summ, vs_base=vs, chart_png=None,
        assumptions=_assumptions(prep, S) + notes,
        upcoming_fixed=[{k: v for k, v in u.items() if k != "payee"} for u in cal["upcoming"] if u["kind"] != "ongoing"][:15],
        patterns=dict(card_ratio=round(P["card_ratio"], 4), delivery_ratio=round(P["dlv_ratio"], 4),
                      food_ratio=round(P["food_ratio"], 4), food_lag_days=P["food_lag_days"],
                      price_drift_per_month=round(P["price_drift"], 5), tax_rate=round(P["tax_rate"], 5),
                      payroll_fixed_cents=int(P["payroll"]["a"]), payroll_per_sales=round(P["payroll"]["b"], 4),
                      repairs_per_month=round(P["repairs"]["per_day"] * DAYS_PER_MONTH, 2),
                      locations={k: dict(trend=round(v["trend"], 4), trend_se=round(v["trend_se"], 4), noise_sd=round(v["sd"], 4),
                                         noise_phi=round(v["phi"], 3), delivery_share=round(v["delivery_share"], 4))
                                 for k, v in P["locations"].items()}),
        settings={"trials": 1 if S["deterministic"] else S["trials"], "horizon_days": len(cal["dates"]),
                  "horizon_months": S["horizon_months"] if not S["horizon_days"] else round(len(cal["dates"]) / DAYS_PER_MONTH),
                  "seed": S["seed"], "deterministic": S["deterministic"]},
        runtime_s=None)
    result["runtime_s"] = round(time.perf_counter() - t0, 2)
    return save_forecast(result, paths, base_paths if scenario else None, chart=S.get("chart", True)) if save else result


# ---------------------------------------------------------------- 10. backtest
def backtest(cutoff=None, report_id="none", settings=None, save=True):
    """Pretend it's `cutoff` (default config.TRAIN_END): forecast with only the data known then, and score it on what happened.

    sales   daily and weekly MAPE of expected sales vs actual POS sales, against a flat weekday-average baseline
    cash    actual end-of-day bank balance vs the forecast: error of the likely path, share of days inside the 10-90% band
    """
    S = {**DEFAULTS, **(settings or {})}
    cutoff = date.fromisoformat(str(cutoff or C.TRAIN_END))
    prep = prepare(cutoff, report_id, S)
    paths, summ = _run(prep, S)
    full = load_history(None, "none")
    end, dates = full["as_of"], prep["cal"]["dates"]
    n = min((end - cutoff).days, len(dates))
    test_days = dates[:n]
    # sales: what the model expected (trend included, no noise) vs what was sold
    exp = sum(M["_expected"][:n] * np.exp(M["trend"] * M["_years"][:n]) for M in prep["P"]["locations"].values())
    act = full["sales"].groupby("date").revenue.sum().reindex(test_days).fillna(0).values
    hist = prep["h"]["sales"].groupby("date").revenue.sum()
    recent = hist[hist.index > cutoff - timedelta(56)]
    flat_dow = pd.Series(recent.values, index=[d.weekday() for d in recent.index]).groupby(level=0).mean()
    flat = np.array([flat_dow.get(d.weekday(), 0.0) for d in test_days])
    ok = act > 0

    def mape(pred, a):
        return round(float(np.mean(np.abs(pred - a) / a)), 4)

    wk = pd.Series(range(n), index=pd.to_datetime(test_days)).index.to_period("W")
    weekly = lambda v: pd.Series(v, index=wk).groupby(level=0).sum().iloc[1:-1]       # full weeks only
    # cash: the real balance each day vs the forecast band
    b = full["bank"]
    opening = int(full["books"]["accounts"].opening_balance_cents.sum())
    daily = b[b.category != "Internal"].groupby("date").signed.sum().reindex(
        [cutoff + timedelta(i) for i in range(-400, n + 1)], fill_value=0)
    bal = opening + b[(b.category != "Internal") & (b.date <= cutoff - timedelta(401))].signed.sum() + daily.cumsum()
    actual_bal = bal.reindex(test_days)
    p10, p50, p90 = np.percentile(paths[:, :n], [10, 50, 90], axis=0)
    inside = (actual_bal.values >= p10) & (actual_bal.values <= p90)
    res = dict(
        kind="backtest", cutoff=str(cutoff), scored_through=str(test_days[-1]), days=n, report_note=prep["h"]["report_note"],
        bank_source=prep["h"]["bank_source"],
        sales=dict(daily_mape=mape(exp[ok], act[ok]), weekly_mape=mape(weekly(exp).values, weekly(act).values),
                   baseline_flat_weekday_daily_mape=mape(flat[ok], act[ok]),
                   baseline_flat_weekday_weekly_mape=mape(weekly(flat).values, weekly(act).values),
                   total_expected_cents=int(exp.sum()), total_actual_cents=int(act.sum()),
                   total_error_pct=round(float(exp.sum() / act.sum() - 1), 4)),
        cash=dict(start_cents=int(prep["h"]["cash_today"]), actual_end_cents=int(actual_bal.iloc[-1]),
                  forecast_p50_end_cents=int(p50[-1]), forecast_p10_end_cents=int(p10[-1]), forecast_p90_end_cents=int(p90[-1]),
                  end_error_pct=round(float(p50[-1] / actual_bal.iloc[-1] - 1), 4),
                  mean_abs_error_cents=int(np.mean(np.abs(p50 - actual_bal.values))),
                  share_of_days_inside_band=round(float(inside.mean()), 3)),
        forecast_from_cutoff=dict(runout=summ["runout"], below_floor=summ["below_floor"]), chart_png=None)
    if save:
        FORECASTS.mkdir(parents=True, exist_ok=True)
        stem = FORECASTS / f"BT-{cutoff}"
        res["chart_png"] = make_chart(paths[:, :n], test_days, _floor(prep), stem.with_suffix(".png"), actual=actual_bal,
                                      title=f"Backtest: forecast from {cutoff} vs actual balance")
        stem.with_suffix(".json").write_text(json.dumps(res, indent=1, default=str))
    return res


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--as-of")
    ap.add_argument("--report", help="report_id, or 'none'")
    ap.add_argument("--trials", type=int, default=DEFAULTS["trials"])
    ap.add_argument("--deterministic", action="store_true")
    ap.add_argument("--no-save", action="store_true")
    ap.add_argument("--scenario", help="JSON string or path to a .json file (see apply_scenario)")
    ap.add_argument("--backtest", nargs="?", const=str(C.TRAIN_END), metavar="CUTOFF")
    a = ap.parse_args()
    S = dict(trials=a.trials, deterministic=a.deterministic)
    if a.backtest:
        r = backtest(a.backtest, report_id=a.report or "none", settings=S, save=not a.no_save)
        s, c = r["sales"], r["cash"]
        print(f"backtest from {r['cutoff']} scored through {r['scored_through']} ({r['days']} days) | bank: {r['bank_source']}")
        print(f"sales  daily MAPE {s['daily_mape']:.1%} (flat weekday baseline {s['baseline_flat_weekday_daily_mape']:.1%}) | "
              f"weekly {s['weekly_mape']:.1%} (baseline {s['baseline_flat_weekday_weekly_mape']:.1%}) | total {s['total_error_pct']:+.1%}")
        print(f"cash   end: actual ${c['actual_end_cents'] / 100:,.0f} vs likely ${c['forecast_p50_end_cents'] / 100:,.0f} "
              f"[{c['forecast_p10_end_cents'] / 100:,.0f}..{c['forecast_p90_end_cents'] / 100:,.0f}] ({c['end_error_pct']:+.1%}) | "
              f"days inside 10-90% band {c['share_of_days_inside_band']:.0%}")
        print("chart:", r["chart_png"])
        raise SystemExit
    scen = None
    if a.scenario:
        scen = json.loads(Path(a.scenario).read_text()) if a.scenario.endswith(".json") else json.loads(a.scenario)
        if set(scen) <= set(K.Scenario.model_fields):          # the contract's Scenario, else the richer what-if dict
            scen = K.Scenario.model_validate(scen)
    r = forecast_detail(scen, report_id=a.report, as_of=a.as_of, save=not a.no_save, settings=S)
    print(f"{r['forecast_id'] or '(not saved)'} | as of {r['as_of']} | cash ${r['cash_today_cents'] / 100:,.0f} | "
          f"{r['settings']['trials']} trials in {r['runtime_s']}s")
    print(f"bank: {r['bank_source']}\nreport: {r['report_note']}")
    ro, bf = r["runout"], r["below_floor"]
    print(f"$0:      bad {ro['p10']} | likely {ro['p50']} | good {ro['p90']} | never within horizon {ro['never_within_horizon_pct']:.0%}")
    print(f"floor ${bf['floor_cents'] / 100:,.0f}: bad {bf['p10']} | likely {bf['p50']} | good {bf['p90']}")
    print("P(out of cash by):", r["prob_runout_by"])
    if r["vs_base"]:
        v = r["vs_base"]
        fmt = lambda x: "beyond horizon" if x is None else f"{x:+} days"
        rd = v["runout_days"]
        print(f"vs base: $0 likely {v['base_runout']['p50']} -> {ro['p50']} ({fmt(rd['p50'])}) | bad {fmt(rd['p10'])} | "
              f"good {fmt(rd['p90'])}")
        for e in r["scenario_effects"]:
            print("  effect:", {k: v for k, v in e.items()})
    print("assumptions:\n  " + "\n  ".join(r["assumptions"]))
    if r["chart_png"]:
        print("chart:", r["chart_png"])
