import json
import re
import sqlite3

import pytest

CUTOFF = "2026-01-10"          # the historical cutoff most tests use; the snapshot's latest bank date is 2026-02-02


def table(con, name, rows):
    columns = list(rows[0])
    con.execute(f'CREATE TABLE "{name}" ({",".join(columns)})')
    con.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in columns)})',
                    [tuple(row[c] for c in columns) for row in rows])


# Settlement deposits for the matching tests (make_root(..., settlements=True)); kept out of the default root so the
# balance expectations elsewhere stay simple. Card net per batch is 10000 - (260 + 150) = 9590.
SETTLEMENTS = [
    ("c0102", "2026-01-05", 9590, "TABLETAP PAYMENTS DEP 0102"),      # matched (Fri batch, Mon deposit)
    ("c0103", "2026-01-05", 9590, "TABLETAP PAYMENTS DEP 0103"),      # matched
    ("c0104", "2026-01-05", 9500, "TABLETAP PAYMENTS DEP 0104"),      # amount_mismatch -90
    ("c0106", "2026-01-07", 9590, "TABLETAP PAYMENTS DEP 0106"),      # multiple (duplicate below)
    ("c0106b", "2026-01-07", 9590, "TABLETAP PAYMENTS DEP 0106"),
    ("c0107", "2026-01-08", 9585, "TABLETAP PAYMENTS DEP 0107"),      # matched within the 10c tolerance
    ("c0108", "2026-01-09", 9590, "TABLETAP PAYMENTS DEP 0108"),      # matched
    ("c0109", "2026-01-12", 9590, "TABLETAP PAYMENTS DEP 0109"),      # after the cutoff: not_yet_due at 01-10
    ("c1231", "2026-01-06", 777, "TABLETAP PAYMENTS DEP 1231"),       # no such batch: unmatched deposit
    ("b0105a", "2026-01-05", 997, "BRANCH DEPOSIT"),                  # slip 01-03 (Sat), via the window
    ("b0105b", "2026-01-05", 996, "BRANCH DEPOSIT"),                  # slip 01-04 (Sun), via the window
    ("b0105c", "2026-01-05", 995, "BRANCH DEPOSIT"),                  # slip 01-05, same day
    ("b0106", "2026-01-06", 794, "BRANCH DEPOSIT"),                   # slip 01-06 counted 994: $2.00 short
    ("b0108", "2026-01-08", 992, "BRANCH DEPOSIT"),                   # slip 01-08; 01-07, 01-09, 01-10 missing
    ("r1229", "2026-01-06", 1700, "RAPIDPLATE PAYOUT WK 1229"),       # week of 12-29: 2000 less 15%
    ("r1222", "2026-01-02", 500, "RAPIDPLATE PAYOUT WK 1222"),        # no sales that week: unmatched deposit
]


def bank_rows(settlements=False):
    """(nessie_id, account, type, date, amount, status, counterparty, memo)"""
    rows = [
        ("pre", "CHS", "purchase", "2025-12-20", 500, "completed", "Supplier", "ACH SUPPLIER"),
        ("n1", "CHS", "deposit", "2026-01-02", 5000, "completed", None, "BRANCH DEPOSIT"),
        ("n2", "CHS", "purchase", "2026-01-03", 4000, "completed", "Supplier", "ACH SUPPLIER"),
        ("n3", "CHS", "withdrawal", "2026-01-04", 1000, "completed", None, "TRANSFER TO RES XFER-1"),
        ("n4", "RES", "deposit", "2026-01-04", 1000, "completed", None, "TRANSFER FROM CHS XFER-1"),
        ("n5", "CHS", "purchase", "2026-01-05", 9999, "pending", "Supplier", "ACH SUPPLIER"),
        ("n7", "CHS", "purchase", "2026-01-08", 4000, "completed", "Supplier", "ACH SUPPLIER"),
        ("n8", "CHS", "purchase", "2026-01-20", 4000, "completed", "Supplier", "ACH SUPPLIER"),
        ("n6", "CHS", "deposit", "2026-02-02", 6000, "completed", None, "BRANCH DEPOSIT"),
    ]
    rows += [(f"p{i:02d}", "CHS", "deposit", f"2026-01-0{6 + i % 4}", 100, "completed", None, f"TABLETAP PAYMENTS DEP {i:02d}")
             for i in range(60)]
    if settlements:
        rows += [(i, "CHS", "deposit", d, v, "completed", None, memo) for i, d, v, memo in SETTLEMENTS]
    return rows


