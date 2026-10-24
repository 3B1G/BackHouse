"""Generate a SIMULATED two-location restaurant (v2).

Agent-visible (data/):
  books.db       source documents a real restaurant keeps: POS tickets, invoices, inventory counts,
                 payroll, vendor bills, leases, agreements, logs. No pre-built "expected bank" table.
  bank_feed.csv  bank-visible columns only; this is what gets loaded into Nessie.
Grader-only (data/grader/): answer_key.json, ledger_expected.csv, bank_truth.csv
"""
import hashlib, json, math, sqlite3
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

import config as C

rng = np.random.default_rng(C.SEED)
OUT = Path(__file__).parent / "data"
GR = OUT / "grader"
GR.mkdir(parents=True, exist_ok=True)
SIM_START = C.START - timedelta(C.WARMUP_DAYS)
DAYS = [SIM_START + timedelta(n) for n in range((C.END - SIM_START).days + 1)]
BANK_HOLIDAYS = {date(2025, 11, 11), date(2025, 11, 27), date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 19),
                 date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19), date(2026, 9, 7),   # Jul 4 2026 is a Saturday: Fed open Fri
                 date(2026, 10, 12)}
by_course = defaultdict(list)
for _i, _v in C.MENU.items():
    by_course[_v[0]].append(_i)
MAINS, STARTERS, DESSERTS, DRINKS = by_course["main"], by_course["starter"], by_course["dessert"], by_course["drink"]
W = {c: np.array([C.MENU[i][2] for i in by_course[c]]) / sum(C.MENU[i][2] for i in by_course[c])
     for c in ("main", "starter", "dessert")}


def bday(d):
    return d.weekday() < 5 and d not in BANK_HOLIDAYS


def next_bday(d):
    while not bday(d):
        d += timedelta(1)
    return d


def prev_bday(d):
    while not bday(d):
        d -= timedelta(1)
    return d


def step(table, d):
    return table[max(k for k in table if k <= d)]


def cents(x):
    return int(round(x))


def is_open(loc, d):
    return d not in C.CLOSED_DATES and d.weekday() not in C.LOCATIONS[loc]["closed_dow"]


# ---------------------------------------------------------------- prices
weeks = (C.END - SIM_START).days // 7 + 2
PNOISE = {}
for g in C.INGREDIENTS:
    x, s = 0.0, []
    for _ in range(weeks):
        x = C.PRICE_AR * x + rng.normal(0, C.PRICE_SIGMA)
        s.append(x)
    PNOISE[g] = s


