import builtins
import json
import re
import sqlite3
from datetime import date

import pytest

from bookkeeper.bookkeeper_tools import (GROUP_OF, MAX_ROWS, RESPONSE_CAP, TOOL_GROUPS, ToolSession, asi_tool_definitions,
                                         claude_tool_definitions, succeeded)
from conftest import CUTOFF, make_root


@pytest.fixture
def s(root):
    return ToolSession(root, "boh-1loc", CUTOFF)


def rows(out):
    assert "error" not in out, out
    return out["rows"]


# ---------------------------------------------------------------- schemas and dispatch
def test_eleven_tools_in_asi_and_claude_formats():
    asi = asi_tool_definitions()
    assert [t["function"]["name"] for t in asi] == TOOL_GROUPS["bank"] + TOOL_GROUPS["books"]
    for t in asi:
        assert t["type"] == "function" and set(t["function"]) == {"name", "description", "parameters"}
        assert t["function"]["parameters"]["additionalProperties"] is False
    assert {t["name"] for t in claude_tool_definitions()} == set(GROUP_OF)
    names = json.dumps(asi)
    assert "query_nessie" not in names and "query_books" not in names
    status = next(t for t in asi if t["function"]["name"] == "search_costs")["function"]["parameters"]["properties"]["status"]
    assert "unknown" in status["enum"]


@pytest.mark.parametrize("name,args,message", [
    ("nope", {}, "unknown tool"),
    ("search_costs", "not a dict", "JSON object"),
    ("search_costs", {"colour": "red"}, "unknown field"),
    ("search_costs", {"cost_type": "lunch"}, "must be one of"),
    ("search_costs", {"date_from": "Jan 5"}, "must be a date"),
    ("search_costs", {"limit": MAX_ROWS + 1}, "at most 50"),
    ("search_costs", {"limit": 0}, "at least 1"),
    ("search_bank_transactions", {"offset": -1}, "at least 0"),
    ("search_bank_transactions", {"amount_cents": "40"}, "must be an integer"),
    ("search_bank_transactions", {"amount_cents": True}, "must be an integer"),
    ("search_bank_transactions", {"date_from": "2026-01-09", "date_to": "2026-01-01"}, "after date_to"),
    ("get_cost", {}, "missing required field 'cost_id'"),
    ("get_card_batches", {"date_from": "2026-01-01"}, "missing required field 'date_to'"),
])
def test_invalid_arguments_rejected_before_dispatch(s, name, args, message, monkeypatch):
    monkeypatch.setattr(ToolSession, "search_costs", lambda *a, **k: pytest.fail("dispatched"))
    out = s.call(name, args)
    assert message in out["error"] and out["cutoff"] == CUTOFF
    assert not succeeded(out)


def test_integral_floats_and_nulls_are_accepted(s):
    out = s.call("search_bank_transactions", {"amount_cents": 4000.0, "account": None})
    assert out["total_matches"] == 2


def test_every_response_carries_cutoff_and_limitations(s):
    calls = {"get_bank_transaction": {"bank_id": "n2"}, "search_bank_transactions": {}, "get_payee_history": {"text": "SUPPLIER"},
             "get_bank_bills": {}, "get_account_balances": {}, "search_costs": {}, "get_cost": {"cost_id": "now"},
             "get_agreements": {}, "get_card_batches": {"date_from": "2026-01-01", "date_to": "2026-01-31"},
             "get_cash_deposit_slips": {"date_from": "2026-01-01", "date_to": "2026-01-31"},
             "get_delivery_payouts": {"date_from": "2025-12-29", "date_to": "2026-01-31"}}
    assert set(calls) == set(GROUP_OF)
    for name, args in calls.items():
        out = s.call(name, args)
        assert out["cutoff"] == CUTOFF and out["limitations"], name
        assert succeeded(out) == (name != "get_bank_bills"), (name, out)


