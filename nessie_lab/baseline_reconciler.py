"""Rule-based Bookkeeper baseline: books + bank -> data/flags_baseline.json. Never reads data/grader/.

The bank is the Nessie copy (data/nessie_sync_<tag>.db, via features.load_bank()); without it, bank_feed.csv with a warning.

Rebuilds what the bank should show from source documents, matches it to bank rows, then flags:
  missing     expected from the books, never hit the bank
  unexpected  bank row with no document behind it
  variance    matched, but the amount differs by more than materiality
"""
import json, sqlite3
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from features import load_bank

D = Path(__file__).parent / "data"
MATERIALITY = 1000                                     # cents
FED_HOLIDAYS = {date(2025, 11, 11), date(2025, 11, 27), date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 19),
                date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19), date(2026, 9, 7), date(2026, 10, 12)}


def nbd(d):
    while d.weekday() >= 5 or d in FED_HOLIDAYS:
        d += timedelta(1)
    return d


con = sqlite3.connect(D / "books.db")
B = {n: pd.read_sql(f"select * from {n}", con) for n in [r[0] for r in con.execute("select name from sqlite_master where type='table'")]}
for df in B.values():
    for c in df.columns:
        if c in ("date", "due", "issued", "debited") or c.endswith("_date") or c == "batch_date":
            df[c] = pd.to_datetime(df[c]).dt.date
bank, BANK_SOURCE = load_bank()
bank["date"] = pd.to_datetime(bank.date).dt.date
bank = bank.sort_values(["date", "account", "nessie_type", "amount", "bank_id"]).reset_index(drop=True)   # same order whatever the source
start, end = bank.date.min(), bank.date.max()
pct, fixed = 0.026, 15                                 # from agreements: "2.6% + 15c per card transaction"
assert "2.6%" in B["agreements"].terms[0]

E = []


def X(acct, d, direction, amt, ref, hint, tol=1):
    if start <= d <= end:
        E.append(dict(account=acct, date=d, direction=direction, amount=int(round(amt)), ref=ref, hint=hint, tol=tol))


for r in B["pos_card_batches"].itertuples():
    X(r.location, nbd(r.batch_date + timedelta(1)), "in", r.gross_sales - r.refunds - (r.gross_sales * pct + fixed * r.transactions),
      f"BATCH-{r.location}-{r.batch_date:%Y%m%d}", f"TABLETAP PAYMENTS DEP {r.batch_date:%m%d}", tol=3)
for r in B["cash_deposit_slips"].itertuples():
    X(r.location, r.deposit_date, "in", r.counted_cash, f"SLIP-{r.location}-{r.deposit_date}", "BRANCH DEPOSIT")
tk = B["tickets"]
dl = tk[tk.tender == "platform"].copy()
dl["wk"] = dl.date.map(lambda d: d - timedelta(d.weekday()))
for (loc, plat, wk), g in dl.groupby(["location", "channel", "wk"]):
    s = g.subtotal.sum()
    X(loc, nbd(wk + timedelta(8)), "in", s - round(s * 0.15), f"DLV-{loc}-{plat}-{wk}", f"{plat.upper()} PAYOUT WK {wk:%m%d}")
plans = B["ap_payment_plans"]
for r in plans.itertuples():
    X(r.invoice_no.split("-")[1], r.due, "out", r.amount, r.invoice_no, "ACH ")          # invoice_no = PREFIX-LOC-SEQ
for r in B["invoices"][~B["invoices"].invoice_no.isin(plans.invoice_no)].itertuples():
    X(r.location, nbd(r.due_date), "out", r.total, r.invoice_no, f"ACH {r.supplier.upper()}")
for r in B["credit_memos"].itertuples():
    X(r.location, r.issued, "in", r.amount, r.credit_no, r.credit_no)
for r in B["vendor_bills"].itertuples():
    X(r.location, nbd(r.due_date), "out", r.amount, r.bill_id, r.vendor.upper()[:10])
for r in B["leases"].itertuples():
    for m in pd.period_range(start, end, freq="M"):
        d1 = date(m.year, m.month, 1)
        X(r.location, nbd(d1), "out", r.base * (1 + r.escalator_pct if d1 >= pd.Timestamp(r.anniversary).date() else 1), f"RENT-{r.location}-{m}", "RENT")
