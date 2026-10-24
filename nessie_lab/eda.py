"""Exploratory data analysis -> eda/report.md + eda/*.png (v2). Run after generate.py and features.py.
Reads only agent-visible data (books.db, features.db); never data/grader/.
"""
import sqlite3
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

import config as C

D, OUT = Path(__file__).parent / "data", Path(__file__).parent / "eda"
OUT.mkdir(exist_ok=True)
books, feats = sqlite3.connect(D / "books.db"), sqlite3.connect(D / "features.db")
q = lambda sql, con=books: pd.read_sql(sql, con)
md = ["# Back of House: EDA (SIMULATED data)\n", f"Bank window {C.START} to {C.END}. Backtest trains through {C.TRAIN_END}.\n"]
plt.rcParams.update({"figure.dpi": 110, "axes.spines.top": False, "axes.spines.right": False})
INK, A, B, RED = "#17211e", "#0d6b55", "#b8860b", "#a3261d"
table = lambda df: df.to_markdown(index=False)
section = lambda t, body: md.append(f"\n## {t}\n\n{body}\n")
ym = lambda s: pd.to_datetime(s).dt.to_period("M")

# 1. integrity
bank = q("select * from bank_categorized", feats)
bank["date"] = pd.to_datetime(bank.date)
chk = [
    ("invoice total = sum of lines", q("select count(*) n from invoices i join (select invoice_no, sum(line_total) s from invoice_lines group by 1) l using(invoice_no) where i.total != l.s").n[0] == 0),
    ("invoice line = qty x unit price (+-1c)", q("select count(*) n from invoice_lines where abs(line_total - round(qty*unit_price*100)) > 1").n[0] == 0),
    ("ticket gross = sum of lines", q("select count(*) n from tickets t join (select ticket_id, sum(qty*unit_price) s from ticket_lines group by 1) l using(ticket_id) where t.gross != l.s").n[0] == 0),
    ("subtotal = gross - discount", q("select count(*) n from tickets where subtotal != gross - discount").n[0] == 0),
    ("bank dates inside window", bank.date.between(str(C.START), str(C.END)).all()),
    ("bank amounts positive integers", bool((bank.amount > 0).all())),
    ("bank ids unique", bank.bank_id.is_unique),
    ("every bank row categorized", (bank.category != "Uncategorized").all()),
    ("no payroll dated after window in books", q(f"select count(*) n from payroll_runs where pay_date > '{C.END}'").n[0] == 0),
    ("no grader columns in bank feed", not {"txn_id", "kind", "books_ref", "planted"} & set(pd.read_csv(D / "bank_feed.csv", nrows=1).columns)),
]
cd = q("select * from feat_cash_daily", feats)
mins = {a: int(cd[f"balance_{a}"].min() / 100) for a in C.OPENING_BALANCE}
chk.append(("no negative daily balance", min(mins.values()) > 0))
section("1. Integrity checks", table(pd.DataFrame(chk, columns=["check", "pass"])) + f"\n\nLowest daily balance by account ($): {mins}")

# 2. revenue patterns
ds = q("select * from feat_daily_sales", feats)
ds["date"] = pd.to_datetime(ds.date)
mon = ds.groupby([ym(ds.date), "location"]).revenue.sum().unstack() / 100
fig, ax = plt.subplots(figsize=(9, 3.4))
mon.plot(ax=ax, color=[A, B], marker="o"); ax.set_title("Monthly revenue by location ($, partial first/last months)"); ax.set_xlabel("")
fig.tight_layout(); fig.savefig(OUT / "revenue_monthly.png"); plt.close(fig)
op = ds[ds.open == 1]
tk = q(f"select * from tickets where date >= '{C.START}'")
check = tk[tk.channel == "dine_in"].groupby(["location", "service"]).apply(lambda g: g.subtotal.sum() / g.party_size.sum() / 100, include_groups=False).unstack().round(2)
kpi = op.groupby("location").agg(avg_daily_rev=("revenue", "mean"), covers=("covers", "mean"), delivery_share=("delivery_share", "mean"),
                                 rev_per_labor_hr=("rev_per_labor_hr", "mean")).reset_index()
