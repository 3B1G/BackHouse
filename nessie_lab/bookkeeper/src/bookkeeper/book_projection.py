"""Project the restaurant's cost documents into the Book: one row per cost, with line items and a payment status.

Pure function of the source tables (pandas frames as read from books.db); nothing is read or written here.

  policy="book"        what the books say on the day they close (build_book.py, standalone tools): open AP is pending,
                       autopays and other recorded costs are paid, future lease/loan obligations are scheduled.
  policy="historical"  what a dated record proves at a cutoff (Bookkeeper --as-of): documents after the cutoff are
                       dropped first, credits and descriptions are rebuilt from what remains, and a cost is paid only
                       when a payment record dated on or before the cutoff exists. Otherwise its status is unknown, or
                       scheduled for a contractual obligation that falls due after the cutoff. Never uses open AP,
                       deposits in transit or autopay assumptions.

costs       cost_id, cost_type, category, payee, location, account, doc_date, due_date, amount_cents,
            credit_cents, terms_days, installments, payment_method, status, status_basis, description, source_table
cost_lines  cost_id, line_no, item, description, qty, unit, unit_price_dollars, line_total_cents
"""
from datetime import date

import pandas as pd

TABLES = ["locations", "invoices", "invoice_lines", "credit_memos", "ap_payment_plans", "vendor_bills", "repair_invoices",
          "misc_payables", "leases", "payroll_runs", "loans", "loan_schedule", "sales_tax_returns", "owner_draws",
          "processor_disputes"]
OPTIONAL = ["open_ap", "invoice_payments"]                 # open_ap: book policy only; invoice_payments: when the books have it
COST_COLUMNS = ["cost_id", "cost_type", "category", "payee", "location", "account", "doc_date", "due_date", "amount_cents",
                "credit_cents", "terms_days", "installments", "payment_method", "status", "status_basis", "description",
                "source_table"]
LINE_COLUMNS = ["cost_id", "line_no", "item", "description", "qty", "unit", "unit_price_dollars", "line_total_cents"]


def money(c):
    return f"${c / 100:,.2f}"


def qty(x):
    return f"{x:g}"


def _as_date(v):
    if v is None or isinstance(v, date):
        return v
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return pd.Timestamp(v).date()


def _dated(tables):
    """Copies of the source frames with date columns as datetime.date (None when missing)."""
    out = {}
    for name, df in tables.items():
        df = df.copy()
        for c in df.columns:
            if c in ("date", "due", "issued", "opened", "debited", "anniversary") or c.endswith("_date"):
                df[c] = pd.Series([_as_date(v) for v in df[c]], index=df.index, dtype=object)
        out[name] = df
    return out


def _empty(columns):
    return pd.DataFrame(columns=columns)


