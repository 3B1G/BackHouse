"""Bookkeeper's data tools: the 11 domain tools over a frozen, cutoff-filtered snapshot of the bank and the books."""
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .bookkeeper_tools import GROUP_OF, TOOL_GROUPS, TOOLSET_VERSION, ToolSession, asi_tool_definitions, succeeded
from .contracts import AccountBalance, Balances

# Traces written before the domain tools used two SQL tools; resume still recognizes them.
LEGACY_TOOL_GROUPS = {"query_nessie": "bank", "query_books": "books"}

__all__ = ["GROUP_OF", "LEGACY_TOOL_GROUPS", "TOOL_GROUPS", "TOOLSET_VERSION", "SnapshotTools", "read_database",
           "succeeded", "tool_definitions"]


@contextmanager
def read_database(path: Path):
    """Open an existing database without allowing SQLite to create or modify it."""
    con = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    try:
        yield con
    finally:
        con.close()


class SnapshotTools:
    """Own one request's tool session and source metadata for a single historical cutoff."""

    def __init__(self, root: Path, tag: str, as_of: date | None):
        self.session = ToolSession(root, tag, as_of, mode="historical")
        self.bank_path, self.books_path = self.session.bank_path, self.session.books_path
        self.as_of, self.start = self.session.cutoff, self.session.start
        self.bank_rows = self.session.bank_rows
        self.limitations = list(self.session.limitations)
        self.groups = TOOL_GROUPS

    def call(self, name: str, arguments) -> dict:
        """Run one tool; problems come back as {"error": ...} rather than exceptions."""
        return self.session.call(name, arguments)

    def close(self):
        """Release the private snapshot after a report run."""
        self.session.close()

    def balances(self) -> Balances:
        """Calculate cash from opening balances and completed movements, without corrections."""
        b = self.session.account_balances()
        return Balances(as_of=b["as_of"], accounts=[AccountBalance(**a) for a in b["accounts"]], total_cents=b["total_cents"])


def tool_definitions():
    """The 11 function tools in ASI's schema format."""
    return asi_tool_definitions("historical")
