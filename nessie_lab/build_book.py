"""Build the Book: one row per cost the restaurant has paid or still owes, with line items and status.

  python build_book.py        # run after generate.py; adds tables `costs` and `cost_lines` to data/books.db

Reads only the restaurant's own documents in books.db. Never reads bank_feed.csv, Nessie or data/grader/:
`status` is what the books say, and the bank is the proof. An autopay invoice the books treat as paid
stays "paid" here even if the bank never debited it; finding that gap is the Bookkeeper's job.

costs       cost_id, cost_type, category, payee, location, account, doc_date, due_date, amount_cents,
            credit_cents, terms_days, installments, payment_method, status, status_basis, description, source_table
cost_lines  cost_id, line_no, item, description, qty, unit, unit_price_dollars, line_total_cents

status      paid       the books record it as paid (autopay on the due date, or a recorded payment)
            pending    billed but unpaid when the books close (the open_ap table; matches Nessie's pending bills)
            scheduled  a future obligation from a contract (lease, loan schedule) with no bill yet
IDs are the books' own references (invoice_no, bill_id, RENT-CHS-2026-10, PAY-CHS-20261016, LOAN-2026-10, TAX-2026-09,
DRAW-2026-10, ...), the same ones grade.py accepts as books_ref. Amounts are integer cents; sum of lines = amount_cents.
"""
import sqlite3
from pathlib import Path

import pandas as pd

import config as C

DB = Path(__file__).parent / "data" / "books.db"
END = C.END


def money(c):
    return f"${c / 100:,.2f}"


def qty(x):
    return f"{x:g}"


con = sqlite3.connect(DB)
B = {t: pd.read_sql(f"select * from {t}", con) for t in
     ["locations", "invoices", "invoice_lines", "credit_memos", "ap_payment_plans", "open_ap", "vendor_bills", "repair_invoices",
      "misc_payables", "leases", "payroll_runs", "loans", "loan_schedule", "sales_tax_returns", "owner_draws", "processor_disputes"]}
for df in B.values():
    for c in df.columns:
        if c in ("date", "due", "issued", "opened", "debited", "anniversary") or c.endswith("_date"):
            df[c] = pd.to_datetime(df[c]).dt.date
site = {r.location: r.name.replace("Demo Bistro ", "") for r in B["locations"].itertuples()}
open_ap = set(B["open_ap"].doc_id)
costs, lines = [], []


def add(cost_id, cost_type, category, payee, location, account, doc_date, due_date, amount, status, status_basis, description,
        source_table, items, payment_method, terms_days=None, credit=0, installments=1):
    """items: (item, description, qty, unit, unit_price_dollars, line_total_cents)"""
    costs.append(dict(cost_id=cost_id, cost_type=cost_type, category=category, payee=payee, location=location, account=account,
                      doc_date=str(doc_date), due_date=str(due_date), amount_cents=int(amount), credit_cents=int(credit),
                      terms_days=terms_days, installments=installments, payment_method=payment_method, status=status,
                      status_basis=status_basis, description=description, source_table=source_table))
    for n, (item, desc, q, unit, price, total) in enumerate(items, 1):
        lines.append(dict(cost_id=cost_id, line_no=n, item=item, description=desc, qty=q, unit=unit,
                          unit_price_dollars=price, line_total_cents=int(total)))


def billed_status(doc_id):
    if doc_id in open_ap:
        return "pending", f"open AP: billed, not yet paid on {END}"
    return "paid", "autopay on the due date; the books treat it as paid (the bank is the proof)"


# supplier invoices: line items, credit memos, payment plans
lines_by = dict(tuple(B["invoice_lines"].groupby("invoice_no", sort=False)))
cm_by = dict(tuple(B["credit_memos"].groupby("invoice_no")))
plan_by = dict(tuple(B["ap_payment_plans"].groupby("invoice_no")))
for r in B["invoices"].itertuples():
    ln = lines_by[r.invoice_no]
    goods = ", ".join(f"{g.replace('_', ' ')} {qty(q)} {u}" for g, q, u in zip(ln.ingredient, ln.qty, ln.unit))
    if len(ln) > 5:
        goods = ", ".join(goods.split(", ")[:5]) + f" and {len(ln) - 5} more"
    desc = f"{r.supplier} invoice {r.invoice_no} for {site[r.location]}: {goods}; net {r.terms_days} days"
    plan, cms = plan_by.get(r.invoice_no), cm_by.get(r.invoice_no)
    if plan is not None:
        desc += "; paid in installments: " + ", ".join(f"{money(p.amount)} due {p.due}" for p in plan.itertuples())
    if cms is not None:
        desc += "; " + "; ".join(f"credit memo {m.credit_no} for {money(m.amount)} issued {m.issued} ({m.reason})" for m in cms.itertuples())
    add(r.invoice_no, "supplier_invoice", r.category, r.supplier, r.location, r.location, r.invoice_date, r.due_date, r.total,
        *billed_status(r.invoice_no), desc, "invoices",
        [(g, f"{g} from {r.supplier}", float(q), u, float(p), t) for g, q, u, p, t in zip(ln.ingredient, ln.qty, ln.unit, ln.unit_price, ln.line_total)],
        "ACH on the due date", terms_days=int(r.terms_days), credit=0 if cms is None else int(cms.amount.sum()),
        installments=1 if plan is None else len(plan))