kpi["avg_daily_rev"] /= 100
kpi["covers_per_seat"] = kpi.apply(lambda r: r.covers / C.LOCATIONS[r.location]["seats"], axis=1)
dow = (op.groupby(["dow", "location"]).revenue.mean().unstack() / 100).round(0)
dow.index = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
corr = op.assign(h=op.actual_hours).groupby("location").apply(lambda g: g.covers.corr(g.h), include_groups=False).round(2).to_dict()
section("2. Revenue and operations", "![](revenue_monthly.png)\n\n" + table(kpi.round(2)) +
        "\n\nDine-in check per cover ($):\n\n" + table(check.reset_index()) +
        "\n\nAverage revenue by weekday ($):\n\n" + table(dow.reset_index(names="dow")) +
        f"\n\nLabor hours vs covers correlation: {corr} (staff are scheduled from a forecast, then cut or called in).")

# 3. P&L (accrual) vs benchmark
win = pd.period_range(C.START, C.END, freq="M")
rev = tk.groupby(ym(tk.date)).subtotal.sum().reindex(win)
inv = q(f"select invoice_date, total from invoices where invoice_date >= '{C.START}'")
cnt = q("select count_date, sum(value_cents) v from inventory_counts group by 1")
d_inv = cnt.set_index(ym(cnt.count_date)).v.diff().reindex(win).fillna(0)
cogs = inv.groupby(ym(inv.invoice_date)).total.sum().reindex(win) - d_inv
pay = q("select * from payroll_runs")
pay_m = pay.assign(m=ym(pay.pay_date)).groupby("m")[["hourly_wages", "salaries", "employer_taxes_benefits", "tips_paid"]].sum().reindex(win)
labor = pay_m.hourly_wages + pay_m.salaries + pay_m.employer_taxes_benefits - pay_m.tips_paid * C.TIP_FICA
occ = pd.Series([sum(L["base"] * (1 + L["escalator_pct"] if p.start_time.date() >= L["anniversary"] else 1) for L in C.LEASES.values()) for p in win], index=win)
ext = bank[bank.category != "Internal"]
net = ext.groupby(ym(ext.date)).signed.sum().reindex(win)
pl = pd.DataFrame({"revenue_$": rev / 100, "cogs_%": cogs / rev, "labor_excl_tips_%": labor / rev, "occupancy_%": occ / rev, "net_cash_$": net / 100})
full = pl.iloc[1:-1]
fy = pd.DataFrame([
    ("food & bev cost (COGS)", full["cogs_%"].mean(), 0.32), ("payroll + benefits (excl. tips)", full["labor_excl_tips_%"].mean(), 0.365),
    ("occupancy", full["occupancy_%"].mean(), 0.06), ("net cash margin", net.sum() / rev.sum(), 0.028)],
    columns=["metric", "ours", "NRA median (full-service)"])
section("3. Monthly P&L (accrual) vs NRA benchmarks",
        table(pl.reset_index(names="month").assign(month=lambda x: x.month.astype(str)).round(3)) +
        "\n\nFull months only:\n\n" + table(fy.round(3)) +
        "\n\nLabor is accrued on pay date; May 2026 has three paydays, so its labor % is a cash-timing spike, not a rate change."
        " First and last months are partial.")

# 4. menu economics
im = q("select * from feat_item_month", feats)
first, last = sorted(im.month.unique())[1], im.month.max()
keep = im[im.month.isin([first, last]) & im.course.isin(["main", "starter", "dessert", "drink", "bottle"])]
mt = keep.pivot_table(index=["item", "course"], columns="month", values=["avg_price", "plate_cost", "cm_per_plate"]).reset_index()
mt.columns = [" ".join(c).strip() for c in mt.columns]
for c in mt.columns[2:]:
    mt[c] = (mt[c] / 100).round(2)
