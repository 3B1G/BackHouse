"""Feature tables for the agents -> data/features.db (v2).

Uses ONLY books.db and bank_feed.csv (bank-visible columns, i.e. what Nessie will hold).
Never reads data/grader/. Bank rows are categorized from description + payee, as a bookkeeper would.
"""
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd

import config as C

D = Path(__file__).parent / "data"
TABLES = ["tickets", "ticket_lines", "daily_ops", "invoices", "invoice_lines", "menu_prices", "recipes", "suppliers",
          "open_ap", "accounts", "vendor_bills", "leases", "repair_invoices", "misc_payables"]


def load():
    con = sqlite3.connect(D / "books.db")
    t = {n: pd.read_sql(f"select * from {n}", con) for n in TABLES}
    con.close()
    for df in t.values():
        for c in [c for c in df.columns if c.endswith("date") or c in ("date", "due", "effective_from")]:
            df[c] = pd.to_datetime(df[c])
    bank = pd.read_csv(D / "bank_feed.csv", parse_dates=["date"])
    return t, bank


def categorize(bank, t):
    """Bank row -> category using only the description, payee and the books' vendor lists."""
    sup = dict(zip(t["suppliers"].supplier, t["suppliers"].category))
    vb = dict(zip(t["vendor_bills"].vendor, t["vendor_bills"].category))
    landlords = set(t["leases"].landlord)
    repair = set(t["repair_invoices"].vendor)
    misc = set(t["misc_payables"].payee)
    memo, cp = bank.memo.fillna(""), bank.counterparty.fillna("")
    rules = [
        (memo.str.startswith("ONLINE XFER"), "Internal"),
        (memo.str.startswith("TABLETAP PAYMENTS DEP"), "Sales - Card"),
        (memo.str.startswith("BRANCH DEPOSIT"), "Sales - Cash"),
        (memo.str.contains("PAYOUT"), "Sales - Delivery"),
        (memo.str.contains(" CREDIT CM-"), "Supplier Credit"),
        (memo.str.startswith("BAYPAY PAYROLL"), "Labor"),
        (memo.str.startswith("CDTFA"), "Sales Tax"),
        (memo.str.contains("LOAN PMT"), "Debt Service"),
        (memo.str.startswith("OWNER DRAW"), "Owner Draw"),
        (memo.str.contains("CHARGEBACK"), "Chargeback"),
        (cp.isin(landlords), "Occupancy"),
        (cp.isin(list(sup)), cp.map(sup)),
        (cp.isin(list(vb)), cp.map(vb)),
        (cp.isin(repair), "Repairs & Maintenance"),
        (cp.isin(misc), "G&A"),
    ]
    cat = pd.Series("Uncategorized", index=bank.index, dtype=object)
    done = pd.Series(False, index=bank.index)
    for m, v in rules:
        sel = m & ~done
        cat[sel] = v[sel] if isinstance(v, pd.Series) else v
        done |= m
    return bank.assign(category=cat, signed=np.where(bank.direction == "in", bank.amount, -bank.amount))


def daily_sales(t):
    tk = t["tickets"][t["tickets"].date >= pd.Timestamp(C.START)]
    g = tk.groupby(["location", "date"]).agg(revenue=("subtotal", "sum"), tickets=("ticket_id", "count"),
                                             covers=("party_size", "sum"), discounts=("discount", "sum"))
    g["delivery_rev"] = tk[~tk.channel.isin(["dine_in", "private_event"])].groupby(["location", "date"]).subtotal.sum()
    ops = t["daily_ops"].set_index(["location", "date"])[["open", "rain", "event", "actual_hours", "scheduled_hours", "private_event_guests"]]
    f = ops.join(g).fillna({"revenue": 0, "tickets": 0, "covers": 0, "delivery_rev": 0, "discounts": 0}).reset_index()
    f = f[f.date >= pd.Timestamp(C.START)].sort_values(["location", "date"])
    f["avg_check"] = (f.revenue / f.covers.replace(0, np.nan)).round(0)
    f["delivery_share"] = (f.delivery_rev / f.revenue.replace(0, np.nan)).round(3)
    f["rev_per_labor_hr"] = (f.revenue / 100 / f.actual_hours.replace(0, np.nan)).round(2)
    f["dow"], f["month"] = f.date.dt.dayofweek, f.date.dt.month
    f["is_peak_night"] = f.dow.isin([4, 5]).astype(int)
    f["has_event"] = f.event.notna().astype(int)
    for k in (7, 14):
        f[f"rev_lag{k}"] = f.groupby("location").revenue.shift(k)
    for k in (7, 28):
        f[f"rev_roll{k}"] = f.groupby("location").revenue.transform(lambda s: s.shift(1).rolling(k, min_periods=k).mean()).round(0)
    f["split"] = np.where(f.date <= pd.Timestamp(C.TRAIN_END), "train", "test")
    return f