def make_root(path, books_extra=None, tag="boh-1loc", settlements=False):
    data = path / "data"
    data.mkdir(parents=True)
    (data / "grader").mkdir()
    (data / "grader" / "answer_key.json").write_text('{"items": [{"secret": "never"}]}')
    with sqlite3.connect(data / "books.db") as con:
        table(con, "accounts", [
            dict(account="CHS", nickname="Operating", opening_as_of="2026-01-01", opening_balance_cents=10000),
            dict(account="RES", nickname="Reserve", opening_as_of="2026-01-01", opening_balance_cents=2000)])
        table(con, "locations", [dict(location="CHS", name="Demo Bistro Chestnut")])
        table(con, "agreements", [
            dict(counterparty="TableTap Payments", terms="2.6% + 15c per card transaction; next-business-day settlement"),
            dict(counterparty="RapidPlate", terms="15% commission on delivery subtotal; weekly payout Tuesday")])
        table(con, "suppliers", [dict(supplier="Supplier", category="Food Supply - Dry Goods", terms_days=14, delivery_days="[1, 4]")])
        inv = dict(supplier="Supplier", category="Food Supply - Dry Goods", location="CHS", terms_days=14, status="settled", outstanding=0)
        table(con, "invoices", [
            dict(invoice_no="old", invoice_date="2025-12-01", due_date="2025-12-15", total=500, **inv),
            dict(invoice_no="now", invoice_date="2026-01-02", due_date="2026-01-20", total=4000, **inv),
            dict(invoice_no="bulk", invoice_date="2026-01-05", due_date="2026-01-19", total=5500, **inv),
            dict(invoice_no="future", invoice_date="2026-02-01", due_date="2026-02-10", total=9000, **inv)])
        lines = [dict(invoice_no=k, ingredient="flour", unit="lb", qty=1, unit_price=4.0, line_total=400) for k in ["old", "now", "future"]]
        lines += [dict(invoice_no="bulk", ingredient=f"item_{i}", unit="ea", qty=1, unit_price=1.0, line_total=100) for i in range(55)]
        table(con, "invoice_lines", lines)
        table(con, "credit_memos", [dict(credit_no="CM-9", invoice_no="now", supplier="Supplier", location="CHS", issued="2026-01-15",
                                         amount=700, reason="Short-shipped flour; refunded by ACH")])
        table(con, "ap_payment_plans", [dict(invoice_no="now", installment=1, due="2026-01-20", amount=2000),
                                        dict(invoice_no="now", installment=2, due="2026-02-03", amount=2000)])
        table(con, "open_ap", [dict(doc_type="invoice", doc_id="now", payee="Supplier", location="CHS", doc_date="2026-01-02",
                                    due_date="2026-01-20", total=4000)])
        table(con, "deposits_in_transit", [dict(type="undeposited_cash", location="CHS", source_date="2026-01-10", amount=1234,
                                                note="POS cash not yet deposited")])
        table(con, "vendor_bills", [
            dict(bill_id="VB-CHS-GAS-2026-01", vendor="City Gas", category="Utilities", location="CHS", bill_date="2026-01-01",
                 due_date="2026-01-08", amount=3000, autopay=1),
            dict(bill_id="VB-CHS-GAS-2026-02", vendor="City Gas", category="Utilities", location="CHS", bill_date="2026-02-01",
                 due_date="2026-02-08", amount=3100, autopay=1)])
        table(con, "repair_invoices", [dict(repair_id="RPR-1", location="CHS", date="2026-01-04", vendor="Bay Kitchen Repair",
                                            amount=800, description="Service call")])
        table(con, "misc_payables", [dict(misc_id="MISC-1", location="CHS", date="2026-01-30", payee="SF DPH", amount=1100,
                                          description="Health permit")])
        table(con, "leases", [dict(location="CHS", landlord="Landlord LLC", base=100000, escalator_pct=0.03, anniversary="2027-01-01")])
        pay = dict(location="CHS", hours=10.0, hourly_wages=1000, salaries=500, tips_paid=100, employer_taxes_benefits=50, total_debit=1650)
        table(con, "payroll_runs", [dict(pay_date="2026-01-09", period_start="2025-12-22", period_end="2026-01-04", **pay),
                                    dict(pay_date="2026-01-23", period_start="2026-01-05", period_end="2026-01-18", **pay)])
        table(con, "loans", [dict(lender="Lender", type="SBA", original_cents=100000, balance_at_start_cents=90000, rate=0.085,
                                  monthly_cents=2250, pay_day=20, account="CHS")])
        table(con, "loan_schedule", [dict(due="2026-01-20", payment=2250, interest=600, principal=1650, balance_after=88350),
                                     dict(due="2026-02-20", payment=2250, interest=590, principal=1660, balance_after=86690)])
        table(con, "sales_tax_returns", [dict(period="2025-12", taxable_sales=10000, marketplace_sales_excluded=0, tax_due=200,
                                              due_date="2026-01-20", paid_date="2026-01-20")])
        table(con, "owner_draws", [dict(date="2026-01-05", amount=700, from_account="CHS"),
                                   dict(date="2026-02-25", amount=700, from_account="CHS")])
        table(con, "processor_disputes", [dict(case_id="CB-1", location="CHS", opened="2026-01-06", debited="2026-01-14", amount=300,
                                               outcome="lost")])
        table(con, "pos_card_batches", [dict(location="CHS", batch_date=f"2026-01-{d:02d}", gross_sales=10000, transactions=10, refunds=0)
                                        for d in range(2, 13)])
        table(con, "cash_deposit_slips", [dict(location="CHS", deposit_date=f"2026-01-{d:02d}", pos_cash=1000, counted_cash=1000 - d)
                                          for d in range(3, 13)])
        ticket = dict(location="CHS", hour=19, service="dinner", party_size=2, gross=0, discount=0, discount_reason=None, tax=0, tip=0)
        table(con, "tickets", [
            dict(ticket_id="t1", date="2026-01-02", channel="dine_in", tender="card", subtotal=5000, **ticket),
            dict(ticket_id="t2", date="2026-02-01", channel="dine_in", tender="card", subtotal=5000, **ticket),
            dict(ticket_id="d1", date="2025-12-30", channel="RapidPlate", tender="platform", subtotal=2000, **ticket),
            dict(ticket_id="d2", date="2026-01-06", channel="RapidPlate", tender="platform", subtotal=3000, **ticket),
            dict(ticket_id="d3", date="2026-01-09", channel="RapidPlate", tender="platform", subtotal=1000, **ticket),
            dict(ticket_id="d4", date="2026-01-11", channel="RapidPlate", tender="platform", subtotal=9000, **ticket)])
        table(con, "ticket_lines", [dict(ticket_id="t1", item="Pasta"), dict(ticket_id="t2", item="Pizza")])
        table(con, "answer_key", [dict(secret="never")])
        for name, rows in (books_extra or {}).items():
            table(con, name, rows)
    with sqlite3.connect(data / f"nessie_sync_{tag}.db") as con:
        table(con, "bank_tx", [dict(nessie_id=i, account=a, nessie_type=t, date=d, amount=v, status=s, counterparty=cp,
                                    memo=f"{memo} |ref=TSECRET{i}", bank_id="T" + i)
                               for i, a, t, d, v, s, cp, memo in bank_rows(settlements)])
        table(con, "bank_bills", [dict(account="CHS", _id="bill-1", status="pending", payee="Supplier [SIM]",
                                       nickname="now due 2026-01-20", creation_date="2026-02-03", payment_date="2026-01-20",
                                       recurring_date="20", upcoming_payment_date="2026-01-20", payment_amount="4000.0",
                                       account_id="acct")])
    return path