mains = im[(im.month == last) & (im.course == "main")]
fig, ax = plt.subplots(figsize=(7.5, 5))
ax.scatter(mains.mix_share * 100, mains.cm_per_plate / 100, c=[RED if v else A for v in mains.loses_money], s=50)
for r in mains.itertuples():
    ax.annotate(r.item, (r.mix_share * 100, r.cm_per_plate / 100), fontsize=7, xytext=(4, 2), textcoords="offset points")
ax.axvline(70 / len(mains), ls="--", c="grey", lw=.8)
ax.axhline(np.average(mains.cm_per_plate, weights=mains.units) / 100, ls="--", c="grey", lw=.8); ax.axhline(0, c=RED, lw=.8)
ax.set_xlabel("menu mix % (mains)"); ax.set_ylabel("profit per plate after fees ($)"); ax.set_title(f"Menu engineering, mains, {last}")
fig.tight_layout(); fig.savefig(OUT / "menu_engineering.png"); plt.close(fig)
lob = im[im.item == "Lobster Ravioli"][["month", "avg_price", "plate_cost", "cm_per_plate", "units"]].assign(**{c: lambda x, c=c: x[c] / 100 for c in ["avg_price", "plate_cost", "cm_per_plate"]})
cls = im[(im.month == last) & im.menu_class.notna()][["item", "course", "menu_class", "units", "food_cost_pct", "loses_money"]]
section("4. Menu economics", "![](menu_engineering.png)\n\n" + table(mt) + "\n\nLobster Ravioli by month:\n\n" + table(lob.round(2)) +
        "\n\nMenu-engineering classes, last month:\n\n" + table(cls.sort_values(["course", "menu_class"]).round(3)))

# 5. ingredient prices
ig = q("select * from feat_ingredient_month", feats)
fig, ax = plt.subplots(figsize=(9, 3.4))
for g, col in [("lobster", RED), ("olive_oil", B), ("eggs", A), ("parmesan", "grey")]:
    s = ig[ig.ingredient == g]; ax.plot(s.month, s.index_vs_first, marker=".", label=g, color=col)
ax.set_title("Ingredient price index (first month = 1.0)"); ax.legend(frameon=False); ax.tick_params(axis="x", rotation=45)
fig.tight_layout(); fig.savefig(OUT / "ingredient_prices.png"); plt.close(fig)
section("5. Ingredient prices", "![](ingredient_prices.png)\n\n" +
        table(ig.groupby("ingredient").index_vs_first.last().sort_values(ascending=False).head(6).reset_index()))

# 6. cash
cd["date"] = pd.to_datetime(cd.date)
fig, ax = plt.subplots(figsize=(9, 3.4))
ax.plot(cd.date, cd.balance_total / 100, color=INK, label="total")
for a, col in zip(C.OPENING_BALANCE, [A, B, "grey"]):
    ax.plot(cd.date, cd[f"balance_{a}"] / 100, color=col, lw=.9, label=a)
ax.axvline(pd.Timestamp(C.TRAIN_END), ls="--", c="grey", lw=.8)
ax.set_title("Bank balance, derived from opening + transactions ($)"); ax.legend(frameon=False, ncol=4)
fig.tight_layout(); fig.savefig(OUT / "cash_balance.png"); plt.close(fig)
section("6. Cash", "![](cash_balance.png)\n\n" + f"Cash on {C.END}: **${cd.balance_total.iloc[-1] / 100:,.0f}**. "
        f"Net cash over the window: **${net.sum() / 100:,.0f}**. See scenario_check.py for the forward view.")

# 7. bank feed composition
section("7. Bank feed by Nessie type and category",
        table(bank.groupby(["nessie_type", "category"]).agg(rows=("bank_id", "count"), total_usd=("amount", lambda s: round(s.sum() / 100))).reset_index()))
(OUT / "report.md").write_text("\n".join(md), encoding="utf-8")
print("\n".join(md[:3]) + "\n".join(m for m in md if m.startswith("\n## 1.") or m.startswith("\n## 3.")))
