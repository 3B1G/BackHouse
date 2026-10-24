from datetime import date
import json
import sqlite3
from pathlib import Path

import pytest

from bookkeeper.tools import LEGACY_TOOL_GROUPS, TOOL_GROUPS, SnapshotTools, tool_definitions


def test_historical_cutoff_and_no_synthetic_ids(root):
    s = SnapshotTools(root, "boh-1loc", date(2026, 1, 10))
    try:
        assert s.as_of == "2026-01-10" and s.start == "2026-01-01"
        assert s.bank_rows == 67                       # 69 bank rows, 2 after the cutoff
        bank = s.call("search_bank_transactions", {"limit": 50})
        assert "TSECRET" not in json.dumps(bank) and "Tn" not in json.dumps(bank)
        invoices = s.call("search_costs", {"cost_type": "supplier_invoice"})["rows"]
        assert [r["cost_id"] for r in invoices] == ["old", "bulk", "now"]
        assert s.call("get_cost", {"cost_id": "future"})["error"]
        assert any("pre" == r["bank_id"] for r in bank["rows"])
        assert "Bank rows before the opening date are available as evidence but excluded from balances." in s.limitations
    finally:
        s.close()


def test_balance_completed_only_and_transfers(root):
    s = SnapshotTools(root, "boh-1loc", date(2026, 1, 10))
    b = s.balances()
    assert b.total_cents == 15000
    assert {a.account: a.balance_cents for a in b.accounts} == {"CHS": 12000, "RES": 3000}
    assert b.total_cents == s.call("get_account_balances", {})["total_cents"]
    s.close()
    s = SnapshotTools(root, "boh-1loc", None)
    assert s.balances().total_cents == 17000
    s.close()


def test_balance_rejects_unknown_account_and_bad_amounts(root):
    with sqlite3.connect(root / "data/nessie_sync_boh-1loc.db") as con:
        con.execute("INSERT INTO bank_tx (nessie_id, account, nessie_type, date, amount, status, counterparty, memo, bank_id) "
                    "VALUES ('x1', 'ZZZ', 'deposit', '2026-01-05', 1, 'completed', NULL, 'X', 'Tx1')")
    with pytest.raises(ValueError, match="no opening account balance"):
        SnapshotTools(root, "boh-1loc", None).balances()


def test_missing_snapshot_future_and_before_opening(root):
    with pytest.raises(sqlite3.Error):
        SnapshotTools(root, "absent", None)
    with pytest.raises(ValueError, match="coverage"):
        SnapshotTools(root, "boh-1loc", date(2027, 1, 1))
    with pytest.raises(ValueError, match="opening"):
        SnapshotTools(root, "boh-1loc", date(2025, 12, 20))


def test_tool_definitions_and_groups():
    names = [t["function"]["name"] for t in tool_definitions()]
    assert len(names) == 11 and names == TOOL_GROUPS["bank"] + TOOL_GROUPS["books"]
    assert set(LEGACY_TOOL_GROUPS) == {"query_nessie", "query_books"}
    assert not set(LEGACY_TOOL_GROUPS) & set(names)


def test_current_repository_snapshot():
    root = Path(__file__).resolve().parents[2]
    if not (root / "data/nessie_sync_boh-1loc.db").exists() or not (root / "data/books.db").exists():
        pytest.skip("Repository snapshots are optional generated artifacts")
    s = SnapshotTools(root, "boh-1loc", None)
    assert s.balances().total_cents == sum(a.balance_cents for a in s.balances().accounts)
    assert s.balances().total_cents == s.call("get_account_balances", {})["total_cents"]
    assert s.call("search_bank_transactions", {})["total_matches"] == s.bank_rows
    assert s.call("search_costs", {})["total_matches"] > 0
    s.close()