# recurring vendor bills (utilities, software, linen, insurance, ...)
for r in B["vendor_bills"].itertuples():
    month = r.bill_id.rsplit("-", 2)[-2] + "-" + r.bill_id.rsplit("-", 1)[-1]
    add(r.bill_id, "vendor_bill", r.category, r.vendor, r.location, r.location, r.bill_date, r.due_date, r.amount,
        *billed_status(r.bill_id), f"{r.vendor}: {r.category.lower()} bill for {site[r.location]}, billing month {month}"
        + (", paid by autopay" if r.autopay else ""), "vendor_bills",
        [(r.category, f"{r.vendor} service, billing month {month}", 1.0, "bill", r.amount / 100, r.amount)],
        "autopay on the due date" if r.autopay else "manual payment", terms_days=(r.due_date - r.bill_date).days)

for r in B["repair_invoices"].itertuples():
    add(r.repair_id, "repair", "Repairs & Maintenance", r.vendor, r.location, r.location, r.date, r.date, r.amount,
        *(("paid", "paid on the service date") if r.date <= END else ("scheduled", "service booked after the books close")),
        f"{r.vendor}: {r.description} at {site[r.location]} on {r.date}", "repair_invoices",
        [("repair", r.description, 1.0, "job", r.amount / 100, r.amount)], "paid on the service date")

for r in B["misc_payables"].itertuples():
    add(r.misc_id, "misc_payable", "Licenses & Permits", r.payee, r.location, r.location, r.date, r.date, r.amount,
        *(("paid", "paid on the document date") if r.date <= END else ("scheduled", "due after the books close")),
        f"{r.payee}: {r.description} for {site[r.location]}", "misc_payables",
        [("permit", r.description, 1.0, "fee", r.amount / 100, r.amount)], "paid on the document date")

# rent: expanded monthly from the lease for the bank window, plus next month's rent (not yet due)
for L in B["leases"].itertuples():
    months = pd.period_range(C.START, END, freq="M").append(pd.PeriodIndex([pd.Period(END, "M") + 1]))
    for ym in months:
        d1 = ym.start_time.date()
        if d1 < C.START:
            continue
        esc = int(round(L.base * L.escalator_pct)) if d1 >= L.anniversary else 0
        items = [("base rent", f"base rent incl. NNN/CAM, {ym}", 1.0, "month", L.base / 100, L.base)]
        if esc:
            items.append(("rent escalator", f"{L.escalator_pct:.0%} annual escalator since {L.anniversary}", 1.0, "month", esc / 100, esc))
        add(f"RENT-{L.location}-{ym}", "rent", "Occupancy", L.landlord, L.location, L.location, d1, d1, L.base + esc,
            *(("paid", "monthly rent per the lease, due on the 1st") if d1 <= END else ("scheduled", "next month's rent per the lease, not yet due")),
            f"Rent incl. NNN for {site[L.location]}, {ym}, to {L.landlord}" + (f" (includes the {L.escalator_pct:.0%} escalator)" if esc else ""),
            "leases", items, "ACH on the 1st (next business day)")

for r in B["payroll_runs"].itertuples():
    items = [("hourly wages", f"{r.hours:g} hourly staff hours", float(r.hours), "hours", r.hourly_wages / r.hours / 100, r.hourly_wages),
             ("salaries", "GM and sous chef salaries", 1.0, "period", r.salaries / 100, r.salaries),
             ("tips paid out", "guest tips passed through to staff", 1.0, "period", r.tips_paid / 100, r.tips_paid),
             ("employer taxes and benefits", "FICA, FUTA/SUI, workers comp, SF HCSO", 1.0, "period",
              r.employer_taxes_benefits / 100, r.employer_taxes_benefits)]
    add(f"PAY-{r.location}-{r.pay_date:%Y%m%d}", "payroll", "Labor", "BayPay payroll", r.location, r.location, r.period_end, r.pay_date,
        r.total_debit, "paid", f"payroll run on {r.pay_date}",
        f"Biweekly payroll for {site[r.location]}, {r.period_start} to {r.period_end}: wages {money(r.hourly_wages)} ({r.hours:g} h), "
        f"salaries {money(r.salaries)}, tips {money(r.tips_paid)}, employer taxes and benefits {money(r.employer_taxes_benefits)}",
        "payroll_runs", items, "payroll provider debit on pay day")