def test_response_cap(s, monkeypatch):
    monkeypatch.setattr(ToolSession, "get_agreements", lambda self, counterparty=None: {"blob": "x" * (RESPONSE_CAP + 1)})
    assert "smaller limit" in s.call("get_agreements", {})["error"]


def test_unexpected_exception_becomes_error(s, monkeypatch):
    def boom(self, **kw):
        raise KeyError("broken")
    monkeypatch.setattr(ToolSession, "get_agreements", boom)
    assert s.call("get_agreements", {})["error"].startswith("KeyError")


# ---------------------------------------------------------------- bank tools
def test_bank_rows_use_native_ids_and_sanitized_memos(s):
    page = s.call("search_bank_transactions", {"limit": 50})
    text = json.dumps(page)
    assert "TSECRET" not in text and "|ref=" not in text and not re.search(r'"T(n|p)\d', text)
    assert {r["bank_id"] for r in page["rows"]} >= {"n1", "n2"}
    assert all(r["date"] <= CUTOFF for r in page["rows"])
    one = s.call("get_bank_transaction", {"bank_id": "n2"})
    assert one["transaction"]["bank_id"] == "n2" and one["transaction"]["memo"] == "ACH SUPPLIER"
    assert one["transaction"]["status"] == "completed"
    assert "error" in s.call("get_bank_transaction", {"bank_id": "Tn2"})      # synthetic ids are not accepted


def test_get_bank_transaction_related_rows_respect_cutoff_and_bounds(s, root):
    one = s.call("get_bank_transaction", {"bank_id": "n7"})
    assert [r["bank_id"] for r in one["same_amount_within_14_days"]] == ["n2"]       # n8 (2026-01-20) is after the cutoff
    later = ToolSession(root, "boh-1loc", None).call("get_bank_transaction", {"bank_id": "n7"})
    assert [r["bank_id"] for r in later["same_amount_within_14_days"]] == ["n2", "n8"]
    many = s.call("get_bank_transaction", {"bank_id": "p00"})
    assert len(many["same_amount_within_14_days"]) == 25
    assert many["same_amount_within_14_days_total"] == 59 and many["same_amount_within_14_days_truncated"]
    missing = s.call("get_bank_transaction", {"bank_id": "n8"})
    assert "no bank transaction" in missing["error"]


def test_bank_pagination_beyond_fifty(s):
    seen, offset, pages = [], 0, 0
    while offset is not None:
        page = s.call("search_bank_transactions", {"text": "TABLETAP", "limit": 50, "offset": offset})
        assert page["total_matches"] == 60 and page["sum_amount_cents"] == 6000
        seen += page["rows"]
        offset, pages = page["next_offset"], pages + 1
        assert page["truncated"] == (offset is not None)
    assert pages == 2 and len(seen) == 60 and len({r["bank_id"] for r in seen}) == 60
    keys = [(r["date"], r["account"], r["nessie_type"], r["amount_cents"], r["bank_id"]) for r in seen]
    assert keys == sorted(keys)
    again = s.call("search_bank_transactions", {"text": "TABLETAP", "limit": 7, "offset": 21})
    assert again["rows"] == seen[21:28] and again["next_offset"] == 28
    past = s.call("search_bank_transactions", {"text": "TABLETAP", "offset": 500})
    assert past["rows"] == [] and past["total_matches"] == 60 and past["next_offset"] is None


def test_bank_search_filters_and_empty(s):
    out = s.call("search_bank_transactions", {"amount_cents": 4100, "amount_tolerance_cents": 100, "direction": "out"})
    assert [r["bank_id"] for r in out["rows"]] == ["n2", "n7"] and out["sum_amount_cents"] == 8000
    empty = s.call("search_bank_transactions", {"text": "NO SUCH PAYEE"})
    assert empty["rows"] == [] and empty["total_matches"] == 0 and not empty["truncated"]
    assert succeeded(empty)


