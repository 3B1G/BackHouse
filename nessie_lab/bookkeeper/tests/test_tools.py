from datetime import date
import json
import sqlite3
from pathlib import Path

import pytest

from bookkeeper.tools import SnapshotTools, SQLiteTool


def test_historical_cutoff_and_no_synthetic_ids(root):
    s = SnapshotTools(root, "boh-1loc", date(2026, 1, 10))
    try:
        bank = s.tools["query_nessie"].call("query", "select * from bank_tx")
        assert len(bank["rows"]) == 6
        assert "TSECRET" not in json.dumps(bank)
        assert "bank_id" not in json.dumps(bank)
        books = s.tools["query_books"]
        assert [r["invoice_no"] for r in books.call("query", "select invoice_no from invoices order by invoice_no")["rows"]] == ["now", "old"]
        assert len(books.call("query", "select * from invoice_lines")["rows"]) == 2
        assert len(books.call("query", "select * from ticket_lines")["rows"]) == 1
        assert books.call("query", "select paid_date from sales_tax_returns")["rows"] == [{"paid_date": None}]
        schema = books.call("schema")["tables"]
        assert "status" not in schema["invoices"]
        assert "open_ap" not in schema and "answer_key" not in schema
    finally:
        s.close()


def test_balance_completed_only_and_transfers(root):
    s = SnapshotTools(root, "boh-1loc", date(2026, 1, 10))
    b = s.balances()
    assert b.total_cents == 13000
    assert {a.account: a.balance_cents for a in b.accounts} == {"CHS": 10000, "RES": 3000}
    s.close()
    s = SnapshotTools(root, "boh-1loc", None)
    assert s.balances().total_cents == 19000
    s.close()


def test_missing_snapshot_future_and_before_opening(root):
    with pytest.raises(sqlite3.Error):
        SnapshotTools(root, "absent", None)
    with pytest.raises(ValueError, match="coverage"):
        SnapshotTools(root, "boh-1loc", date(2027, 1, 1))
    with pytest.raises(ValueError, match="opening"):
        SnapshotTools(root, "boh-1loc", date(2025, 12, 20))


def test_paging_aggregates_and_readonly():
    t = SQLiteTool({"items": (["i"], [{"i": i} for i in range(205)])}, "2026-01-01", [])
    page = t.call("query", "select i from items order by i")
    assert page["truncated"] and len(page["rows"]) == 200 and page["next_offset"] == 200
    assert len(t.call("query", "select i from items order by i", offset=200)["rows"]) == 5
    assert t.call("query", "select sum(i) as total from items")["rows"][0]["total"] == sum(range(205))
    for sql in ["delete from items", "attach database '/tmp/nope' as other", "pragma table_info(items)",
                "select * from sqlite_master", "select load_extension('bad')", "select i from items; delete from items"]:
        with pytest.raises(sqlite3.Error):
            t.call("query", sql)
    with pytest.raises(ValueError):
        t.call("query", "select * from items", limit=201)
    t.close()


def test_current_repository_snapshot():
    root = Path(__file__).resolve().parents[2]
    if not (root / "data/nessie_sync_boh-1loc.db").exists():
        pytest.skip("Repository snapshots are optional generated artifacts")
    s = SnapshotTools(root, "boh-1loc", None)
    assert s.balances().total_cents == sum(a.balance_cents for a in s.balances().accounts)
    assert s.tools["query_nessie"].call("query", "select count(*) as n from bank_tx")["rows"][0]["n"] == len(s.bank_rows)
    s.close()
