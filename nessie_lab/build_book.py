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
import sys
from pathlib import Path

import pandas as pd

import config as C

sys.path.insert(0, str(Path(__file__).parent / "bookkeeper" / "src"))
from bookkeeper.book_projection import TABLES, money, project_costs  # noqa: E402  (the one implementation, shared with Bookkeeper)

DB = Path(__file__).parent / "data" / "books.db"
END = C.END

con = sqlite3.connect(DB)
B = {t: pd.read_sql(f"select * from {t}", con) for t in TABLES + ["open_ap"]}
open_ap = set(B["open_ap"].doc_id)
COSTS, LINES = project_costs(B, END, policy="book", window_start=C.START, sales_tax_rate=C.SALES_TAX)

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
