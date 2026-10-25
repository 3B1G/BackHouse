import json
import sqlite3

import pytest


def table(con, name, rows):
    columns = list(rows[0])
    con.execute(f'CREATE TABLE "{name}" ({",".join(columns)})')
    con.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in columns)})',
                    [tuple(row[c] for c in columns) for row in rows])


@pytest.fixture
def root(tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    with sqlite3.connect(data / "books.db") as con:
        table(con, "accounts", [
            dict(account="CHS", opening_as_of="2026-01-01", opening_balance_cents=10000),
            dict(account="RES", opening_as_of="2026-01-01", opening_balance_cents=2000)])
        table(con, "invoices", [
            dict(invoice_no="old", invoice_date="2025-12-01", due_date="2025-12-15", total=500, status="settled", outstanding=0),
            dict(invoice_no="now", invoice_date="2026-01-02", due_date="2026-01-20", total=4000, status="settled", outstanding=0),
            dict(invoice_no="future", invoice_date="2026-02-01", due_date="2026-02-10", total=9000, status="settled", outstanding=0)])
        table(con, "invoice_lines", [dict(invoice_no=k, ingredient="flour", qty=1, unit_price=4.0, line_total=400) for k in ["old", "now", "future"]])
        table(con, "tickets", [dict(ticket_id="t1", date="2026-01-02"), dict(ticket_id="t2", date="2026-02-01")])
        table(con, "ticket_lines", [dict(ticket_id="t1", item="Pasta"), dict(ticket_id="t2", item="Pizza")])
        table(con, "sales_tax_returns", [dict(period="2025-12", paid_date="2026-01-20", tax_due=200)])
        table(con, "open_ap", [dict(doc_id="future-state")])
        table(con, "answer_key", [dict(secret="never")])
    rows = [
        ("pre", "CHS", "purchase", "2025-12-20", 500, "completed"),
        ("n1", "CHS", "deposit", "2026-01-02", 5000, "completed"),
        ("n2", "CHS", "purchase", "2026-01-03", 4000, "completed"),
        ("n3", "CHS", "withdrawal", "2026-01-04", 1000, "completed"),
        ("n4", "RES", "deposit", "2026-01-04", 1000, "completed"),
        ("n5", "CHS", "purchase", "2026-01-05", 9999, "pending"),
        ("n6", "CHS", "deposit", "2026-02-02", 6000, "completed")]
    with sqlite3.connect(data / "nessie_sync_boh-1loc.db") as con:
        table(con, "bank_tx", [dict(nessie_id=i, account=a, nessie_type=t, date=d, amount=v,
            status=s, counterparty="Supplier", memo="purchase |ref=TSECRET", bank_id="T"+i)
            for i,a,t,d,v,s in rows])
    return tmp_path


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


class ScriptedClient:
    def __init__(self, draft):
        self.calls = 0
        self.draft = draft

    async def complete(self, messages, tools):
        self.calls += 1
        assert {t["function"]["name"] for t in tools} == {"query_nessie", "query_books"}
        if self.calls == 1:
            return {"role": "assistant", "content": None, "tool_calls": [
                {"id": f"c{i}", "type": "function", "function": {"name": name,
                 "arguments": json.dumps(dict(operation="query", sql=sql, limit=200, offset=0))}}
                for i, (name, sql) in enumerate([
                    ("query_nessie", "select * from bank_tx order by date, nessie_id"),
                    ("query_books", "select * from invoices order by invoice_no")])]}
        return {"role": "assistant", "content": json.dumps(self.draft)}
