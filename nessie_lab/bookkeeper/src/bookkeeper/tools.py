"""Bookkeeper's data tools: the 11 domain tools over a frozen, cutoff-filtered snapshot of the bank and the books."""
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from .bookkeeper_tools import GROUP_OF, TOOL_GROUPS, TOOLSET_VERSION, ToolSession, asi_tool_definitions, succeeded
from .contracts import AccountBalance, Balances

LOG = logging.getLogger("bookkeeper")
# Traces written before the domain tools used two SQL tools; resume still recognizes them.
LEGACY_TOOL_GROUPS = {"query_nessie": "bank", "query_books": "books"}

__all__ = ["GROUP_OF", "LEGACY_TOOL_GROUPS", "TOOL_GROUPS", "TOOLSET_VERSION", "SnapshotTools", "manifest_mismatch", "read_database",
           "succeeded", "tool_definitions"]


def manifest_mismatch(root: Path, tag: str, session) -> str | None:
    """A limitation when the snapshot's Nessie ids are not the committed manifest's (a different Nessie load).

    Reports name native Nessie ids; contracts.from_bk_report converts them with bookkeeper_tools.bank_id_map(), which
    only knows the ids of the manifest and of whichever sync is on that machine."""
    path = root / "data" / f"nessie_manifest_{tag}.json"
    if not path.is_file():
        return None
    try:
        known = set(json.loads(path.read_text())["tx"].values())
    except (ValueError, KeyError, TypeError, AttributeError):
        return f"The committed {path.name} could not be read, so whether its Nessie ids match this snapshot is unknown."
    ids = list(session.bank.bank_id)
    missing = sum(i not in known for i in ids)
    if not missing:
        return None
    return (f"{missing} of {len(ids)} bank rows in this snapshot have Nessie ids that are not in the committed {path.name} "
            f"(a different Nessie load); converting this report's bank ids needs this machine's "
            f"data/nessie_sync_{tag}.db (bookkeeper_tools.bank_id_map()).")


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
        self.bank_ids = frozenset(self.session.bank.bank_id)          # every bank row through the cutoff (the Verifier's check)
        self.limitations = list(self.session.limitations)
        if note := manifest_mismatch(Path(root), tag, self.session):
            LOG.warning(note)
            self.limitations.append(note)
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