def test_payee_history(s):
    out = s.call("get_payee_history", {"text": "TABLETAP", "limit": 3})
    assert out["matches"] == 60 and len(out["most_recent"]) == 3
    assert out["by_memo"][0] == dict(memo_pattern="TABLETAP PAYMENTS DEP ##", count=60, first_date="2026-01-06",
                                     last_date="2026-01-09", min_cents=100, median_cents=100, max_cents=100)
    assert not out["by_memo_truncated"]
    none = s.call("get_payee_history", {"text": "SUPPLIER", "before_date": "2025-12-01"})
    assert none["matches"] == 0 and "2026-01-01 to 2026-01-10" in none["note"]


def test_bank_bills_unavailable_historically(s):
    out = s.call("get_bank_bills", {"status": "pending"})
    assert out["status"] == "unavailable" and "no dated snapshot" in out["reason"]
    assert "bills" not in out and not succeeded(out)


def test_account_balances_completed_only_and_bounded(s, root):
    out = s.call("get_account_balances", {})
    assert {a["account"]: a["balance_cents"] for a in out["accounts"]} == {"CHS": 12000, "RES": 3000}
    assert out["total_cents"] == 15000 and out["as_of"] == CUTOFF
    early = s.call("get_account_balances", {"as_of": "2026-01-03"})
    assert early["total_cents"] == 10000 + 5000 - 4000 + 2000
    assert "after the investigation cutoff" in s.call("get_account_balances", {"as_of": "2026-01-11"})["error"]
    assert "before the opening balances" in s.call("get_account_balances", {"as_of": "2025-12-31"})["error"]
    report = {k: v for k, v in s.account_balances().items()}
    tool = {**out}
    assert report["total_cents"] == tool["total_cents"]
    assert [{k: a[k] for k in a if k != "nickname"} for a in tool["accounts"]] == report["accounts"]


# ---------------------------------------------------------------- books tools
def costs(s, **args):
    return {r["cost_id"]: r for r in rows(s.call("search_costs", {"limit": 50, **args}))}


def test_cost_projection_is_cutoff_filtered(s):
    c = costs(s)
    assert "future" not in c and "VB-CHS-GAS-2026-02" not in c and "MISC-1" not in c and "DRAW-2026-01" in c
    assert all((r["doc_date"] or "") <= CUTOFF for r in c.values() if r["cost_type"] not in ("rent", "loan_payment", "sales_tax"))
    assert c["RENT-CHS-2026-01"]["status"] == "unknown" and c["RENT-CHS-2026-02"]["status"] == "scheduled"
    assert c["LOAN-2026-01"]["status"] == "scheduled" and c["LOAN-2026-02"]["status"] == "scheduled"
    assert "PAY-CHS-20260123" not in c and c["PAY-CHS-20260109"]["status"] == "paid"


def test_unknown_payment_status_without_evidence(s, root):
    c = costs(s)
    assert c["now"]["status"] == "unknown"                       # in open_ap today, but that is current state
    assert c["old"]["status"] == "unknown"
    assert c["VB-CHS-GAS-2026-01"]["status"] == "unknown"        # autopay is an assumption, not evidence
    assert "paid by autopay" not in s.call("get_cost", {"cost_id": "VB-CHS-GAS-2026-01"})["cost"]["description"]
    assert c["RPR-1"]["status"] == "unknown"
    assert c["DRAW-2026-01"]["status"] == "paid"
    assert c["TAX-2025-12"]["status"] == "scheduled"
    assert "paid" not in s.call("get_cost", {"cost_id": "TAX-2025-12"})["cost"]["status_basis"]
    assert "pending" not in {r["status"] for r in c.values()}
    later = costs(ToolSession(root, "boh-1loc", None))
    assert later["TAX-2025-12"]["status"] == "paid" and later["now"]["status"] == "unknown"