@pytest.fixture
def root(tmp_path):
    return make_root(tmp_path / "root")


@pytest.fixture
def draft():
    return dict(coverage=dict(bank_rows_reviewed=6, matched_to_documents=3, investigated=3, cleared_as_ok=0),
        summary=dict(errors=0, needs_owner=1, total_impact_cents=4000, recoverable_cents=0, ongoing_monthly_cents=0),
        findings=[dict(id="F1", root_cause="unrecorded_charge", verdict="needs_owner", confidence=0.8,
            counterparty="Supplier", dates=dict(first="2026-01-03", last="2026-01-03"),
            bank_ids=["n2"], books_refs=[], amounts=dict(expected_cents=0, actual_cents=4000, impact_cents=4000),
            explanation="A $40 payment needs documentation.", evidence=[dict(type="bank_row", ref="n2")],
            treatment=dict(kind="exclude", bank_ids=["n2"]), recoverable="unknown",
            assumption_if_unconfirmed="One-off expense", suggested_action="Ask for the receipt.")],
        cleared=[], limitations=[])


def tool_call(i, name, args):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def batch_of(messages):
    return re.search(r"\nBatch: (.+)\n", messages[0]["content"]).group(1)


def batch_result(draft=None):
    if draft is None:
        return dict(coverage=dict(bank_rows_reviewed=0, matched_to_documents=0, investigated=0, cleared_as_ok=0),
                    findings=[], cleared=[], limitations=[])
    return {k: draft[k] for k in ("coverage", "findings", "cleared", "limitations")}


class ScriptedClient:
    """Plays every batch: the investigating batch makes one round of tool calls, then each batch returns its result.

    The draft's findings come from `investigator` (the unrecorded-charges batch by default); other batches find nothing.
    """

    CALLS = [("search_bank_transactions", {"limit": 50}), ("search_costs", {"cost_type": "supplier_invoice"})]

    def __init__(self, draft, calls=None, investigator="unrecorded_charges"):
        self.calls = 0
        self.draft = draft
        self.script = calls or self.CALLS
        self.investigator = investigator
        self.seen_tools = []               # (batch, tool names offered) per call

    async def complete(self, messages, tools):
        self.calls += 1
        batch = batch_of(messages)
        self.seen_tools.append((batch, [t["function"]["name"] for t in tools]))
        if tools and batch == self.investigator and len(messages) == 2:
            return {"role": "assistant", "content": None,
                    "tool_calls": [tool_call(i, name, args) for i, (name, args) in enumerate(self.script)]}
        return {"role": "assistant", "content": json.dumps(batch_result(self.draft if batch == self.investigator else None))}