def ing_price(g, d):
    unit, base, _, drift, steps, _ = C.INGREDIENTS[g]
    p = (1 if g in C.DELIVERED_EXEMPT else C.DELIVERED_PRICE_FACTOR) * base * (1 + drift) ** ((d - C.START).days / 30.44) * math.exp(PNOISE[g][(d - SIM_START).days // 7])
    for sd, m in steps.items():
        if d >= sd:
            p *= m
    return p


def menu_price(item, d, channel, service):
    p = C.MENU[item][1]
    if d >= C.MENU_REPRICE[0]:
        p = C.MENU_REPRICE[1].get(item, p)
    if channel != "dine_in":
        p = round(p * C.DELIVERY_MARKUP * 2) / 2
    elif service == "lunch" and C.MENU[item][0] == "main":
        p = round(p * C.LUNCH_MAIN_PRICE * 2) / 2
    return cents(p * 100)


# ---------------------------------------------------------------- 1. sales
rain_today, rain_by_day = False, {}
for d in DAYS:
    pi = C.RAIN_PROB[d.month]
    a = min(1.0, pi * (1 - C.RAIN_PERSIST) / (1 - pi))
    rain_today = rng.random() < (C.RAIN_PERSIST if rain_today else a)
    rain_by_day[d] = rain_today

events_by_day = {}
for name, (s, e, m) in C.EVENTS.items():
    for k in range((e - s).days + 1):
        events_by_day[s + timedelta(k)] = (name, m)
private_dates = set()
for ym in pd.period_range(SIM_START, C.END, freq="M"):
    cands = [d for d in DAYS if d.year == ym.year and d.month == ym.month and d.weekday() in (4, 5) and is_open("CHS", d)]
    for k in rng.choice(len(cands), size=min(C.PRIVATE_EVENTS_PER_MONTH, len(cands)), replace=False):
        private_dates.add(cands[k])

tickets, lines, daily, refunds, gift_cards = [], [], [], [], []
usage = defaultdict(float)
gc_pool, gc_sold = defaultdict(int), defaultdict(int)
tid = 0


def add_line(order, item, q, d, channel, service):
    order[item] = order.get(item, 0) + q


for d in DAYS:
    m, dow, rain = d.month, d.weekday(), rain_by_day[d]
    trend = 1 + C.COVER_TREND_PER_YEAR * (d - C.START).days / 365
    ev_name, ev = events_by_day.get(d, (None, {}))
    for loc, L in C.LOCATIONS.items():
        if not is_open(loc, d):
            daily.append(dict(location=loc, date=d, open=0, rain=int(rain), event=ev_name, covers_lunch=0,
                              covers_dinner=0, covers_delivery=0, private_event_guests=0))
            continue
        base = C.DOW_MULT[dow] * C.MONTH_MULT[m] * trend * rng.lognormal(0, 0.08)
        evm = {k: (1 + (v - 1) * C.FLEET_WEEK_UNI_DAMPING if ev_name == "fleet_week" and loc == "UNI" else v)
               for k, v in ev.items()}
        rd = C.RAIN_DINEIN if rain else 1
        cov = {s: rng.poisson(L[s] * base * evm.get(s, 1) * rd) for s in ("lunch", "dinner")}
        del_cov = rng.poisson((L["lunch"] + L["dinner"]) * C.DELIVERY_SHARE / (1 - C.DELIVERY_SHARE) * base
                              * evm.get("delivery", 1) * (C.RAIN_DELIVERY if rain else 1))
        parties = []
        for service, n in cov.items():
            while n > 0:
                size = min(n, rng.choice(6, p=C.PARTY_SIZE_P[service]) + 1)
                parties.append((service, "dine_in", size))
                n -= size
        n = del_cov
        while n > 0:
            size = min(n, rng.choice(3, p=[.45, .40, .15]) + 1)
            parties.append(("lunch" if rng.random() < 0.3 else "dinner",
                            rng.choice(list(C.PLATFORMS), p=list(C.PLATFORMS.values())), size))
            n -= size
        pe_guests = int(rng.integers(12, 26)) if (loc == "CHS" and d in private_dates) else 0
        if pe_guests:
            parties.append(("dinner", "private_event", pe_guests))
        daily.append(dict(location=loc, date=d, open=1, rain=int(rain), event=ev_name, covers_lunch=cov["lunch"],
                          covers_dinner=cov["dinner"], covers_delivery=del_cov, private_event_guests=pe_guests))
        for service, channel, size in parties:
            tid += 1
            delivery = channel in C.PLATFORMS
            order = {}
            if channel == "private_event":
                order = {"Private Event Prix Fixe": size, "Bottle of Wine": size // 3}
            else:
                att = C.LUNCH_ATTACH if service == "lunch" else 1.0
                n_main = rng.binomial(size, C.MAIN_PER_COVER) or 1
                for it in rng.choice(MAINS, size=n_main, p=W["main"]):
                    add_line(order, it, 1, d, channel, service)
                if (k := rng.binomial(n_main, C.MODIFIER_P)):
                    add_line(order, "Add Grilled Chicken", k, d, channel, service)
                for it in rng.choice(STARTERS, size=rng.binomial(size, C.STARTER_PER_COVER * att * (0.8 if delivery else 1)), p=W["starter"]):
                    add_line(order, it, 1, d, channel, service)
                for it in rng.choice(DESSERTS, size=rng.binomial(size, C.DESSERT_PER_COVER * att * (0.7 if delivery else 1)), p=W["dessert"]):
                    add_line(order, it, 1, d, channel, service)
                for dr in DRINKS:
                    alcohol = dr in ("House Wine (glass)", "Cocktail", "Draft Beer")
                    if delivery and dr != "Soda":
                        continue
                    p = C.MENU[dr][2] * (att if alcohol else 1.0) * (1.5 if service == "lunch" and dr == "Soda" else 1)
                    if (k := rng.binomial(size, min(1, p))):
                        add_line(order, dr, k, d, channel, service)
                if not delivery and service == "dinner" and size >= 2 and rng.random() < C.MENU["Bottle of Wine"][2]:
                    add_line(order, "Bottle of Wine", 1 + int(size >= 4 and rng.random() < .4), d, channel, service)
            gross = sum(menu_price(i, d, channel, service) * q for i, q in order.items())
            discount, reason = 0, None
            if channel == "dine_in" and rng.random() < C.DISCOUNT_P:
                discount = cents(gross * rng.uniform(*C.DISCOUNT_RANGE))
                reason = rng.choice(["comp", "manager", "loyalty"], p=[.4, .35, .25])
            subtotal = gross - discount
            if delivery:
                tender, tax, tip = "platform", 0, 0          # marketplace facilitator remits tax; tips go to drivers
            else:
                svc = cents(subtotal * 0.20) if channel == "private_event" else 0
                tax = cents((subtotal + svc) * C.SALES_TAX)
                tender = "card" if (channel == "private_event" or rng.random() < C.CARD_SHARE_DINEIN) else "cash"
                if (tender == "card" and d >= C.GIFT_CARDS["redeem_from"]
                        and gc_pool[loc] - (subtotal + tax) >= C.GIFT_CARDS["breakage"] * gc_sold[loc]
                        and rng.random() < C.GIFT_CARDS["redeem_p"]):
                    tender = "gift_card"
                    gc_pool[loc] -= subtotal + tax
                if channel == "private_event":
                    tip = svc                                   # auto-gratuity (taxable service charge)
                elif tender == "card":
                    tip = cents(subtotal * rng.choice(C.TIP_CHOICES, p=C.TIP_P))
                    if rng.random() < C.TIP_ROUND_UP_P:
                        tip += (100 - (subtotal + tax + tip) % 100) % 100
                else:
                    tip = 0
            hour = (11 + int(rng.choice(4, p=C.LUNCH_HOUR_P))) if service == "lunch" else (17 + int(rng.choice(5, p=C.DINNER_HOUR_P)))
            tickets.append(dict(ticket_id=tid, location=loc, date=d, hour=hour, service=service, channel=channel,
                                party_size=size, tender=tender, gross=gross, discount=discount, discount_reason=reason,
                                subtotal=subtotal, tax=tax, tip=tip))
            for it, q in order.items():
                lines.append(dict(ticket_id=tid, item=it, qty=q, unit_price=menu_price(it, d, channel, service)))
                for g, qty in C.MENU[it][3].items():
                    usage[(loc, d, g)] += qty * q
            if tender == "card" and channel == "dine_in" and rng.random() < C.REFUND_P:
                rd = d + timedelta(1)
                while not is_open(loc, rd):                   # refunds go out with the next open day's batch
                    rd += timedelta(1)
                if rd <= C.END:
                    refunds.append(dict(ticket_id=tid, location=loc, sale_date=d, refund_date=rd,
                                        amount=subtotal + tax + tip, reason="guest complaint"))
        if C.GIFT_CARDS["sell_from"] <= d <= C.GIFT_CARDS["sell_to"]:
            for _ in range(rng.poisson(C.GIFT_CARDS["per_day"][loc])):
                amt = int(rng.choice(C.GIFT_CARDS["amounts"])) * 100
                gift_cards.append(dict(card_id=f"GC{len(gift_cards) + 1:05d}", location=loc, sold_date=d, amount=amt))
                gc_pool[loc] += amt
                gc_sold[loc] += amt

T, TL, DAILY = pd.DataFrame(tickets), pd.DataFrame(lines), pd.DataFrame(daily)
REF, GC = pd.DataFrame(refunds), pd.DataFrame(gift_cards)

# ---------------------------------------------------------------- 2. labor (scheduled from a forecast, then adjusted)
DAILY["realized_covers"] = DAILY.covers_lunch + DAILY.covers_dinner + 0.5 * DAILY.covers_delivery + DAILY.private_event_guests
DAILY["dow"] = [d.weekday() for d in DAILY.date]
DAILY = DAILY.sort_values(["location", "date"]).reset_index(drop=True)
DAILY["forecast_covers"] = (DAILY[DAILY.open == 1].groupby(["location", "dow"]).realized_covers
                            .transform(lambda s: s.shift(1).rolling(4, min_periods=1).mean()))
for loc, L in C.LOCATIONS.items():
    msk = (DAILY.location == loc) & (DAILY.open == 1)
    first = DAILY.loc[msk, "realized_covers"].head(14).mean()
    fc = DAILY.loc[msk, "forecast_covers"].fillna(first)
    DAILY.loc[msk, "scheduled_hours"] = L["fixed_hrs"] + L["hrs_per_cover"] * fc
    DAILY.loc[msk, "actual_hours"] = ((DAILY.loc[msk, "scheduled_hours"] + C.SCHEDULE_ADJ * L["hrs_per_cover"]
                                       * (DAILY.loc[msk, "realized_covers"] - fc)) * rng.lognormal(0, 0.03, msk.sum()))
DAILY[["scheduled_hours", "actual_hours"]] = DAILY[["scheduled_hours", "actual_hours"]].fillna(0).round(2)
DAILY["foh_rate"] = [step(C.MIN_WAGE, d) for d in DAILY.date]
DAILY["boh_rate"] = [step(C.BOH_WAGE, d) for d in DAILY.date]
DAILY["wages_cents"] = (DAILY.actual_hours * (C.FOH_HOURS_SHARE * DAILY.foh_rate + (1 - C.FOH_HOURS_SHARE) * DAILY.boh_rate) * 100).round().astype(int)
DAILY["staff_meals"] = (DAILY.actual_hours / C.STAFF_MEAL_PER_HOURS).round().astype(int)
for r in DAILY[DAILY.staff_meals > 0].itertuples():
    for g, q in C.STAFF_MEAL.items():
        usage[(r.location, r.date, g)] += q * r.staff_meals

# ---------------------------------------------------------------- 3. purchasing with packs, par levels, inventory
by_sup = defaultdict(list)
for g, v in C.INGREDIENTS.items():
    by_sup[v[2]].append(g)
invoices, inv_lines, counts, credit_memos = [], [], [], []
inv_seq = defaultdict(lambda: 10000)
stockouts = 0


def first_on_or_after(d0, weekday):
    return d0 + timedelta((weekday - d0.weekday()) % 7)


D3_REF = first_on_or_after(date(2026, 3, 3), 1)          # Sonoma Dairy standing order at C.ALT_LOC, two Tuesdays
D3_TGT = D3_REF + timedelta(7)
CM_DAY = first_on_or_after(date(2026, 3, 10), 1)         # Northbay Protein Co short-ships salmon at CHS
d3_lines = None
for loc in C.LOCATIONS:
    consumed = {g: [usage.get((loc, d, g), 0) * C.USAGE_VARIANCE for d in DAYS] for g in C.INGREDIENTS}
    on_hand = {g: 3 * (sum(consumed[g][:14]) / 14) for g in C.INGREDIENTS}
    for di, d in enumerate(DAYS):
        for sup, (cat, terms, ddays, _) in C.SUPPLIERS.items():
            if d.weekday() not in ddays or not is_open(loc, d):
                continue
            nd = next(d + timedelta(k) for k in range(1, 15) if (d + timedelta(k)).weekday() in ddays and is_open(loc, d + timedelta(k)))
            cover = (nd - d).days
            ln = []
            if loc == C.ALT_LOC and sup == "Sonoma Dairy Supply" and d == D3_TGT and d3_lines:
                ln = list(d3_lines)                         # standing order at contract prices
            else:
                for g in by_sup[sup]:
                    hist = consumed[g][max(0, di - 14):di] or consumed[g][:14]
                    par = sum(hist) / len(hist) * cover * C.PAR_SAFETY
                    need = par - on_hand[g]
                    if need > 0:
                        pack = C.INGREDIENTS[g][5]
                        ln.append((g, math.ceil(need / pack) * pack, round(ing_price(g, d) * rng.lognormal(0, 0.005), 4)))
            if loc == C.ALT_LOC and sup == "Sonoma Dairy Supply" and d == D3_REF:
                d3_lines = list(ln)
            if not ln:
                continue
            inv_seq[sup] += 1
            prefix = "".join(w[0] for w in sup.split()[:3]).upper()
            inv_no = f"{prefix}-{loc}-{inv_seq[sup]}"
            total = 0
            for g, q, p in ln:
                lt = cents(q * p * 100)
                total += lt
                on_hand[g] += q
                inv_lines.append(dict(invoice_no=inv_no, ingredient=g, unit=C.INGREDIENTS[g][0], qty=q, unit_price=p, line_total=lt))
            invoices.append(dict(invoice_no=inv_no, supplier=sup, category=cat, location=loc, invoice_date=d,
                                 due_date=d + timedelta(terms), terms_days=terms, total=total))
            if loc == "CHS" and sup == "Northbay Protein Co" and d == CM_DAY:
                sal = next(x for x in ln if x[0] == "salmon")
                short = min(sal[1], 5)
                on_hand["salmon"] -= short
                credit_memos.append(dict(credit_no="CM-2207", invoice_no=inv_no, supplier=sup, location=loc,
                                         issued=d + timedelta(6), amount=cents(short * sal[2] * 100),
                                         reason=f"Short-shipped {short} lb salmon; refunded by ACH"))
        for g in C.INGREDIENTS:
            on_hand[g] -= consumed[g][di]
            cat = C.SUPPLIERS[C.INGREDIENTS[g][2]][0]
            on_hand[g] *= 1 - C.SHRINK_PER_DAY.get(cat, 0)
            if on_hand[g] < 0:
                stockouts += 1
                on_hand[g] = 0.0
        if (d + timedelta(1)).day == 1 and d >= C.START - timedelta(1):
            for g in C.INGREDIENTS:
                q = round(on_hand[g] * rng.lognormal(0, 0.02), 1)
                counts.append(dict(location=loc, count_date=d, ingredient=g, qty=q, unit=C.INGREDIENTS[g][0],
                                   value_cents=cents(q * ing_price(g, d) * 100)))
INV, INVL, CNT, CM = pd.DataFrame(invoices), pd.DataFrame(inv_lines), pd.DataFrame(counts), pd.DataFrame(credit_memos)

# ---------------------------------------------------------------- 4. payroll
payroll = []
pay = C.PAYROLL_FIRST
while pay <= C.END + timedelta(14):
    pay_d = prev_bday(pay)
    p_start, p_end = pay - timedelta(18), pay - timedelta(5)
    for loc, L in C.LOCATIONS.items():
        dm = (DAILY.location == loc) & DAILY.date.between(p_start, p_end)
        hourly = int(DAILY.loc[dm, "wages_cents"].sum())
        share = C.SALARY_SHARE[loc]
        salary = cents((L["gm_salary"] + share * sum(C.SHARED_SALARIES.values())) / 26 * 100)
        tk = T[(T.location == loc) & T.date.between(p_start, p_end)]
        tips = int(tk.tip.sum())
        burden = cents((hourly + salary) * C.EMPLOYER_BURDEN + tips * C.TIP_FICA)
        payroll.append(dict(location=loc, pay_date=pay_d, period_start=p_start, period_end=p_end,
                            hours=round(DAILY.loc[dm, "actual_hours"].sum(), 1), hourly_wages=hourly, salaries=salary,
                            tips_paid=tips, employer_taxes_benefits=burden, total_debit=hourly + salary + tips + burden))
    pay += timedelta(14)
PAY = pd.DataFrame(payroll)
PAY_FUTURE = PAY[PAY.pay_date > C.END]                # upcoming runs: not in the books (cadence is learnable)

# ---------------------------------------------------------------- 5. books' other source documents + expected bank (grader)
ledger, vendor_bills, repairs, misc, draws, transfers, disputes, batches, slips, plans = [], [], [], [], [], [], [], [], [], []


def LG(acct, d, ntype, direction, amount, cp, memo, kind, ref):
    ledger.append(dict(account=acct, date=d, nessie_type=ntype, direction=direction, amount=int(amount),
                       counterparty=cp, memo=memo, kind=kind, books_ref=ref))


last4 = {a: v[2] for a, v in C.ACCOUNTS.items()}
# card batches (POS end-of-day report: gross, refunds, count; fees per processor agreement)
card_t = T[T.tender == "card"].assign(g=lambda x: x.subtotal + x.tax + x.tip)
gc_sales = GC.groupby(["location", "sold_date"]).amount.agg(["sum", "count"]) if len(GC) else None
ref_by = REF.groupby(["location", "refund_date"]).amount.sum() if len(REF) else pd.Series(dtype=int)
keys = set(card_t.groupby(["location", "date"]).groups) | (set(gc_sales.index) if gc_sales is not None else set())
batch_items = {}


def batch_fee(items, pct):
    return sum(cents(x * pct) + C.CARD_FIXED for x in items)      # processor rounds per transaction


for loc, d in sorted(keys):
    g = card_t[(card_t.location == loc) & (card_t.date == d)]
    gs, gn = (gc_sales.loc[(loc, d)] if gc_sales is not None and (loc, d) in gc_sales.index else (0, 0))
    gross, n = int(g.g.sum()) + int(gs), len(g) + int(gn)
    rf = int(ref_by.get((loc, d), 0))
    batch_items[(loc, d)] = [int(x) for x in g.g] + ([int(x) for x in GC[(GC.location == loc) & (GC.sold_date == d)].amount] if len(GC) else [])
    fee = batch_fee(batch_items[(loc, d)], C.CARD_PCT)
    dep = next_bday(d + timedelta(1))
    if rng.random() < C.SETTLEMENT_DELAY_P:
        dep = next_bday(dep + timedelta(1))
    batches.append(dict(location=loc, batch_date=d, gross_sales=gross, transactions=n, refunds=rf))
    LG(loc, dep, "deposit", "in", gross - rf - fee, C.PROCESSOR, f"TABLETAP PAYMENTS DEP {d:%m%d}", "card_settlement", f"BATCH-{loc}-{d:%Y%m%d}")
# cash: deposit slip = counted cash (POS cash +/- small variance), Tue & Fri
cash = T[T.tender == "cash"].assign(g=lambda x: x.subtotal + x.tax).groupby(["location", "date"]).g.sum()
undeposited = {}
for loc in C.LOCATIONS:
    acc = 0
    for d in DAYS:
        acc += int(cash.get((loc, d), 0))
        if d.weekday() in (1, 4) and bday(d) and acc:
            counted = acc + int(rng.integers(-C.CASH_VARIANCE_CENTS, C.CASH_VARIANCE_CENTS + 1))
            slips.append(dict(location=loc, deposit_date=d, pos_cash=acc, counted_cash=counted))
            LG(loc, d, "deposit", "in", counted, "Branch", "BRANCH DEPOSIT", "cash_deposit", f"SLIP-{loc}-{d:%Y%m%d}")
            acc = 0
    undeposited[loc] = acc
# delivery payouts: Mon-Sun week, paid the following Tuesday
dlv = T[T.channel.isin(list(C.PLATFORMS))].copy()
dlv["wk"] = dlv.date.map(lambda d: d - timedelta(d.weekday()))
dlv_transit = []
for (loc, plat, wk), g in dlv.groupby(["location", "channel", "wk"]):
    gross = int(g.subtotal.sum())
    if wk <= C.END < next_bday(wk + timedelta(8)):
        dlv_transit.append(dict(type="delivery_payout", location=loc, source_date=wk, amount=gross - cents(gross * C.DELIVERY_COMMISSION),
                                note=f"{plat} week of {wk}"))
    LG(loc, next_bday(wk + timedelta(8)), "deposit", "in", gross - cents(gross * C.DELIVERY_COMMISSION), plat,
       f"{plat.upper()} PAYOUT WK {wk:%m%d}", "delivery_payout", f"DLV-{loc}-{plat}-{wk:%Y%m%d}")
# supplier invoices -> ACH on due date
D2 = INV[(INV.supplier == "Mission Dry Goods") & (INV.location == "CHS") & INV.invoice_date.between(date(2026, 1, 20), date(2026, 2, 6))].sort_values("total").iloc[-1]
for r in INV.itertuples():
    memo = f"ACH {r.supplier.upper()}" + (f" INV {r.invoice_no}" if C.SUPPLIERS[r.supplier][3] else "")
    if r.invoice_no == D2.invoice_no:
        d1, d2 = next_bday(r.due_date), next_bday(r.due_date + timedelta(14))
        first = r.total // 2
        plans.append(dict(invoice_no=r.invoice_no, installment=1, due=d1, amount=first))
        plans.append(dict(invoice_no=r.invoice_no, installment=2, due=d2, amount=r.total - first))
        LG(r.location, d1, "purchase", "out", first, r.supplier, memo + " 1/2", "supplier_payment", r.invoice_no)
        LG(r.location, d2, "purchase", "out", r.total - first, r.supplier, memo + " 2/2", "supplier_payment", r.invoice_no)
    else:
        LG(r.location, next_bday(r.due_date), "purchase", "out", r.total, r.supplier, memo, "supplier_payment", r.invoice_no)
for r in CM.itertuples():
    LG(r.location, r.issued, "deposit", "in", r.amount, r.supplier, f"{r.supplier.upper()} CREDIT {r.credit_no}", "credit_memo", r.credit_no)
# rent, vendor bills, repairs, permits
months = pd.period_range(SIM_START, C.END, freq="M")
cov_m = DAILY.assign(ym=pd.to_datetime(DAILY.date).dt.to_period("M")).groupby(["location", "ym"]).realized_covers.sum()
for loc in C.LOCATIONS:
    Ls = C.LEASES[loc]
    avg_cov = cov_m[loc].mean()
    for ym in months.append(pd.PeriodIndex([pd.Period(C.END, "M") + 1])):
        d1 = date(ym.year, ym.month, 1)
        rent = cents(Ls["base"] * (1 + Ls["escalator_pct"] if d1 >= Ls["anniversary"] else 1))
        LG(loc, next_bday(d1), "purchase", "out", rent, Ls["landlord"], f"ACH {Ls['landlord'].upper()} RENT {ym}", "rent", f"RENT-{loc}-{ym}")
        for payee, cat, amts, day, var in C.RECURRING:
            if loc not in amts:
                continue
            if date(ym.year, ym.month, day) - timedelta(14) > C.END:
                continue
            a = amts[loc]
            if var == "seasonal":
                a *= (1.15 if ym.month in (11, 12, 1, 2) else 0.92 if ym.month in (6, 7, 8, 9) else 1.0) * rng.lognormal(0, .06)
            elif var == "noise":
                a *= rng.lognormal(0, .05)
            elif var == "volume":
                a *= cov_m.get((loc, ym), cov_m[loc].iloc[-1]) / avg_cov * rng.lognormal(0, .04)
            elif var == "campaign":
                a = rng.choice([1_500_00, 2_500_00, 4_000_00], p=[.3, .5, .2])
            code = "".join(w[0] for w in payee.replace("(SIM)", "").replace("&", "").split()[:3]).upper()
            bill_id = f"VB-{loc}-{code}-{ym}"
            due = date(ym.year, ym.month, day)
            vendor_bills.append(dict(bill_id=bill_id, vendor=payee, category=cat, location=loc,
                                     bill_date=due - timedelta(14), due_date=due, amount=cents(a), autopay=1))
            LG(loc, next_bday(due), "purchase", "out", cents(a), payee, f"{payee.upper().replace(' (SIM)', '')} AUTOPAY", "vendor_bill", bill_id)
        for k in range(rng.poisson(C.REPAIRS["per_month_per_loc"]) if ym <= pd.Period(C.END, "M") else 0):
            d = next_bday(date(ym.year, ym.month, int(rng.integers(1, 28))))
            amt = cents(C.REPAIRS["median_cents"] * rng.lognormal(0, .6))
            rid = f"RPR-{loc}-{ym}-{k}"
            repairs.append(dict(repair_id=rid, location=loc, date=d, vendor=C.REPAIRS["merchant"], amount=amt, description="Service call"))
            LG(loc, d, "purchase", "out", amt, C.REPAIRS["merchant"], "BAY KITCHEN REPAIR SVC", "repair", rid)
bd, bloc, bamt, bdesc = C.REPAIRS["big"]
repairs.append(dict(repair_id="RPR-BIG-1", location=bloc, date=bd, vendor=C.REPAIRS["merchant"], amount=bamt, description=bdesc))
LG(bloc, bd, "purchase", "out", bamt, C.REPAIRS["merchant"], "BAY KITCHEN REPAIR SVC", "repair", "RPR-BIG-1")
for d, loc, amt, payee, desc in C.PERMITS:
    mid = f"MISC-{loc}-{d:%Y%m%d}"
    misc.append(dict(misc_id=mid, location=loc, date=d, payee=payee, amount=amt, description=desc))
    LG(loc, next_bday(d), "purchase", "out", amt, payee, f"{payee.upper()} {desc.upper()}", "permit", mid)
# payroll, sales tax, loan, draw, chargebacks
for r in PAY.itertuples():
    LG(r.location, r.pay_date, "withdrawal", "out", r.total_debit, "BayPay", f"BAYPAY PAYROLL {r.pay_date:%m/%d}", "payroll", f"PAY-{r.location}-{r.pay_date:%Y%m%d}")
T["_mkt"] = np.where(T.tender == "platform", T.subtotal, 0)
T["_txbl"] = np.where(T.tender == "platform", 0, T.subtotal)
tax_m = T.assign(ym=pd.to_datetime(T.date).dt.to_period("M")).groupby("ym").agg(tax=("tax", "sum"), taxable=("_txbl", "sum"), mkt=("_mkt", "sum"))
T = T.drop(columns=["_mkt", "_txbl"])
returns = []
for ym in months[:-1]:
    nxt = ym + 1
    d = next_bday(date(nxt.year, nxt.month, C.SALES_TAX_REMIT_DAY))
    returns.append(dict(period=str(ym), taxable_sales=int(tax_m.loc[ym, "taxable"]), marketplace_sales_excluded=int(tax_m.loc[ym, "mkt"]),
                        tax_due=int(tax_m.loc[ym, "tax"]), due_date=d, paid_date=d if d <= C.END else None))
    LG("CHS", d, "withdrawal", "out", int(tax_m.loc[ym, "tax"]), "CDTFA", f"CDTFA EFT SALES TAX {ym}", "sales_tax", f"TAX-{ym}")
loan_sched, bal = [], C.LOAN["balance_at_start"]
for ym in pd.period_range(C.START, C.END + timedelta(400), freq="M"):
    d = next_bday(date(ym.year, ym.month, C.LOAN["day"]))
    if d < C.START or bal <= 0:
        continue
    interest = cents(bal * C.LOAN["rate"] / 12)
    principal = min(C.LOAN["monthly"] - interest, bal)
    loan_sched.append(dict(due=d, payment=interest + principal, interest=interest, principal=principal, balance_after=bal - principal))
    bal -= principal
    if d <= C.END:
        LG(C.LOAN["account"], d, "withdrawal", "out", interest + principal, C.LOAN["lender"], "PACIFIC COAST SBL LOAN PMT", "loan_payment", f"LOAN-{ym}")
for ym in months:
    d = next_bday(date(ym.year, ym.month, C.OWNER_DRAW["day"]))
    draws.append(dict(date=d, amount=C.OWNER_DRAW["monthly"], from_account="CHS"))
    LG("CHS", d, "withdrawal", "out", C.OWNER_DRAW["monthly"], "Owner", "OWNER DRAW TO PERSONAL", "owner_draw", f"DRAW-{ym}")
for d, loc, amt in C.CHARGEBACKS:
    cid = f"CB-{loc}-{d:%m%d}"
    disputes.append(dict(case_id=cid, location=loc, opened=d - timedelta(9), debited=d, amount=amt, outcome="lost"))
    LG(loc, d, "withdrawal", "out", amt, C.PROCESSOR, f"TABLETAP CHARGEBACK {cid}", "chargeback", cid)

# keep only the bank window, then add sweeps that keep UNI inside its band (two-location mode only)
ledger = [l for l in ledger if C.START <= l["date"] <= C.END]


def balance_on(rows, acct, d):
    return C.OPENING_BALANCE[acct] + sum(r["amount"] * (1 if r["direction"] == "in" else -1)
                                         for r in rows if r["account"] == acct and r["date"] <= d)


def xfer(d, src, dst, amt, ref):
    transfers.append(dict(date=d, from_account=src, to_account=dst, amount=amt, ref=ref))
    LG(src, d, "withdrawal", "out", amt, "Internal", f"ONLINE XFER TO {C.ACCOUNTS[dst][1].upper()}", "internal_transfer", ref)
    LG(dst, d, "deposit", "in", amt, "Internal", f"ONLINE XFER FROM {C.ACCOUNTS[src][1].upper()}", "internal_transfer", ref)


xfer(C.RESERVE_DRAW[0], "RES", "CHS", C.RESERVE_DRAW[1], "XFER-RES-1")
for ym in pd.period_range(C.START, C.END, freq="M"):
    d = next_bday(date(ym.year, ym.month, 26))
    if "UNI" not in C.ACCOUNTS or not (C.START <= d <= C.END):    # sweeps only exist with two locations
        continue
    b = balance_on(ledger, "UNI", d + timedelta(10))
    if b < C.SWEEP_LOW or b > C.SWEEP_HIGH:
        amt = int(round((C.SWEEP_TARGET - b) / 1000_00) * 1000_00)
        xfer(d, *(("CHS", "UNI") if amt > 0 else ("UNI", "CHS")), abs(amt), f"XFER-{ym}")

LED = pd.DataFrame(ledger).sort_values(["date", "account", "kind", "books_ref"]).reset_index(drop=True)
LED.insert(0, "txn_id", [f"L{i:06d}" for i in range(1, len(LED) + 1)])

# ---------------------------------------------------------------- 6. bank = expected + planted errors
BANK = LED.copy()
BANK["planted"] = None
key = []


def find(**c):
    m = np.ones(len(BANK), bool)
    for k, v in c.items():
        m &= (BANK[k].between(*v) if isinstance(v, tuple) else BANK[k] == v).to_numpy()
    hits = BANK[m & BANK.planted.isna().to_numpy()]
    assert len(hits), c
    return hits.index[0]


# E1 duplicate supplier debit
i = find(account="CHS", kind="supplier_payment", counterparty="Bay Produce Co", date=(date(2026, 4, 13), date(2026, 4, 24)))
dup = BANK.loc[i].copy(); dup["txn_id"] = None; dup["planted"] = "E1"
BANK.loc[i, "planted"] = "E1"
BANK = pd.concat([BANK, dup.to_frame().T], ignore_index=True)
key.append(dict(id="E1", kind="duplicate_charge", account="CHS", date=str(BANK.loc[i, "date"]), books_ref=BANK.loc[i, "books_ref"],
                impact_cents=int(BANK.loc[i, "amount"]), explanation="Supplier ACH debited twice for one invoice."))
# E2 debit above invoice
i = find(account=C.ALT_LOC, kind="supplier_payment", counterparty="Northbay Protein Co", date=(date(2026, 5, 4), date(2026, 5, 15)))
BANK.loc[i, "amount"] = int(BANK.loc[i, "amount"]) + 100_00; BANK.loc[i, "planted"] = "E2"
key.append(dict(id="E2", kind="supplier_overcharge", account=C.ALT_LOC, date=str(BANK.loc[i, "date"]), books_ref=BANK.loc[i, "books_ref"],
                impact_cents=100_00, explanation="Supplier debit is $100.00 above the invoice it pays."))
# E3 delivery commission at 20% for three June weeks (one root cause)
imp = 0
idx = BANK[(BANK.account == "CHS") & (BANK.counterparty == "RapidPlate") & BANK.date.between(date(2026, 6, 2), date(2026, 6, 30))].index[:3]
for i in idx:
    wk = pd.to_datetime(BANK.loc[i, "books_ref"][-8:]).date()
    gross = int(dlv[(dlv.location == "CHS") & (dlv.channel == "RapidPlate") & (dlv.wk == wk)].subtotal.sum())
    new = gross - cents(gross * 0.20)
    imp += int(BANK.loc[i, "amount"]) - new
    BANK.loc[i, "amount"] = new; BANK.loc[i, "planted"] = "E3"
key.append(dict(id="E3", kind="delivery_commission_overcharge", account="CHS", date=str(BANK.loc[idx[0], "date"]),
                books_ref=list(BANK.loc[idx, "books_ref"]), impact_cents=imp,
                explanation="Three weekly RapidPlate payouts imply 20% commission vs the contracted 15%."))
# E4 card fee creep at CHS for sales dated on/after 2026-08-01
creep_from = date(2026, 8, 1)
idx = BANK[(BANK.account == "CHS") & (BANK.kind == "card_settlement")].index
idx = [i for i in idx if pd.to_datetime(BANK.loc[i, "books_ref"][-8:]).date() >= creep_from]
bt = pd.DataFrame(batches).set_index(["location", "batch_date"])
imp = 0
for i in idx:
    sd = pd.to_datetime(BANK.loc[i, "books_ref"][-8:]).date()
    b = bt.loc[("CHS", sd)]
    new = int(b.gross_sales) - int(b.refunds) - batch_fee(batch_items[("CHS", sd)], 0.029)
    imp += int(BANK.loc[i, "amount"]) - new
    BANK.loc[i, "amount"] = new; BANK.loc[i, "planted"] = "E4"
key.append(dict(id="E4", kind="card_fee_creep", account="CHS", date=str(creep_from), books_ref="BATCH-CHS-* (sale date >= 2026-08-01)",
                impact_cents=imp, explanation="Processor charges 2.9% instead of the agreed 2.6% on every CHS batch for sales from 2026-08-01."))
# E5 cash deposit short of the deposit slip
i = find(account=C.ALT_LOC, kind="cash_deposit", date=(date(2026, 2, 10), date(2026, 2, 13)))
BANK.loc[i, "amount"] = int(BANK.loc[i, "amount"]) - 200_00; BANK.loc[i, "planted"] = "E5"
key.append(dict(id="E5", kind="cash_deposit_short", account=C.ALT_LOC, date=str(BANK.loc[i, "date"]), books_ref=BANK.loc[i, "books_ref"],
                impact_cents=200_00, explanation="Bank credited $200 less than the deposit slip."))
# E6 charge with no document in the books
row = dict(txn_id=None, account="CHS", date=date(2026, 7, 9), nessie_type="purchase", direction="out", amount=349_00,
           counterparty="TableTap POS", memo="TABLETAP POS HARDWARE PROTECTION", kind="unrecorded", books_ref=None, planted="E6")
BANK = pd.concat([BANK, pd.DataFrame([row])], ignore_index=True)
key.append(dict(id="E6", kind="unrecorded_charge", account="CHS", date="2026-07-09", books_ref=None, impact_cents=349_00,
                explanation="Charge with no bill, invoice, agreement or log entry in the books."))
# E7 invoice due inside the window but never paid
i = find(account=C.ALT_LOC, kind="supplier_payment", counterparty="Sonoma Dairy Supply", date=(date(2026, 9, 28), date(2026, 10, 9)))
E7_ref, E7_amt, E7_date = BANK.loc[i, "books_ref"], int(BANK.loc[i, "amount"]), str(BANK.loc[i, "date"])
BANK = BANK.drop(index=i)
key.append(dict(id="E7", kind="missed_payment", account=C.ALT_LOC, date=E7_date, books_ref=E7_ref, impact_cents=E7_amt,
                explanation="Invoice past due in the books; no payment ever left the bank (late-fee and supplier risk)."))

# assign bank-side ids only after planting (content-derived, see below)
BANK = BANK.sort_values(["date", "account", "nessie_type", "amount"]).reset_index(drop=True)
# natural noise: the bank occasionally recounts a cash deposit (+/-$1-9); immaterial, not an error
cash_rows = BANK[(BANK.kind == "cash_deposit") & BANK.planted.isna()].index
for i in cash_rows[rng.random(len(cash_rows)) < C.BANK_COUNT_CORRECTION_P]:
    BANK.loc[i, "amount"] = int(BANK.loc[i, "amount"]) + int(rng.choice([-1, 1]) * rng.integers(100, 1000))
    BANK.loc[i, "planted"] = "noise"
# ids derive from row content: regenerating identical data gives identical ids, and ids carry no position info
occ = BANK.groupby(["account", "date", "amount", "memo"]).cumcount()
BANK["bank_id"] = ["T" + hashlib.sha1(f"{r.account}|{r.date}|{r.amount}|{r.memo}|{n}".encode()).hexdigest()[:9].upper()
                   for r, n in zip(BANK.itertuples(), occ)]
assert BANK.bank_id.is_unique
BANK["amount"] = BANK.amount.astype("int64")
assert (BANK.amount > 0).all()
for k in key:
    k["bank_ids"] = list(BANK[BANK.planted == k["id"]].bank_id)


def decoy(id_, kind, mask, note):
    key.append(dict(id=id_, kind=kind, should_flag=False, bank_ids=list(BANK[mask].bank_id),
                    date=str(BANK[mask].date.min()), explanation=note))


for k in key:
    k["should_flag"] = True
decoy("D1", "credit_memo_refund", BANK.books_ref == "CM-2207", "Inbound from a supplier; books hold credit memo CM-2207.")
decoy("D2", "split_payment", (BANK.books_ref == D2.invoice_no) & (BANK.kind == "supplier_payment"),
      f"Two installments for {D2.invoice_no}; books hold the payment plan.")
d3 = INV[(INV.location == C.ALT_LOC) & (INV.supplier == "Sonoma Dairy Supply") & INV.invoice_date.isin([D3_REF, D3_TGT])].invoice_no
decoy("D3", "standing_order_same_amount", BANK.books_ref.isin(d3), "Identical amounts a week apart, same memo; two different standing-order invoices.")
decoy("D4", "documented_chargeback", BANK.kind == "chargeback", "Processor chargebacks; books hold the dispute log.")
is_batch = BANK.kind == "card_settlement"
late = pd.Series(False, index=BANK.index)
late[is_batch] = [r.date > next_bday(pd.to_datetime(r.books_ref[-8:]).date() + timedelta(1)) for r in BANK[is_batch].itertuples()]
decoy("D5", "delayed_settlement", late & BANK.planted.isna(), "Card batches that settled one business day late, in full.")
decoy("D6", "large_documented_repair", BANK.books_ref == "RPR-BIG-1", "Large repair, documented by a repair invoice.")

# ---------------------------------------------------------------- open AP / receivables at END
due_after = lambda r: (next_bday(r.due_date + timedelta(14)) if r.invoice_no == D2.invoice_no else next_bday(r.due_date)) > C.END
open_inv = INV[(INV.invoice_date <= C.END) & INV.apply(due_after, axis=1)].copy()   # E7 is autopay in the books: found only via the bank
VB = pd.DataFrame(vendor_bills)
open_vb = VB[(VB.bill_date <= C.END) & (VB.due_date.map(next_bday) > C.END)]
open_ap = pd.concat([open_inv.assign(doc_type="invoice", doc_id=open_inv.invoice_no, payee=open_inv.supplier,
                                     doc_date=open_inv.invoice_date)[["doc_type", "doc_id", "payee", "location", "doc_date", "due_date", "total"]],
                     open_vb.assign(doc_type="vendor_bill", doc_id=open_vb.bill_id, payee=open_vb.vendor, doc_date=open_vb.bill_date,
                                    total=open_vb.amount)[["doc_type", "doc_id", "payee", "location", "doc_date", "due_date", "total"]]])
open_tax = [r for r in returns if r["paid_date"] is None]
open_ap = pd.concat([open_ap, pd.DataFrame([dict(doc_type="sales_tax", doc_id=f"TAX-{r['period']}", payee="CDTFA", location="CHS",
                                                  doc_date=r["due_date"], due_date=r["due_date"], total=r["tax_due"]) for r in open_tax])])
in_transit = [dict(type="card_batch", location=b["location"], source_date=b["batch_date"],
                   amount=b["gross_sales"] - b["refunds"] - batch_fee(batch_items[(b["location"], b["batch_date"])], C.CARD_PCT), note="expected net of fees")
              for b in batches if b["batch_date"] <= C.END and next_bday(b["batch_date"] + timedelta(1)) > C.END]
in_transit += [dict(type="undeposited_cash", location=l, source_date=C.END, amount=a, note="POS cash not yet deposited") for l, a in undeposited.items() if a]
in_transit += dlv_transit

# ---------------------------------------------------------------- write: books (agent-visible)
win = lambda df, col: df[df[col] >= SIM_START]
menu_prices = []
for it, (course, p, _, _) in C.MENU.items():
    for eff in [SIM_START] + ([C.MENU_REPRICE[0]] if it in C.MENU_REPRICE[1] else []):
        for ch, sv in (("dine_in", "dinner"), ("dine_in", "lunch"), ("delivery", "dinner")):
            menu_prices.append(dict(item=it, course=course, channel=ch, service=sv, effective_from=eff,
                                    price_cents=menu_price(it, eff, "RapidPlate" if ch == "delivery" else "dine_in", sv)))
(OUT / "books.db").unlink(missing_ok=True)      # fresh file: dropped tables would linger in free pages
con = sqlite3.connect(OUT / "books.db")
tables = {
    "locations": pd.DataFrame([dict(location=k, name=L["name"], street_number=L["street_number"], street_name=L["street_name"],
                                    zip=C.GEO["zip"], neighborhood=C.GEO["neighborhood"][k], cuisine=C.GEO["cuisine"],
                                    lat=L["lat"], lng=L["lng"], seats=L["seats"], closed_dow=json.dumps(L["closed_dow"]))
                               for k, L in C.LOCATIONS.items()]),
    "accounts": pd.DataFrame([dict(account=a, type=t, nickname=n, last4=l4, opening_balance_cents=C.OPENING_BALANCE[a],
                                   opening_as_of=C.START) for a, (t, n, l4) in C.ACCOUNTS.items()]),
    "menu_prices": pd.DataFrame(menu_prices),
    "recipes": pd.DataFrame([dict(item=i, ingredient=g, qty=q, unit=C.INGREDIENTS[g][0]) for i, v in C.MENU.items() for g, q in v[3].items()]
                            + [dict(item="Staff Meal", ingredient=g, qty=q, unit=C.INGREDIENTS[g][0]) for g, q in C.STAFF_MEAL.items()]),
    "ingredients": pd.DataFrame([dict(ingredient=g, unit=v[0], supplier=v[2], pack_size=v[5]) for g, v in C.INGREDIENTS.items()]),
    "suppliers": pd.DataFrame([dict(supplier=s, category=v[0], terms_days=v[1], delivery_days=json.dumps(v[2])) for s, v in C.SUPPLIERS.items()]),
    "daily_ops": DAILY.drop(columns=["dow"]), "tickets": T, "ticket_lines": TL, "refunds": REF, "gift_cards": GC,
    "pos_card_batches": pd.DataFrame(batches), "cash_deposit_slips": pd.DataFrame(slips),
    "invoices": INV, "invoice_lines": INVL, "credit_memos": CM, "ap_payment_plans": pd.DataFrame(plans),
    "inventory_counts": CNT, "payroll_runs": PAY[PAY.pay_date <= C.END], "vendor_bills": VB, "repair_invoices": pd.DataFrame(repairs),
    "misc_payables": pd.DataFrame(misc), "owner_draws": pd.DataFrame(draws), "internal_transfers": pd.DataFrame(transfers),
    "processor_disputes": pd.DataFrame(disputes), "sales_tax_returns": pd.DataFrame(returns),
    "leases": pd.DataFrame([dict(location=k, **v) for k, v in C.LEASES.items()]),
    "agreements": pd.DataFrame([dict(counterparty=C.PROCESSOR, terms=f"{C.CARD_PCT:.1%} + {C.CARD_FIXED}c per card transaction; next-business-day settlement"),
                                *[dict(counterparty=p, terms=f"{C.DELIVERY_COMMISSION:.0%} commission on delivery subtotal; weekly payout Tuesday")
                                  for p in C.PLATFORMS]]),
    "loans": pd.DataFrame([dict(lender=C.LOAN["lender"], type=C.LOAN["type"], original_cents=C.LOAN["original"],
                                balance_at_start_cents=C.LOAN["balance_at_start"], rate=C.LOAN["rate"],
                                monthly_cents=C.LOAN["monthly"], pay_day=C.LOAN["day"], account=C.LOAN["account"])]),
    "loan_schedule": pd.DataFrame(loan_sched), "open_ap": open_ap, "deposits_in_transit": pd.DataFrame(in_transit),
}
for name, df in tables.items():
    df.to_sql(name, con, index=False)
con.close()
bank_cols = ["bank_id", "account", "date", "nessie_type", "direction", "amount", "counterparty", "memo"]
BANK.loc[BANK.nessie_type != "purchase", "counterparty"] = None      # banks show only a description for these
BANK[bank_cols].to_csv(OUT / "bank_feed.csv", index=False)
# ---------------------------------------------------------------- write: grader-only
LED.to_csv(GR / "ledger_expected.csv", index=False)
BANK[["bank_id", "txn_id", "kind", "books_ref", "planted"]].to_csv(GR / "bank_truth.csv", index=False)
(GR / "answer_key.json").write_text(json.dumps({"note": "SIMULATED. Grader only. Never load into books.db or Nessie.",
                                                 "materiality_cents": C.MATERIALITY_CENTS,
                                                 "scoring": "one item per root cause; a flag matches an item if it names any of its bank_ids or its books_ref",
                                                 "items": key}, indent=2, default=str))

# ---------------------------------------------------------------- calibration summary (accrual view)
Tw = T[T.date >= C.START]
ym = lambda s: pd.to_datetime(s).dt.to_period("M")
rev = Tw.groupby(ym(Tw.date)).subtotal.sum()
purch = INV[INV.invoice_date >= C.START].groupby(ym(INV[INV.invoice_date >= C.START].invoice_date)).total.sum()
inv_val = CNT.groupby(ym(CNT.count_date)).value_cents.sum()
d_inv = inv_val.diff().reindex(rev.index).fillna(0)
cogs = purch.reindex(rev.index).fillna(0) - d_inv
Dw = DAILY[DAILY.date >= C.START]
sal = (sum(L["gm_salary"] for L in C.LOCATIONS.values()) + sum(C.SHARED_SALARIES.values())) * 100 / 12
lab = (Dw.groupby(ym(Dw.date)).wages_cents.sum() + sal) * (1 + C.EMPLOYER_BURDEN)
bnk = BANK[BANK.kind != "internal_transfer"]
net = (bnk.amount * np.where(bnk.direction == "in", 1, -1)).groupby(ym(bnk.date)).sum()
summ = pd.DataFrame({"revenue_$": rev / 100, "cogs%": cogs / rev * 100, "labor%": lab / rev * 100, "net_cash_$": net / 100}).round(1)
print(summ.to_string())
ck = Tw[Tw.channel == "dine_in"].groupby("service").apply(lambda g: g.subtotal.sum() / g.party_size.sum() / 100, include_groups=False)
bev = TL.merge(Tw[["ticket_id"]]).assign(c=lambda x: x.item.map(lambda i: C.MENU[i][0]))
bev_share = (bev.qty * bev.unit_price)[bev.c.isin(["drink", "bottle"])].sum() / (bev.qty * bev.unit_price).sum()
print(f"\nFY revenue ${rev.sum() / 100:,.0f} | COGS {cogs.sum() / rev.sum():.1%} | labor (excl tips) {lab.sum() / rev.sum():.1%} "
      f"| net cash ${net.sum() / 100:,.0f} ({net.sum() / rev.sum():.1%})")
print(f"check/cover dine-in: lunch ${ck['lunch']:.2f} dinner ${ck['dinner']:.2f} | beverage share {bev_share:.1%} "
      f"| discounts {Tw.discount.sum() / Tw.gross.sum():.1%} | stockout ingredient-days {stockouts}")
sign = np.where(BANK.direction == "in", 1, -1)
bal = {a: (C.OPENING_BALANCE[a] + int((BANK.amount * sign)[BANK.account == a].sum())) / 100 for a in C.OPENING_BALANCE}
db = BANK.assign(s=BANK.amount * sign).groupby(["account", "date"]).s.sum().groupby(level=0).cumsum()
print("ending balances:", bal, "| total", round(sum(bal.values())),
      "| min daily:", {a: round((C.OPENING_BALANCE[a] + db[a].min()) / 100) for a in C.OPENING_BALANCE})
print(f"rows: tickets {len(T):,} lines {len(TL):,} invoices {len(INV):,} bank {len(BANK):,} {BANK.nessie_type.value_counts().to_dict()}")
print("answer key:", [(k["id"], k["kind"], len(k["bank_ids"])) for k in key])