def test_recorded_invoice_payments_are_evidence(tmp_path):
    root = make_root(tmp_path / "paid", {"invoice_payments": [
        dict(invoice_no="now", payment_date="2026-01-08", amount=4000), dict(invoice_no="bulk", payment_date="2026-01-09", amount=100),
        dict(invoice_no="old", payment_date="2026-01-20", amount=500)]})
    c = costs(ToolSession(root, "boh-1loc", CUTOFF))
    assert c["now"]["status"] == "paid" and c["bulk"]["status"] == "pending"
    assert c["old"]["status"] == "pending"                       # its payment is after the cutoff


def test_status_filter_discloses_unknown(s):
    out = s.call("search_costs", {"status": "paid", "limit": 50})
    assert {r["status"] for r in out["rows"]} == {"paid"}
    unknown = [r for r in rows(s.call("search_costs", {"status": "unknown", "limit": 50}))]
    assert out["omitted_unknown_status"] == len(unknown) > 0
    assert out["omitted_unknown_status_cents"] == sum(r["amount_cents"] for r in unknown)
    assert out["status_counts"]["unknown"] == len(unknown)
    narrow = s.call("search_costs", {"status": "paid", "cost_type": "payroll"})
    assert narrow["omitted_unknown_status"] == 0
    assert "omitted_unknown_status" not in s.call("search_costs", {"status": "unknown"})


def test_future_credit_memo_hidden_everywhere(s, root):
    detail = s.call("get_cost", {"cost_id": "now"})
    assert detail["cost"]["credit_cents"] == 0
    assert "CM-9" not in json.dumps(detail) and "credit memo" not in detail["cost"]["description"]
    assert [i["due"] for i in detail["installments"]] == ["2026-01-20", "2026-02-03"]        # contractual dates stay
    assert detail["installments_total"] == 2 and not detail["installments_truncated"]
    assert "CM-9" not in json.dumps(s.call("search_costs", {"text": "CM-9"}))
    later = ToolSession(root, "boh-1loc", None).call("get_cost", {"cost_id": "now"})
    assert later["cost"]["credit_cents"] == 700 and later["credit_memos"][0]["credit_no"] == "CM-9"
    assert "credit memo CM-9" in later["cost"]["description"]


def test_get_cost_detail_bounds_and_related_records(s):
    bulk = s.call("get_cost", {"cost_id": "BULK"})                      # case-insensitive
    assert bulk["cost"]["cost_id"] == "bulk" and len(bulk["lines"]) == 50
    assert bulk["lines_total"] == 55 and bulk["lines_truncated"]
    assert bulk["supplier_terms"] == dict(category="Food Supply - Dry Goods", terms_days=14, delivery_days="Tue, Fri")
    now = s.call("get_cost", {"cost_id": "now"})
    assert [r["cost_id"] for r in now["same_payee_before"]] == ["bulk", "old"]           # most recent first, never "future"
    assert now["same_payee_before_total"] == 2 and not now["same_payee_before_truncated"]
    missing = s.call("get_cost", {"cost_id": "future"})
    assert "no cost future in the Book on or before 2026-01-10" in missing["error"]


def test_chargeback_outcome_hidden_until_debited(s, root):
    cb = s.call("get_cost", {"cost_id": "CB-1"})["cost"]
    assert cb["status"] == "unknown" and cb["due_date"] is None
    assert "lost" not in json.dumps(cb) and "2026-01-14" not in json.dumps(cb)
    batches = s.call("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-31"})
    assert batches["chargebacks"] == [dict(case_id="CB-1", location="CHS", opened="2026-01-06", debited=None, amount_cents=300,
                                           outcome=None)]
    later = ToolSession(root, "boh-1loc", None).call("get_cost", {"cost_id": "CB-1"})["cost"]
    assert later["status"] == "paid" and "lost" in later["status_basis"]