loan = B["loans"].iloc[0]
for r in B["loan_schedule"].itertuples():
    add(f"LOAN-{r.due:%Y-%m}", "loan_payment", "Debt Service", loan.lender, loan.account, loan.account, r.due, r.due, r.payment,
        *(("paid", f"loan autodebit due {r.due}") if r.due <= END else ("scheduled", "future installment on the loan schedule")),
        f"{loan.type} loan payment to {loan.lender}: interest {money(r.interest)} + principal {money(r.principal)}, "
        f"balance after {money(r.balance_after)}", "loan_schedule",
        [("interest", f"{loan.rate:.1%} a year on the outstanding balance", 1.0, "month", r.interest / 100, r.interest),
         ("principal", "reduces the loan balance", 1.0, "month", r.principal / 100, r.principal)], "lender autodebit")

for r in B["sales_tax_returns"].itertuples():
    tid = f"TAX-{r.period}"
    status = ("paid", f"return paid {r.paid_date}") if pd.notna(r.paid_date) else \
             ("pending", f"open AP: return due {r.due_date}") if tid in open_ap else ("scheduled", f"return due {r.due_date}")
    add(tid, "sales_tax", "Sales Tax (collected from guests)", "CDTFA", "CHS", "CHS", r.due_date, r.due_date, r.tax_due, *status,
        f"Sales tax return for {r.period}: {C.SALES_TAX:.3%} on taxable sales of {money(r.taxable_sales)} "
        f"({money(r.marketplace_sales_excluded)} of delivery-platform sales excluded: the platforms remit that tax)", "sales_tax_returns",
        [("sales tax", f"tax collected on {r.period} sales", 1.0, "return", r.tax_due / 100, r.tax_due)], "CDTFA EFT on the due date")

for r in B["owner_draws"].itertuples():
    add(f"DRAW-{r.date:%Y-%m}", "owner_draw", "Owner Draw (equity, not an expense)", "Owner", r.from_account, r.from_account, r.date, r.date,
        r.amount, *(("paid", f"owner draw recorded {r.date}") if r.date <= END else ("scheduled", "planned owner draw")),
        "Monthly owner draw to the owner's personal account (the owner-chef takes a draw instead of a salary)", "owner_draws",
        [("owner draw", "transfer to personal account", 1.0, "month", r.amount / 100, r.amount)], "transfer to personal account")

for r in B["processor_disputes"].itertuples():
    add(r.case_id, "chargeback", "Card Chargebacks", "TableTap Payments", r.location, r.location, r.opened, r.debited, r.amount,
        "paid", f"processor debited {r.debited} after the dispute was {r.outcome}",
        f"Card chargeback {r.case_id} at {site[r.location]}: dispute opened {r.opened}, debited {r.debited}, outcome {r.outcome}",
        "processor_disputes", [("chargeback", f"disputed card sale, case {r.case_id}", 1.0, "case", r.amount / 100, r.amount)],
        "processor debit")

COSTS = pd.DataFrame(costs).sort_values(["due_date", "cost_id"]).reset_index(drop=True)
LINES = pd.DataFrame(lines)

# checks: unique ids, lines add up, pending == open_ap, nothing dropped from the source tables
assert COSTS.cost_id.is_unique, COSTS[COSTS.cost_id.duplicated()].cost_id.tolist()
diff = LINES.groupby("cost_id").line_total_cents.sum().reindex(COSTS.cost_id).to_numpy() - COSTS.amount_cents.to_numpy()
assert not diff.any(), COSTS.cost_id[diff != 0].tolist()
assert set(COSTS[COSTS.status == "pending"].cost_id) == open_ap, open_ap ^ set(COSTS[COSTS.status == "pending"].cost_id)
for t, col, n in [("invoices", "total", "supplier_invoice"), ("vendor_bills", "amount", "vendor_bill"), ("payroll_runs", "total_debit", "payroll"),
                  ("loan_schedule", "payment", "loan_payment"), ("sales_tax_returns", "tax_due", "sales_tax")]:
    assert B[t][col].sum() == COSTS[COSTS.cost_type == n].amount_cents.sum(), t

con.execute("drop table if exists costs")
con.execute("drop table if exists cost_lines")
COSTS.to_sql("costs", con, index=False)
LINES.to_sql("cost_lines", con, index=False)
con.execute("create index ix_cost_lines on cost_lines(cost_id)")
con.commit()
con.close()

summary = COSTS.groupby(["cost_type", "status"]).agg(n=("cost_id", "size"), total=("amount_cents", "sum"))
summary["total"] = summary.total.map(money)
print(summary.to_string())
by = COSTS.groupby("status").amount_cents.sum()
print(f"costs {len(COSTS)}, lines {len(LINES)} | pending {money(by.get('pending', 0))} | scheduled {money(by.get('scheduled', 0))}")
