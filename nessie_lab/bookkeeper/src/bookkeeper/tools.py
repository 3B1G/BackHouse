"""Cutoff-filtered SQLite substitutes for the two future production tools."""
import json
import re
import sqlite3
import time
from contextlib import contextmanager
from datetime import date
from pathlib import Path
from typing import Protocol

from .contracts import AccountBalance, Balances

STATIC = {"locations", "accounts", "recipes", "ingredients", "suppliers", "leases", "agreements", "loans"}
DATED = {
    "menu_prices": "effective_from", "daily_ops": "date", "tickets": "date",
    "refunds": "refund_date", "gift_cards": "sold_date", "pos_card_batches": "batch_date",
    "cash_deposit_slips": "deposit_date", "invoices": "invoice_date",
    "credit_memos": "issued", "inventory_counts": "count_date", "payroll_runs": "pay_date",
    "vendor_bills": "bill_date", "repair_invoices": "date", "misc_payables": "date",
    "owner_draws": "date", "internal_transfers": "date", "processor_disputes": "opened",
    "invoice_payments": "payment_date",
}
CHILDREN = {"ticket_lines": ("tickets", "ticket_id"), "invoice_lines": ("invoices", "invoice_no"),
            "ap_payment_plans": ("invoices", "invoice_no")}


@contextmanager
def read_database(path: Path):
    """Open an existing database without allowing SQLite to create or modify it."""
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


class DataTool(Protocol):
    """Replacement adapters implement this schema/query interface."""

    def call(self, operation: str, sql: str | None = None, offset: int = 0, limit: int = 200) -> dict: ...


class SQLiteTool:
    """Expose only sanitized in-memory tables, bounded by time and response size."""

    def __init__(self, tables: dict, as_of: str, limitations: list[str]):
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.as_of, self.limitations = as_of, limitations
        self.schema = {}
        for name, (columns, rows) in tables.items():
            self.schema[name] = columns
            definitions = ",".join(f'"{c}"' for c in columns)
            self.con.execute(f'CREATE TABLE "{name}" ({definitions})')
            self.con.executemany(f'INSERT INTO "{name}" VALUES ({",".join("?" for _ in columns)})',
                                 [tuple(r.get(c) for c in columns) for r in rows])
        self.con.commit()
        self.con.execute("PRAGMA query_only=ON")
        self.con.set_authorizer(self._authorize)

    def _authorize(self, action, arg1, arg2, database, source):
        if action in (sqlite3.SQLITE_SELECT, sqlite3.SQLITE_RECURSIVE):
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_READ and arg1 in self.schema:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_FUNCTION and arg2.lower() not in {"load_extension", "readfile", "writefile"}:
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    def close(self):
        """Release the private snapshot after a report run."""
        self.con.close()

    def call(self, operation, sql=None, offset=0, limit=200):
        """Discover tables or run one read-only SELECT with explicit paging."""
        if operation == "schema":
            return {"tables": self.schema, "as_of": self.as_of, "limitations": self.limitations,
                    "units": "Money is cents except invoice_lines.unit_price, which is dollars per unit."}
        if operation != "query" or not sql or type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 200:
            raise ValueError("Use schema or query, with offset >= 0 and limit between 1 and 200")
        deadline = time.monotonic() + 3
        self.con.set_progress_handler(lambda: int(time.monotonic() > deadline), 1000)
        try:
            rows = self.con.execute(f"SELECT * FROM ({sql.rstrip().rstrip(';')}) LIMIT ? OFFSET ?", (limit + 1, offset)).fetchall()
            result = {"rows": [dict(r) for r in rows[:limit]], "truncated": len(rows) > limit,
                      "next_offset": offset + limit if len(rows) > limit else None, "as_of": self.as_of}
            if len(json.dumps(result)) > 120_000:
                raise ValueError("Response too large: select fewer columns, aggregate, or reduce limit")
            return result
        finally:
            self.con.set_progress_handler(None, 0)