def test_agreements(s):
    all_ = s.call("get_agreements", {})
    assert all_["card_processor"][0]["rate_pct"] == 2.6 and all_["card_processor"][0]["fixed_fee_cents_per_transaction"] == 15
    assert all_["delivery_platforms"][0]["commission_pct"] == 15.0
    assert all_["leases"][0]["monthly_rent_cents"] == 100000 and all_["loans"][0]["monthly_payment_cents"] == 2250
    assert set(s.call("get_agreements", {"counterparty": "rapid"})) == {"delivery_platforms", "cutoff", "limitations"}
    assert "no agreement" in s.call("get_agreements", {"counterparty": "Nobody"})["error"]


def test_card_batches_cutoff_pagination_and_expected_dates(s):
    out = s.call("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-31", "limit": 5})
    assert out["total_matches"] == 9 and out["returned"] == 5 and out["next_offset"] == 5      # 01-02 .. 01-10
    assert out["totals"]["gross_sales_cents"] == 90000
    first = out["rows"][0]
    assert first["expected_fee_cents"] == 260 + 150 and first["expected_net_cents"] == 10000 - 410
    assert first["expected_settlement_date"] == "2026-01-05"                                    # Fri 01-02 settles Monday
    rest = s.call("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-31", "limit": 5, "offset": 5})
    assert [r["batch_date"] for r in rest["rows"]] == ["2026-01-07", "2026-01-08", "2026-01-09", "2026-01-10"]
    assert rest["rows"][-1]["expected_after_cutoff"] and not rest["truncated"]
    empty = s.call("get_card_batches", {"date_from": "2025-06-01", "date_to": "2025-06-30"})
    assert empty["rows"] == [] and empty["totals"]["gross_sales_cents"] == 0 and empty["chargebacks"] == []


def test_cash_slips_omit_snapshot_only_transit(s, root):
    out = s.call("get_cash_deposit_slips", {"date_from": "2026-01-01", "date_to": "2026-01-31"})
    assert out["total_matches"] == 8 and "not_yet_deposited" not in out
    assert out["rows"][0]["over_short_cents"] == -3 and out["totals"]["over_short_cents"] == -sum(range(3, 11))
    assert any("not yet deposited" in note for note in out["limitations"]) and "not_yet_deposited" not in out


def test_delivery_payouts_filter_tickets_and_label_partial_weeks(s):
    out = s.call("get_delivery_payouts", {"date_from": "2025-12-29", "date_to": "2026-01-31"})
    weeks = {r["week_start"]: r for r in out["rows"]}
    assert set(weeks) == {"2025-12-29", "2026-01-05"}
    assert weeks["2025-12-29"]["complete_week"] and weeks["2025-12-29"]["subtotal_cents"] == 2000
    partial = weeks["2026-01-05"]
    assert partial["complete_week"] is False and partial["orders"] == 2 and partial["subtotal_cents"] == 4000     # d4 is after the cutoff
    assert partial["expected_commission_cents"] == 600 and partial["expected_payout_cents"] == 3400
    assert partial["expected_payout_date"] == "2026-01-13" and partial["expected_after_cutoff"]
    assert out["incomplete_weeks"] == ["2026-01-05"] and out["totals"]["orders"] == 3
    assert s.call("get_delivery_payouts", {"date_from": "2026-01-01", "date_to": "2026-01-31", "platform": "UrbanBite"})["rows"] == []


def test_cost_search_pagination_and_ordering(s):
    full = rows(s.call("search_costs", {"limit": 50}))
    paged, offset = [], 0
    while offset is not None:
        page = s.call("search_costs", {"limit": 3, "offset": offset})
        assert page["total_matches"] == len(full)
        paged += page["rows"]
        offset = page["next_offset"]
    assert paged == full
    order = [((r["due_date"] or r["doc_date"]), r["cost_id"]) for r in full]
    assert order == sorted(order)
    total = s.call("search_costs", {"cost_type": "supplier_invoice", "limit": 1})
    assert total["sum_amount_cents"] == 500 + 4000 + 5500 and total["returned"] == 1


