"""Does the data support the demo story? Deterministic seasonal replay (the Forecaster's Monte Carlo replaces this).

Daily path. "Below floor" = first day cash < one payroll cycle + one month of rent (can't make payroll); also reports $0.
"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import config as C

D = Path(__file__).parent / "data"
bank = pd.read_csv(D / "bank_feed.csv", parse_dates=["date"])
con = sqlite3.connect(D / "books.db")
pay = pd.read_sql("select * from payroll_runs", con, parse_dates=["pay_date"])
tl = pd.read_sql("select l.item, l.qty, l.unit_price, t.date, t.channel from ticket_lines l join tickets t using(ticket_id)", con, parse_dates=["date"])
im = pd.read_sql("select * from feat_item_month", sqlite3.connect(D / "features.db"))
tk = pd.read_sql("select date, subtotal from tickets", con, parse_dates=["date"])

one_off = bank.memo.str.startswith("BAY KITCHEN REPAIR") & (bank.amount > 2_000_00)       # e.g. the oven board: don't replay
own_calendar = bank.memo.str.startswith(("ONLINE XFER", "BAYPAY PAYROLL", "CDTFA")) | bank.memo.str.contains(" RENT ")
ext = bank[~own_calendar & ~one_off]          # payroll, sales tax and rent are projected on their own calendars below
signed = np.where(ext.direction == "in", ext.amount, -ext.amount)
# calendar-month totals; the two partial Octobers (Oct 25-31 2025 + Oct 1-24 2026) add up to one full October
net_m = pd.Series(signed, index=ext.index).groupby(ext.date.dt.month).sum()
tw = tk[tk.date >= pd.Timestamp(C.START)]
rev_m = tw.groupby(tw.date.dt.month).subtotal.sum()
all_signed = np.where(bank.direction == "in", bank.amount, -bank.amount)
cash = sum(C.OPENING_BALANCE.values()) + all_signed[~bank.memo.str.startswith("ONLINE XFER")].sum()
runs = pay.groupby("pay_date").total_debit.sum()
run_amt = runs.tail(26).mean()                   # a full year of runs: no seasonal bias
pay_dates = pd.date_range(runs.index.max() + pd.Timedelta(days=14), periods=45, freq="14D")
# stub: rest of October (Oct 25-31) before the first full projected month
oct_stub = by_month_stub = None
floor = int(pay.groupby("pay_date").total_debit.sum().tail(6).mean()
            + sum(v["base"] * (1 + v["escalator_pct"]) for v in C.LEASES.values()))
CM_RATE = 0.62                                   # margin on lost revenue: food ~32% + card/commission ~6%; labor handled by payroll
by_month = {m: (net_m[m], rev_m[m]) for m in range(1, 13)}


returns = pd.read_sql("select * from sales_tax_returns", con)
tax_by_period = dict(zip(returns.period, returns.tax_due))


def bday(d):
    while d.weekday() >= 5:
        d += pd.Timedelta(days=1)
    return d


tax_dates = {}
for p in pd.period_range(pd.Period(C.END, "M"), periods=16, freq="M"):
    prev = p - 1
    amt = tax_by_period.get(str(prev)) or tax_by_period.get(str(prev - 12), 0)   # last year's same month if not filed yet
    tax_dates[bday(pd.Timestamp(p.year, p.month, C.SALES_TAX_REMIT_DAY))] = amt


def rent_on(d):
    """Monthly rent incl. NNN on day d, with the escalator compounding every anniversary."""
    total = 0
    for L in C.LEASES.values():
        a, steps = pd.Timestamp(L["anniversary"]), 0
        while a <= d:
            steps += 1
            a += pd.DateOffset(years=1)
        total += L["base"] * (1 + L["escalator_pct"]) ** steps
    return total


def project(adjust=lambda p, net: net, days=450):
    """Daily path: payroll on its real dates; all other flows spread evenly over each month's days."""
    bal, out = cash, []
    for d in pd.date_range(pd.Timestamp(C.END) + pd.Timedelta(days=1), periods=days):
        p = d.to_period("M")
        net, rev = by_month[p.month]
        net -= -C.COVER_TREND_PER_YEAR * rev * CM_RATE        # the cover decline continues
        bal += adjust(p, net) / p.days_in_month - (d in pay_dates) * run_amt - tax_dates.get(d, 0) - (rent_on(d) if d == bday(d.replace(day=1)) else 0)
        out.append((d, bal))
    return out


def runout(path, level):
    return next((d.date() for d, b in path if b < level), None)


jan_rev = by_month[1][1]
shock = lambda p, net: net - (4_800_00 if p == pd.Period(C.END, "M") + 1 else 0) - (0.15 * jan_rev * CM_RATE if p.month == 1 else 0)
last = im[im.month == sorted(im.month.unique())[-2]].set_index("item")      # last FULL month
fix_items = {"Margherita Pizza": 200, "Rigatoni alla Vodka": 200}             # +$2.00 each
reprice = sum(last.loc[i, "units"] * c * 0.95 for i, c in fix_items.items())  # assume 5% unit loss
lob = last.loc["Lobster Ravioli"]
lobster_fix = -lob.cm_per_plate * lob.units if lob.cm_per_plate < 0 else 0     # stop selling at a loss (re-cost or drop)
fees = 0.003 * bank[(bank.account == "CHS") & bank.memo.str.startswith("TABLETAP PAYMENTS")].amount.tail(30).sum() / 30 * 30.4
monthly_fix = reprice + lobster_fix + fees
fixes = lambda p, net: shock(p, net) + monthly_fix
paths = {"base": project(), "shock": project(shock), "shock + fixes": project(fixes)}
print(f"cash at {C.END}: ${cash / 100:,.0f} | floor (payroll cycle + rent): ${floor / 100:,.0f}")
print(f"monthly fix value: reprice ${reprice / 100:,.0f} + lobster ${lobster_fix / 100:,.0f} + card fee ${fees / 100:,.0f} = ${monthly_fix / 100:,.0f}/mo")
print(f"lobster ravioli last month: cm/plate ${lob.cm_per_plate / 100:.2f}, units {int(lob.units)}")
for k, pth in paths.items():
    low = min(pth, key=lambda x: x[1])
    print(f"{k:14} below floor: {runout(pth, floor)} | below $0: {runout(pth, 0)} | low point {low[0].date()} ${low[1] / 100:,.0f}")
