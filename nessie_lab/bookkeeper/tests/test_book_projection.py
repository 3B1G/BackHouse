import sqlite3

import pandas as pd
import pytest

from bookkeeper.book_projection import TABLES, project_costs


@pytest.fixture
def tables(root):
    with sqlite3.connect(root / "data" / "books.db") as con:
        return {t: pd.read_sql(f"select * from {t}", con) for t in TABLES + ["open_ap", "accounts"]}


def by_id(costs):
    return costs.set_index("cost_id")


def test_book_policy_matches_build_book_rules(tables):
    costs, lines = project_costs(tables, "2026-02-03", policy="book", window_start="2026-01-01", sales_tax_rate=0.08625)
    c = by_id(costs)
    assert c.loc["now", "status"] == "pending" and "open AP" in c.loc["now", "status_basis"]
    assert c.loc["future", "status"] == "paid" and c.loc["VB-CHS-GAS-2026-01", "status"] == "paid"
    assert "paid by autopay" in c.loc["VB-CHS-GAS-2026-01", "description"]
    assert c.loc["now", "credit_cents"] == 700 and "credit memo CM-9 for $7.00 issued 2026-01-15" in c.loc["now", "description"]
    assert "paid in installments: $20.00 due 2026-01-20, $20.00 due 2026-02-03" in c.loc["now", "description"]
    assert c.loc["RENT-CHS-2026-02", "status"] == "paid" and c.loc["RENT-CHS-2026-03", "status"] == "scheduled"
    assert c.loc["LOAN-2026-02", "status"] == "scheduled" and c.loc["MISC-1", "status"] == "paid"
    assert c.loc["TAX-2025-12", "status"] == "paid" and "8.625% on taxable sales" in c.loc["TAX-2025-12", "description"]
    assert c.loc["CB-1", "due_date"] == "2026-01-14" and "outcome lost" in c.loc["CB-1", "description"]
    assert set(c[c.status == "pending"].index) == set(tables["open_ap"].doc_id)
    assert set(lines.cost_id) == set(costs.cost_id) and costs.cost_id.is_unique
    assert lines[lines.cost_id == "PAY-CHS-20260109"].line_total_cents.sum() == c.loc["PAY-CHS-20260109", "amount_cents"]
    assert list(costs.cost_id) == list(costs.sort_values(["due_date", "cost_id"]).cost_id)


def test_projection_has_no_side_effects(tables):
    before = {k: v.copy() for k, v in tables.items()}
    project_costs(tables, "2026-01-10", policy="historical")
    project_costs(tables, "2026-01-10", policy="book")
    for k, v in tables.items():
        pd.testing.assert_frame_equal(v, before[k])


def test_historical_policy_drops_later_documents_and_unknown_status(tables):
    c = by_id(project_costs(tables, "2026-01-10", policy="historical")[0])
    assert "future" not in c.index and "MISC-1" not in c.index and "DRAW-2026-02" not in c.index
    assert c.loc["DRAW-2026-01", "status"] == "paid"
    assert c.loc["now", "status"] == "unknown" and c.loc["now", "credit_cents"] == 0
    assert "credit memo" not in c.loc["now", "description"]
    assert c.loc["PAY-CHS-20260109", "status"] == "paid" and "PAY-CHS-20260123" not in c.index
    assert c.loc["CB-1", "status"] == "unknown" and pd.isna(c.loc["CB-1", "due_date"])
    assert "lost" not in c.loc["CB-1", "description"] and "lost" not in c.loc["CB-1", "status_basis"]
    assert c.loc["TAX-2025-12", "status"] == "scheduled"
    assert not (c.status == "pending").any()


def test_unknown_policy_rejected(tables):
    with pytest.raises(ValueError, match="policy"):
        project_costs(tables, "2026-01-10", policy="guess")