# ---------------------------------------------------------------- isolation, frozen sources, grader
def test_sessions_are_isolated_and_frozen(tmp_path):
    a = make_root(tmp_path / "a")
    b = make_root(tmp_path / "b", tag="other")
    with sqlite3.connect(b / "data" / "nessie_sync_other.db") as con:
        con.execute("UPDATE bank_tx SET amount = amount * 2")
    sa, sb = ToolSession(a, "boh-1loc", CUTOFF), ToolSession(b, "other", "2026-01-05")
    early = ToolSession(a, "boh-1loc", "2026-01-03")
    assert sa.call("get_account_balances", {})["total_cents"] == 15000
    assert early.call("get_account_balances", {})["total_cents"] == 11000 + 2000
    assert sb.cutoff == "2026-01-05" and sb.call("get_bank_transaction", {"bank_id": "n2"})["transaction"]["amount_cents"] == 8000
    assert sa.call("get_bank_transaction", {"bank_id": "n2"})["transaction"]["amount_cents"] == 4000
    assert "n7" not in json.dumps(early.call("search_bank_transactions", {"limit": 50}))
    with sqlite3.connect(a / "data" / "nessie_sync_boh-1loc.db") as con:
        con.execute("DELETE FROM bank_tx WHERE nessie_id = 'n2'")
    with sqlite3.connect(a / "data" / "books.db") as con:
        con.execute("UPDATE invoices SET total = 1 WHERE invoice_no = 'now'")
    assert sa.call("get_bank_transaction", {"bank_id": "n2"})["transaction"]["amount_cents"] == 4000
    assert sa.call("get_cost", {"cost_id": "now"})["cost"]["amount_cents"] == 4000
    fresh = ToolSession(a, "boh-1loc", CUTOFF)
    assert "error" in fresh.call("get_bank_transaction", {"bank_id": "n2"})
    assert fresh.call("get_cost", {"cost_id": "now"})["cost"]["amount_cents"] == 1


def test_cutoff_validation(root):
    assert ToolSession(root, "boh-1loc").cutoff == "2026-02-02"
    assert ToolSession(root, "boh-1loc", date(2026, 1, 10)).cutoff == CUTOFF
    with pytest.raises(ValueError, match="coverage"):
        ToolSession(root, "boh-1loc", "2026-02-03")
    with pytest.raises(ValueError, match="opening"):
        ToolSession(root, "boh-1loc", "2025-12-31")
    with pytest.raises(sqlite3.Error, match="missing"):
        ToolSession(root, "absent")


def test_never_reads_grader_or_snapshot_only_tables(root, monkeypatch):
    opened, real_connect, real_open = [], sqlite3.connect, builtins.open
    monkeypatch.setattr(sqlite3, "connect", lambda path, *a, **k: opened.append(str(path)) or real_connect(path, *a, **k))
    monkeypatch.setattr(builtins, "open", lambda path, *a, **k: opened.append(str(path)) or real_open(path, *a, **k))
    s = ToolSession(root, "boh-1loc", CUTOFF)
    for name in GROUP_OF:
        s.call(name, {"bank_id": "n2"} if name == "get_bank_transaction" else {"cost_id": "now"} if name == "get_cost" else
               {"text": "A"} if name == "get_payee_history" else
               {"date_from": "2026-01-01", "date_to": "2026-01-31"} if name.startswith(("get_card", "get_cash", "get_deliv")) else {})
    assert opened and not [p for p in opened if "/grader/" in p or p.endswith("/grader")]
    assert {p.rsplit("/", 1)[-1].split("?")[0] for p in opened} == {"books.db", "nessie_sync_boh-1loc.db"}
    assert all("mode=ro" in p for p in opened)
    assert "open_ap" not in s.books and "deposits_in_transit" not in s.books and "answer_key" not in s.books