class SnapshotTools:
    """Own the two adapters and source metadata for a single historical cutoff."""

    def __init__(self, root: Path, tag: str, as_of: date | None):
        self.bank_path = root / "data" / f"nessie_sync_{tag}.db"
        self.books_path = root / "data" / "books.db"
        with read_database(self.bank_path) as bank:
            full = [dict(r) for r in bank.execute("SELECT * FROM bank_tx")]
        if not full:
            raise ValueError("The bank snapshot has no transactions")
        self.latest = max(r["date"] for r in full)
        self.as_of = (as_of.isoformat() if as_of else self.latest)
        if self.as_of > self.latest:
            raise ValueError(f"Requested date exceeds snapshot coverage ({self.latest})")
        self.limitations = [
            "Unverified report: snapshot completeness is assumed, not checked.",
            "Static agreements, recipes, leases and reference data have no version history; their historical validity is assumed.",
            "Invoice status reconciliation is future work; snapshot statuses are not inferred from bank payments.",
            "Bank bills and snapshot-only open AP/transit state are omitted because historical state is unavailable.",
        ]
        self.bank_rows = []
        for row in full:
            if row["date"] > self.as_of:
                continue
            clean = {k: row.get(k) for k in ("nessie_id", "account", "nessie_type", "date", "amount", "counterparty", "memo", "status")}
            clean["memo"] = re.sub(r"\s*\|ref=\S+", "", clean["memo"] or "")
            self.bank_rows.append(clean)
        columns = ["nessie_id", "account", "nessie_type", "date", "amount", "counterparty", "memo", "status"]
        tables = {}
        with read_database(self.books_path) as books:
            names = {r[0] for r in books.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            for name in sorted((STATIC | set(DATED) | set(CHILDREN) | {"sales_tax_returns", "loan_schedule"}) & names):
                rows = [dict(r) for r in books.execute(f'SELECT * FROM "{name}"')]
                cols = [r[1] for r in books.execute(f'PRAGMA table_info("{name}")')]
                if name in DATED:
                    field = DATED[name]
                    rows = [r for r in rows if r.get(field) and str(r[field])[:10] <= self.as_of]
                if name == "accounts":
                    rows = [r for r in rows if r["opening_as_of"] <= self.as_of]
                if name == "invoices":
                    cols = [c for c in cols if c not in {"status", "outstanding"}]
                if name == "sales_tax_returns":
                    rows = [r for r in rows if r["period"] < self.as_of[:7]]
                    for row in rows:
                        if row.get("paid_date") and row["paid_date"] > self.as_of:
                            row["paid_date"] = None
                if name == "processor_disputes":
                    for row in rows:
                        if row.get("debited") and row["debited"] > self.as_of:
                            row["debited"], row["outcome"] = None, None
                if name == "loan_schedule":
                    rows = [r for r in rows if r["due"] <= self.as_of]
                tables[name] = (cols, rows)
        for child, (parent, key) in CHILDREN.items():
            if child in tables:
                allowed = {r[key] for r in tables[parent][1]}
                cols, rows = tables[child]
                tables[child] = (cols, [r for r in rows if r[key] in allowed])
        self.accounts = tables["accounts"][1]
        if not self.accounts:
            raise ValueError("No opening balances exist at the requested cutoff")
        self.start = min(r["opening_as_of"] for r in self.accounts)
        if any(r["date"] < self.start for r in self.bank_rows):
            self.limitations.append("Bank rows before the opening date are available as evidence but excluded from balances.")
        self.tools = {
            "query_nessie": SQLiteTool({"bank_tx": (columns, self.bank_rows)}, self.as_of, self.limitations),
            "query_books": SQLiteTool(tables, self.as_of, self.limitations),
        }

    def close(self):
        """Release both query adapters."""
        for tool in self.tools.values():
            tool.close()

    def balances(self) -> Balances:
        """Calculate cash from opening balances and completed movements, without corrections."""
        amounts = {}
        for account in self.accounts:
            opening = account["opening_balance_cents"]
            if type(opening) is not int:
                raise ValueError("Opening balance must be integer cents")
            amounts[account["account"]] = AccountBalance(account=account["account"],
                opening_as_of=account["opening_as_of"], opening_balance_cents=opening,
                inflow_cents=0, outflow_cents=0, balance_cents=opening)
        for row in self.bank_rows:
            if row["status"] != "completed":
                continue
            if row["account"] not in amounts:
                raise ValueError("Completed transaction has no opening account balance")
            entry = amounts[row["account"]]
            if row["date"] < entry.opening_as_of.isoformat():
                continue
            value = row["amount"]
            if type(value) is not int or value < 0:
                raise ValueError("Bank amounts must be nonnegative integer cents")
            if row["nessie_type"] == "deposit":
                entry.inflow_cents += value
            elif row["nessie_type"] in {"purchase", "withdrawal"}:
                entry.outflow_cents += value
            else:
                raise ValueError("Unsupported completed transaction type")
        for entry in amounts.values():
            entry.balance_cents += entry.inflow_cents - entry.outflow_cents
        return Balances(as_of=self.as_of, accounts=list(amounts.values()), total_cents=sum(e.balance_cents for e in amounts.values()))


def tool_definitions():
    """Return the same two function contracts for mock or production adapters."""
    return [{"type": "function", "function": {"name": name,
        "description": description + " Discover schema first. Use SELECTs, aggregates and ORDER BY for stable pagination. No writes.",
        "parameters": {"type": "object", "properties": {
            "operation": {"type": "string", "enum": ["schema", "query"]},
            "sql": {"type": ["string", "null"]}, "offset": {"type": "integer", "minimum": 0},
            "limit": {"type": "integer", "minimum": 1, "maximum": 200}},
            "required": ["operation", "sql", "offset", "limit"], "additionalProperties": False}}}
        for name, description in [("query_nessie", "Read the bank snapshot; nessie_id is the bank reference."),
                                  ("query_books", "Read restaurant documents and opening balances.")]]