for r in B["repair_invoices"].itertuples():
    X(r.location, r.date, "out", r.amount, r.repair_id, "BAY KITCHEN REPAIR")
for r in B["misc_payables"].itertuples():
    X(r.location, nbd(r.date), "out", r.amount, r.misc_id, r.payee.upper()[:10])
for r in B["payroll_runs"].itertuples():
    X(r.location, r.pay_date, "out", r.total_debit, f"PAY-{r.location}-{r.pay_date}", "BAYPAY PAYROLL")
for r in B["sales_tax_returns"].dropna(subset=["paid_date"]).itertuples():
    X("CHS", r.paid_date, "out", r.tax_due, f"TAX-{r.period}", "CDTFA")
for r in B["loan_schedule"].itertuples():
    X(B["loans"].account[0], r.due, "out", r.payment, f"LOAN-{r.due}", "LOAN PMT")
for r in B["owner_draws"].itertuples():
    X(r.from_account, r.date, "out", r.amount, f"DRAW-{r.date}", "OWNER DRAW")
for r in B["internal_transfers"].itertuples():
    X(r.from_account, r.date, "out", r.amount, r.ref, "ONLINE XFER TO")
    X(r.to_account, r.date, "in", r.amount, r.ref, "ONLINE XFER FROM")
for r in B["processor_disputes"].itertuples():
    X(r.location, r.debited, "out", r.amount, r.case_id, r.case_id)
E = pd.DataFrame(E)

# pass 1: amount within materiality and date within tolerance (closest date wins)
used, match = set(), {}
bank_by = {k: g for k, g in bank.groupby(["account", "direction"])}
for i, e in E.sort_values("amount", ascending=False).iterrows():
    g = bank_by.get((e.account, e.direction))
    if g is None:
        continue
    c = g[(~g.bank_id.isin(used)) & ((g.amount - e.amount).abs() <= MATERIALITY) &
          g.date.map(lambda d: abs((d - e.date).days) <= e.tol + 2)]
    if any(ch.isdigit() for ch in e.hint):                        # hint names a specific document (batch date, week, case)
        fam = e.hint.rsplit(" ", 1)[0]
        c = c[c.memo.str.contains(e.hint, regex=False) | ~c.memo.str.contains(fam, regex=False)]   # never take another doc's row
    if len(c):
        if c.memo.str.contains(e.hint, regex=False).any():       # description names this document: trust it over amount
            c = c[c.memo.str.contains(e.hint, regex=False)]
        j = c.assign(gap=c.date.map(lambda d: abs((d - e.date).days))).sort_values("gap").bank_id.iloc[0]
        used.add(j); match[i] = j
# pass 2: same kind of row (memo hint) and date, but amount off by more than materiality -> variance
flags = []
for i, e in E[~E.index.isin(match)].iterrows():
    g = bank_by.get((e.account, e.direction))
    c = g[(~g.bank_id.isin(used)) & g.memo.str.contains(e.hint, regex=False) & g.date.map(lambda d: abs((d - e.date).days) <= e.tol + 2)]
    if len(c):
        c = c.assign(diff=(c.amount - e.amount).abs()).sort_values("diff")
        j = c.bank_id.iloc[0]
        used.add(j); match[i] = j
        flags.append(dict(type="variance", bank_id=j, books_ref=e.ref, expected=int(e.amount), actual=int(c.amount.iloc[0])))
    else:
        flags.append(dict(type="missing", bank_id=None, books_ref=e.ref, expected=int(e.amount), actual=0))
for r in bank[~bank.bank_id.isin(used)].itertuples():
    flags.append(dict(type="unexpected", bank_id=r.bank_id, books_ref=None, expected=0, actual=int(r.amount), memo=r.memo))
(D / "flags_baseline.json").write_text(json.dumps(flags, indent=1, default=str))
print(f"bank source: {BANK_SOURCE}")
print(f"expected rows {len(E)}, bank rows {len(bank)}, matched {len(match)}; flags {len(flags)}:",
      pd.Series([f["type"] for f in flags]).value_counts().to_dict())