def test_legacy_mode_keeps_standalone_behaviour(root):
    s = ToolSession(root, "boh-1loc", "2026-02-03", mode="legacy")
    tx = s.call("get_bank_transaction", {"bank_id": "Tn2"})
    assert tx["transaction"]["bank_id"] == "Tn2" and "TSECRET" not in json.dumps(tx) and "cutoff" not in tx
    assert {r["cost_id"]: r["status"] for r in s.call("search_costs", {"limit": 50})["rows"]}["now"] == "pending"
    bills = s.call("get_bank_bills", {})
    assert bills["count"] == 1 and bills["pending_total_cents"] == 4000
    slips = s.call("get_cash_deposit_slips", {"date_from": "2026-01-01", "date_to": "2026-02-03"})
    assert slips["not_yet_deposited"][0]["amount_cents"] == 1234
    assert len(s.load_bank_frame()) == len(__import__("conftest").bank_rows())


def test_search_rows_are_lean_and_notes_short(s):
    cost = s.call("search_costs", {"limit": 1})
    assert list(cost["rows"][0]) == ["cost_id", "cost_type", "payee", "doc_date", "due_date", "amount_cents", "credit_cents", "status"]
    assert "description" in s.call("get_cost", {"cost_id": cost["rows"][0]["cost_id"]})["cost"]
    bank = s.call("search_bank_transactions", {"limit": 1})["rows"][0]
    assert "direction" not in bank and bank["nessie_type"] and bank["status"]
    assert s.call("search_bank_transactions", {"direction": "in", "limit": 50})["total_matches"] == 62     # filter still works
    assert all(len(note) < 80 for name in GROUP_OF for note in s.call(name, {}).get("limitations", []))
    assert len(s.limitations) >= 9 and all(len(text) > 40 for text in s.limitations)


# ---------------------------------------------------------------- settlement matching
@pytest.fixture
def settled(tmp_path):
    return make_root(tmp_path / "settled", settlements=True)


def by_key(out, key):
    return {r[key]: r for r in out["rows"]}


def test_card_batches_matched_to_deposits(settled):
    s = ToolSession(settled, "boh-1loc", CUTOFF)
    out = s.call("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-31"})
    b = by_key(out, "batch_date")
    assert b["2026-01-02"]["match_status"] == "matched" and b["2026-01-02"]["bank_ids"] == ["c0102"]
    assert b["2026-01-02"]["bank_date"] == "2026-01-05" and b["2026-01-02"]["difference_cents"] == 0
    assert b["2026-01-04"]["match_status"] == "amount_mismatch" and b["2026-01-04"]["difference_cents"] == -90
    assert b["2026-01-05"]["match_status"] == "missing" and b["2026-01-05"]["bank_ids"] == []
    assert b["2026-01-06"]["match_status"] == "multiple" and set(b["2026-01-06"]["bank_ids"]) == {"c0106", "c0106b"}
    assert b["2026-01-07"]["match_status"] == "matched" and b["2026-01-07"]["difference_cents"] == -5
    assert b["2026-01-09"]["match_status"] == "not_yet_due" and b["2026-01-10"]["match_status"] == "not_yet_due"
    assert out["match_counts"] == {"amount_mismatch": 1, "matched": 4, "missing": 1, "multiple": 1, "not_yet_due": 2}
    assert out["difference_total_cents"] == -90 - 5 + 9590           # the duplicate counts as extra money in
    unmatched = {r["bank_id"] for r in out["unmatched_deposits"]}
    assert "c1231" in unmatched and "c0109" not in unmatched        # 01-12 is after the cutoff
    assert out["unmatched_deposits_total"] == 61                     # c1231 plus the 60 paging rows ("TABLETAP PAYMENTS DEP 00".."59")
    strict = s.call("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-31", "tolerance_cents": 0})
    assert by_key(strict, "batch_date")["2026-01-07"]["match_status"] == "amount_mismatch"
    later = by_key(ToolSession(settled, "boh-1loc", None).call("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-31"}),
                   "batch_date")
    assert later["2026-01-09"]["match_status"] == "matched" and later["2026-01-11"]["match_status"] == "missing"