def cash_daily(b, t):
    idx = pd.date_range(C.START, C.END)
    ext = b[b.category != "Internal"]
    piv = ext.pivot_table(index="date", columns="category", values="signed", aggfunc="sum", fill_value=0).reindex(idx, fill_value=0)
    piv.columns = ["flow_" + c.lower().replace(" & ", "_").replace(" - ", "_").replace(" ", "_") for c in piv.columns]
    f = piv.copy()
    f["inflow"] = ext[ext.signed > 0].groupby("date").signed.sum().reindex(idx, fill_value=0)
    f["outflow"] = -ext[ext.signed < 0].groupby("date").signed.sum().reindex(idx, fill_value=0)
    f["net"] = f.inflow - f.outflow
    opening = t["accounts"].set_index("account").opening_balance_cents
    f["balance_total"] = opening.sum() + f.net.cumsum()       # Nessie balance is static: derive it
    for a in opening.index:
        f[f"balance_{a}"] = opening[a] + b[b.account == a].groupby("date").signed.sum().reindex(idx, fill_value=0).cumsum()
    f["net_roll30"] = f.net.rolling(30, min_periods=30).sum()
    f["runway_days_naive"] = np.where(f.net_roll30 < 0, (f.balance_total / (-f.net_roll30 / 30)).round(0), np.nan)
    f["split"] = np.where(f.index <= pd.Timestamp(C.TRAIN_END), "train", "test")
    return f.rename_axis("date").reset_index()


def ingredient_month(t):
    il = t["invoice_lines"].merge(t["invoices"][["invoice_no", "invoice_date"]], on="invoice_no")
    il["month"] = il.invoice_date.dt.to_period("M").astype(str)
    g = (il.groupby(["ingredient", "month"]).apply(lambda x: np.average(x.unit_price, weights=x.qty), include_groups=False)
         .rename("avg_unit_price").reset_index())
    g["mom_change"] = g.groupby("ingredient").avg_unit_price.pct_change().round(4)
    g["index_vs_first"] = (g.avg_unit_price / g.groupby("ingredient").avg_unit_price.transform("first")).round(3)
    return g


def item_month(t, ing):
    tk = t["tickets"][t["tickets"].date >= pd.Timestamp(C.START)][["ticket_id", "date", "channel"]]
    tl = t["ticket_lines"].merge(tk, on="ticket_id")
    tl["month"] = tl.date.dt.to_period("M").astype(str)
    tl["rev"] = tl.qty * tl.unit_price
    tl["dlv"] = ~tl.channel.isin(["dine_in", "private_event"])
    u = tl.groupby(["item", "month"]).agg(units=("qty", "sum"), revenue=("rev", "sum")).reset_index()
    u = u.merge(tl[tl.dlv].groupby(["item", "month"]).agg(dlv_units=("qty", "sum"), dlv_rev=("rev", "sum")).reset_index(),
                how="left").fillna({"dlv_units": 0, "dlv_rev": 0})
    u["avg_price"] = (u.revenue / u.units).round(0)            # realized: mixes lunch, dinner, delivery menus
    course = t["menu_prices"].drop_duplicates("item").set_index("item").course
    u["course"] = u.item.map(course)
    price = ing.pivot(index="month", columns="ingredient", values="avg_unit_price").ffill()
    rec = t["recipes"]
    pc = [dict(item=i, month=m, plate_cost=round(sum(q * price.loc[m, g] for g, q in zip(r.ingredient, r.qty)) * C.USAGE_VARIANCE * 100))
          for m in price.index for i, r in rec.groupby("item")]
    f = u.merge(pd.DataFrame(pc), on=["item", "month"])
    f["food_cost_pct"] = (f.plate_cost / f.avg_price).round(3)
    din_rev = f.revenue - f.dlv_rev
    f["fees_per_plate"] = ((din_rev * C.CARD_SHARE_DINEIN * (1 + C.SALES_TAX + 0.20) * C.CARD_PCT
                            + f.dlv_rev * C.DELIVERY_COMMISSION) / f.units).round(0)
    f["cm_per_plate"] = f.avg_price - f.plate_cost - f.fees_per_plate
    f["cm_total"] = f.cm_per_plate * f.units
    out = []
    for (m, crs), g in f.groupby(["month", "course"]):
        g = g.copy()
        g["mix_share"] = (g.units / g.units.sum()).round(4)
        if crs in ("main", "starter", "dessert", "drink"):
            avg_cm = np.average(g.cm_per_plate, weights=g.units)
            pop, hi = g.mix_share >= 0.7 / len(g), g.cm_per_plate >= avg_cm     # Kasavana-Smith
            g["menu_class"] = np.select([pop & hi, pop & ~hi, ~pop & hi], ["star", "plowhorse", "puzzle"], "dog")
        else:
            g["menu_class"] = None
        out.append(g)
    f = pd.concat(out)
    f["loses_money"] = (f.cm_per_plate < 0).astype(int)
    f["competitor_median_price"] = np.nan                     # filled by the Market scout
    f["price_gap"] = np.nan
    return f.drop(columns=["dlv_units", "dlv_rev"])