def project_costs(tables, cutoff, policy="book", window_start=None, sales_tax_rate=None):
    """Return (costs, cost_lines) as of `cutoff` (the day the books close for policy="book").

    tables: {name: DataFrame} for TABLES (+ OPTIONAL when present). window_start: first month of rent to expand
    (default: the earliest account opening date, else the earliest document). sales_tax_rate: only used in descriptions.
    """
    if policy not in ("book", "historical"):
        raise ValueError(f"unknown policy {policy!r}")
    end = _as_date(cutoff)
    historical = policy == "historical"
    B = _dated({t: tables[t] if t in tables else _empty([]) for t in TABLES + [t for t in OPTIONAL if t in tables]})
    start = _as_date(window_start) if window_start else _default_start(tables, B)
    site = {r.location: r.name.replace("Demo Bistro ", "") for r in B["locations"].itertuples()}
    open_ap = set(B["open_ap"].doc_id) if not historical and "open_ap" in B else set()
    costs, lines = [], []

    def add(cost_id, cost_type, category, payee, location, account, doc_date, due_date, amount, status, status_basis, description,
            source_table, items, payment_method, terms_days=None, credit=0, installments=1):
        """items: (item, description, qty, unit, unit_price_dollars, line_total_cents)"""
        costs.append(dict(cost_id=cost_id, cost_type=cost_type, category=category, payee=payee, location=location, account=account,
                          doc_date=str(doc_date), due_date=None if due_date is None else str(due_date), amount_cents=int(amount),
                          credit_cents=int(credit), terms_days=terms_days, installments=installments, payment_method=payment_method,
                          status=status, status_basis=status_basis, description=description, source_table=source_table))
        for n, (item, desc, q, unit, price, total) in enumerate(items, 1):
            lines.append(dict(cost_id=cost_id, line_no=n, item=item, description=desc, qty=q, unit=unit,
                              unit_price_dollars=price, line_total_cents=int(total)))

    def billed_status(doc_id):
        if doc_id in open_ap:
            return "pending", f"open AP: billed, not yet paid on {end}"
        return "paid", "autopay on the due date; the books treat it as paid (the bank is the proof)"

    def unknown(due):
        if due is not None and due > end:
            return "unknown", f"not yet due at the {end} cutoff; no payment record by then"
        return "unknown", f"no payment record on or before the {end} cutoff (autopay and open AP are not evidence)"

    def upto(df, col):
        if not historical or col not in df:
            return df
        return df[[d is not None and d <= end for d in df[col]]]

    # supplier invoices: line items, credit memos, payment plans
    invoices = upto(B["invoices"], "invoice_date")
    credit_memos = upto(B["credit_memos"], "issued")
    paid_by = {}
    if historical and "invoice_payments" in B:
        p = upto(B["invoice_payments"], "payment_date")
        paid_by = p.groupby("invoice_no").amount.sum().to_dict() if len(p) else {}
    lines_by = dict(tuple(B["invoice_lines"].groupby("invoice_no", sort=False))) if len(B["invoice_lines"]) else {}
    cm_by = dict(tuple(credit_memos.groupby("invoice_no"))) if len(credit_memos) else {}
    plan_by = dict(tuple(B["ap_payment_plans"].groupby("invoice_no"))) if len(B["ap_payment_plans"]) else {}
    for r in invoices.itertuples():
        ln = lines_by.get(r.invoice_no, _empty(["ingredient", "qty", "unit", "unit_price", "line_total"]))
        goods = ", ".join(f"{g.replace('_', ' ')} {qty(q)} {u}" for g, q, u in zip(ln.ingredient, ln.qty, ln.unit))
        if len(ln) > 5:
            goods = ", ".join(goods.split(", ")[:5]) + f" and {len(ln) - 5} more"
        desc = f"{r.supplier} invoice {r.invoice_no} for {site.get(r.location, r.location)}: {goods}; net {r.terms_days} days"
        plan, cms = plan_by.get(r.invoice_no), cm_by.get(r.invoice_no)
        if plan is not None:
            desc += "; paid in installments: " + ", ".join(f"{money(p.amount)} due {p.due}" for p in plan.itertuples())
        if cms is not None:
            desc += "; " + "; ".join(f"credit memo {m.credit_no} for {money(m.amount)} issued {m.issued} ({m.reason})" for m in cms.itertuples())
        if not historical:
            status = billed_status(r.invoice_no)
        elif "invoice_payments" in B:
            got = int(paid_by.get(r.invoice_no, 0))
            status = (("paid", f"recorded payments of {money(got)} by {end} cover the invoice") if got >= r.total else
                      ("pending", f"recorded payments by {end}: {money(got)} of {money(r.total)}"))
        else:
            status = unknown(r.due_date)
        add(r.invoice_no, "supplier_invoice", r.category, r.supplier, r.location, r.location, r.invoice_date, r.due_date, r.total,
            *status, desc, "invoices",
            [(g, f"{g} from {r.supplier}", float(q), u, float(p), t) for g, q, u, p, t in zip(ln.ingredient, ln.qty, ln.unit, ln.unit_price, ln.line_total)],
            "ACH on the due date", terms_days=int(r.terms_days), credit=0 if cms is None else int(cms.amount.sum()),
            installments=1 if plan is None else len(plan))

    # recurring vendor bills (utilities, software, linen, insurance, ...)
    for r in upto(B["vendor_bills"], "bill_date").itertuples():
        month = r.bill_id.rsplit("-", 2)[-2] + "-" + r.bill_id.rsplit("-", 1)[-1]
        add(r.bill_id, "vendor_bill", r.category, r.vendor, r.location, r.location, r.bill_date, r.due_date, r.amount,
            *(unknown(r.due_date) if historical else billed_status(r.bill_id)),
            f"{r.vendor}: {r.category.lower()} bill for {site.get(r.location, r.location)}, billing month {month}"
            + (", paid by autopay" if r.autopay and not historical else ", set up for autopay" if r.autopay else ""), "vendor_bills",
            [(r.category, f"{r.vendor} service, billing month {month}", 1.0, "bill", r.amount / 100, r.amount)],
            "autopay on the due date" if r.autopay else "manual payment", terms_days=(r.due_date - r.bill_date).days)

    for r in upto(B["repair_invoices"], "date").itertuples():
        add(r.repair_id, "repair", "Repairs & Maintenance", r.vendor, r.location, r.location, r.date, r.date, r.amount,
            *(unknown(r.date) if historical else ("paid", "paid on the service date") if r.date <= end else
              ("scheduled", "service booked after the books close")),
            f"{r.vendor}: {r.description} at {site.get(r.location, r.location)} on {r.date}", "repair_invoices",
            [("repair", r.description, 1.0, "job", r.amount / 100, r.amount)], "paid on the service date")

    for r in upto(B["misc_payables"], "date").itertuples():
        add(r.misc_id, "misc_payable", "Licenses & Permits", r.payee, r.location, r.location, r.date, r.date, r.amount,
            *(unknown(r.date) if historical else ("paid", "paid on the document date") if r.date <= end else
              ("scheduled", "due after the books close")),
            f"{r.payee}: {r.description} for {site.get(r.location, r.location)}", "misc_payables",
            [("permit", r.description, 1.0, "fee", r.amount / 100, r.amount)], "paid on the document date")

    # rent: expanded monthly from the lease for the bank window, plus next month's rent (not yet due)
    for L in B["leases"].itertuples():
        months = pd.period_range(start, end, freq="M").append(pd.PeriodIndex([pd.Period(end, "M") + 1]))
        for ym in months:
            d1 = ym.start_time.date()
            if d1 < start:
                continue
            esc = int(round(L.base * L.escalator_pct)) if d1 >= L.anniversary else 0
            items = [("base rent", f"base rent incl. NNN/CAM, {ym}", 1.0, "month", L.base / 100, L.base)]
            if esc:
                items.append(("rent escalator", f"{L.escalator_pct:.0%} annual escalator since {L.anniversary}", 1.0, "month", esc / 100, esc))
            if d1 > end:
                status = ("scheduled", "next month's rent per the lease, not yet due")
            else:
                status = unknown(d1) if historical else ("paid", "monthly rent per the lease, due on the 1st")
            add(f"RENT-{L.location}-{ym}", "rent", "Occupancy", L.landlord, L.location, L.location, d1, d1, L.base + esc, *status,
                f"Rent incl. NNN for {site.get(L.location, L.location)}, {ym}, to {L.landlord}"
                + (f" (includes the {L.escalator_pct:.0%} escalator)" if esc else ""), "leases", items, "ACH on the 1st (next business day)")

    for r in upto(B["payroll_runs"], "pay_date").itertuples():
        items = [("hourly wages", f"{r.hours:g} hourly staff hours", float(r.hours), "hours", r.hourly_wages / r.hours / 100, r.hourly_wages),
                 ("salaries", "GM and sous chef salaries", 1.0, "period", r.salaries / 100, r.salaries),
                 ("tips paid out", "guest tips passed through to staff", 1.0, "period", r.tips_paid / 100, r.tips_paid),
                 ("employer taxes and benefits", "FICA, FUTA/SUI, workers comp, SF HCSO", 1.0, "period",
                  r.employer_taxes_benefits / 100, r.employer_taxes_benefits)]
        add(f"PAY-{r.location}-{r.pay_date:%Y%m%d}", "payroll", "Labor", "BayPay payroll", r.location, r.location, r.period_end, r.pay_date,
            r.total_debit, "paid", f"payroll run on {r.pay_date}",
            f"Biweekly payroll for {site.get(r.location, r.location)}, {r.period_start} to {r.period_end}: wages {money(r.hourly_wages)} "
            f"({r.hours:g} h), salaries {money(r.salaries)}, tips {money(r.tips_paid)}, employer taxes and benefits "
            f"{money(r.employer_taxes_benefits)}", "payroll_runs", items, "payroll provider debit on pay day")

    if len(B["loans"]):
        loan = B["loans"].iloc[0]
        for r in B["loan_schedule"].itertuples():
            if r.due > end:
                status = ("scheduled", "future installment on the loan schedule")
            else:
                status = unknown(r.due) if historical else ("paid", f"loan autodebit due {r.due}")
            add(f"LOAN-{r.due:%Y-%m}", "loan_payment", "Debt Service", loan.lender, loan.account, loan.account, r.due, r.due, r.payment,
                *status, f"{loan.type} loan payment to {loan.lender}: interest {money(r.interest)} + principal {money(r.principal)}, "
                f"balance after {money(r.balance_after)}", "loan_schedule",
                [("interest", f"{loan.rate:.1%} a year on the outstanding balance", 1.0, "month", r.interest / 100, r.interest),
                 ("principal", "reduces the loan balance", 1.0, "month", r.principal / 100, r.principal)], "lender autodebit")

    returns = B["sales_tax_returns"]
    if historical and "period" in returns:
        returns = returns[[p < f"{end:%Y-%m}" for p in returns.period]]
    for r in returns.itertuples():
        tid = f"TAX-{r.period}"
        paid = r.paid_date if r.paid_date is not None and (not historical or r.paid_date <= end) else None
        if paid is not None:
            status = ("paid", f"return paid {paid}")
        elif historical:
            status = ("scheduled", f"return due {r.due_date}") if r.due_date > end else unknown(r.due_date)
        else:
            status = ("pending", f"open AP: return due {r.due_date}") if tid in open_ap else ("scheduled", f"return due {r.due_date}")
        rate = f"{sales_tax_rate:.3%} " if sales_tax_rate is not None else ""
        add(tid, "sales_tax", "Sales Tax (collected from guests)", "CDTFA", "CHS", "CHS", r.due_date, r.due_date, r.tax_due, *status,
            f"Sales tax return for {r.period}: {rate}on taxable sales of {money(r.taxable_sales)} "
            f"({money(r.marketplace_sales_excluded)} of delivery-platform sales excluded: the platforms remit that tax)", "sales_tax_returns",
            [("sales tax", f"tax collected on {r.period} sales", 1.0, "return", r.tax_due / 100, r.tax_due)], "CDTFA EFT on the due date")

    for r in upto(B["owner_draws"], "date").itertuples():
        add(f"DRAW-{r.date:%Y-%m}", "owner_draw", "Owner Draw (equity, not an expense)", "Owner", r.from_account, r.from_account, r.date,
            r.date, r.amount, *(("paid", f"owner draw recorded {r.date}") if r.date <= end else ("scheduled", "planned owner draw")),
            "Monthly owner draw to the owner's personal account (the owner-chef takes a draw instead of a salary)", "owner_draws",
            [("owner draw", "transfer to personal account", 1.0, "month", r.amount / 100, r.amount)], "transfer to personal account")

    for r in upto(B["processor_disputes"], "opened").itertuples():
        settled = r.debited is not None and (not historical or r.debited <= end)
        if settled:
            status = ("paid", f"processor debited {r.debited} after the dispute was {r.outcome}")
            desc = f"dispute opened {r.opened}, debited {r.debited}, outcome {r.outcome}"
        else:
            status = ("unknown", f"dispute open at the {end} cutoff; no processor debit recorded by then")
            desc = f"dispute opened {r.opened}; outcome not known at the {end} cutoff"
        add(r.case_id, "chargeback", "Card Chargebacks", "TableTap Payments", r.location, r.location, r.opened,
            r.debited if settled else None, r.amount, *status,
            f"Card chargeback {r.case_id} at {site.get(r.location, r.location)}: {desc}", "processor_disputes",
            [("chargeback", f"disputed card sale, case {r.case_id}", 1.0, "case", r.amount / 100, r.amount)], "processor debit")

    COSTS = pd.DataFrame(costs, columns=COST_COLUMNS)
    COSTS = COSTS.sort_values(["due_date", "cost_id"], na_position="last").reset_index(drop=True)
    return COSTS, pd.DataFrame(lines, columns=LINE_COLUMNS)


def _default_start(tables, B):
    acc = tables.get("accounts")
    if acc is not None and len(acc):
        return _as_date(min(acc.opening_as_of))
    dates = [d for df in B.values() for c in df.columns if c.endswith("date") for d in df[c] if d is not None]
    return min(dates) if dates else date.today()