def test_exceptions_only_pages_and_keeps_range_counts(settled):
    s = ToolSession(settled, "boh-1loc", CUTOFF)
    out = s.call("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-31", "exceptions_only": True, "limit": 2})
    assert out["total_matches"] == 5 and out["returned"] == 2 and out["next_offset"] == 2
    assert all(r["match_status"] != "matched" for r in out["rows"])
    assert out["match_counts"]["matched"] == 4                       # counts still describe the whole range
    assert out["totals"]["gross_sales_cents"] == 5 * 10000          # totals describe the matches returned by the filter
    assert "true or false" in s.call("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-31",
                                                          "exceptions_only": "yes"})["error"]
    assert "at least 0" in s.call("get_cash_deposit_slips", {"date_from": "2026-01-01", "date_to": "2026-01-31",
                                                             "tolerance_cents": -1})["error"]


def test_cash_slips_matched_same_day_first(settled):
    s = ToolSession(settled, "boh-1loc", CUTOFF)
    out = s.call("get_cash_deposit_slips", {"date_from": "2026-01-01", "date_to": "2026-01-31"})
    slips = {r["deposit_date"]: (r["match_status"], r["bank_ids"], r["difference_cents"]) for r in out["rows"]}
    assert slips["2026-01-05"] == ("matched", ["b0105c"], 0)         # its own same-day deposit, not a weekend slip's
    assert slips["2026-01-03"] == ("matched", ["b0105a"], 0) and slips["2026-01-04"] == ("matched", ["b0105b"], 0)
    assert slips["2026-01-06"] == ("amount_mismatch", ["b0106"], -200)
    assert slips["2026-01-07"][0] == "missing" and slips["2026-01-10"][0] == "missing"
    assert slips["2026-01-08"] == ("matched", ["b0108"], 0)
    assert {r["bank_id"] for r in out["unmatched_deposits"]} == {"n1"}     # 01-02 branch deposit has no slip


def test_payouts_matched_to_weekly_deposits(settled, tmp_path):
    s = ToolSession(settled, "boh-1loc", CUTOFF)
    out = s.call("get_delivery_payouts", {"date_from": "2025-12-01", "date_to": "2026-01-31"})
    w = by_key(out, "week_start")
    assert w["2025-12-29"]["match_status"] == "matched" and w["2025-12-29"]["bank_ids"] == ["r1229"]
    assert w["2026-01-05"]["match_status"] == "not_yet_due"
    assert [r["bank_id"] for r in out["unmatched_deposits"]] == ["r1222"]
    later = by_key(ToolSession(settled, "boh-1loc", None).call("get_delivery_payouts", {"date_from": "2025-12-01", "date_to": "2026-01-31"}),
                   "week_start")
    assert later["2026-01-05"]["match_status"] == "missing" and later["2026-01-05"]["expected_payout_cents"] == 11050
    none = s.call("get_delivery_payouts", {"date_from": "2025-12-01", "date_to": "2026-01-31", "platform": "UrbanBite"})
    assert none["rows"] == [] and none["unmatched_deposits"] == []


def test_matching_ignores_bank_rows_after_cutoff(settled):
    s = ToolSession(settled, "boh-1loc", "2026-01-06")
    out = s.call("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-31"})
    text = json.dumps(out)
    assert "c0107" not in text and "c0108" not in text and "c0109" not in text
    assert by_key(out, "batch_date")["2026-01-06"]["match_status"] == "not_yet_due"   # due 01-07, after this cutoff
    assert by_key(out, "batch_date")["2026-01-05"]["match_status"] == "missing"       # due 01-06