def supplier(t, as_of=C.END):
    inv = t["invoices"][(t["invoices"].invoice_date >= pd.Timestamp(C.START)) & (t["invoices"].invoice_date <= pd.Timestamp(as_of))]
    g = inv.groupby(["supplier", "location"]).agg(spend=("total", "sum"), invoices=("invoice_no", "count"),
                                                   avg_invoice=("total", "mean"), terms_days=("terms_days", "first")).reset_index()
    months = (as_of - C.START).days / 30.44
    g["invoices_per_month"] = (g.invoices / months).round(1)
    g["monthly_spend"] = (g.spend / months).round(0)
    # open AP as of `as_of`: received by then, not yet due (approximates payment on the due date)
    allinv = t["invoices"]
    oa = allinv[(allinv.invoice_date <= pd.Timestamp(as_of)) & (allinv.due_date > pd.Timestamp(as_of))]
    ap = oa.groupby(["supplier", "location"]).total.sum().rename("open_ap").reset_index()
    g = g.merge(ap, how="left").fillna({"open_ap": 0})
    # assumption shown to the owner: extra days each kind of supplier tolerates (Controller's action is +15 days)
    g["stretch_days_available"] = g.supplier.map(lambda s: C.STRETCH_TOLERANCE_DAYS[C.SUPPLIERS[s][0]])
    g["one_time_cash_freed"] = (g.monthly_spend * g.stretch_days_available / 30.44).round(0)
    return g


def recurring(b, as_of=C.END):
    b = b[b.date <= pd.Timestamp(as_of)]
    r = b[(b.direction == "out") & ~b.category.isin(["Internal", "Uncategorized"]) & ~b.category.str.startswith("Food Supply") & (b.category != "Beverage")].copy()
    r["dom"] = r.date.dt.day
    g = r.groupby(["account", "category", r.counterparty.fillna(r.memo.str.split().str[:2].str.join(" "))]).agg(
        n=("amount", "count"), mean=("amount", "mean"), std=("amount", "std"), median_dom=("dom", "median"), last=("date", "max")).reset_index()
    g = g.rename(columns={"counterparty": "payee"})
    key = ["account", "category", r.counterparty.fillna(r.memo.str.split().str[:2].str.join(" "))]
    g["median_gap_days"] = r.sort_values("date").groupby(key).date.apply(lambda s: s.diff().dt.days.median()).values
    g["cv"] = (g["std"] / g["mean"]).round(3)
    g["cadence"] = np.select([g.median_gap_days.between(12, 16), g.median_gap_days.between(26, 35)], ["biweekly", "monthly"], "irregular")
    g[["mean", "std"]] = g[["mean", "std"]].round(0)
    return g


if __name__ == "__main__":
    t, bank = load()
    b = categorize(bank, t)
    unc = b[b.category == "Uncategorized"]
    print(f"categorized {len(b) - len(unc)}/{len(b)} bank rows; uncategorized: {unc.memo.tolist()[:5]}")
    ing = ingredient_month(t)
    tables = {"bank_categorized": b, "feat_daily_sales": daily_sales(t), "feat_cash_daily": cash_daily(b, t),
              "feat_ingredient_month": ing, "feat_item_month": item_month(t, ing), "feat_supplier": supplier(t),
              "feat_recurring": recurring(b),
              # train-only versions for the backtest (no test-period information)
              "feat_supplier_train": supplier(t, C.TRAIN_END), "feat_recurring_train": recurring(b, C.TRAIN_END)}
    (D / "features.db").unlink(missing_ok=True)
    con = sqlite3.connect(D / "features.db")
    for n, df in tables.items():
        df.to_sql(n, con, if_exists="replace", index=False)
        print(f"{n:24} {df.shape}")
    con.close()
