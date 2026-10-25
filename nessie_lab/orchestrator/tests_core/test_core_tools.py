import asyncio
import json
import re

import pytest

from conftest import dump_hash
from orchestrator import tools
from orchestrator.session import ID_RE, Session

NAMES = ["get_cash_position", "list_bills_due", "lookup", "get_findings", "scan_market", "get_menu",
         "list_upcoming_changes", "forecast_base", "forecast_scenario", "propose_fix", "preview_suggestion"]
BK_REPORT = {"report_id": "R-1", "as_of": "2026-10-24", "schema_version": "1.1", "findings": [
    {"id": "B1", "root_cause": "card_fee_creep", "verdict": "error", "confidence": 0.9, "counterparty": "TableTap",
     "bank_ids": ["n-1"], "dates": {"first": "2026-08-01", "last": "2026-10-20"}, "books_refs": [],
     "amounts": {"impact_cents": 34900}, "explanation": "TableTap charged 2.9% instead of 2.6% since Aug 1.",
     "evidence": [{"type": "bank_row", "ref": "n-1"}], "recoverable": True, "suggested_action": "dispute",
     "treatment": {"kind": "ongoing", "since": "2026-08-01", "monthly_cents": 30000}},
    {"id": "B2", "root_cause": "duplicate_charge", "verdict": "error", "confidence": 0.9, "counterparty": "TableTap",
     "bank_ids": ["T0000000A2"], "dates": {"first": "2026-10-21", "last": "2026-10-21"}, "books_refs": [],
     "amounts": {"impact_cents": 34900}, "explanation": "Charged twice.",
     "evidence": [{"type": "bank_row", "ref": "T0000000A2"}],
     "treatment": {"kind": "exclude", "bank_ids": ["T0000000A2"]}},
]}


def test_tools_are_openai_functions_with_the_fixed_names():
    assert [t["function"]["name"] for t in tools.TOOLS] == NAMES
    for t in tools.TOOLS:
        assert t["type"] == "function" and set(t["function"]) == {"name", "description", "parameters"}
        assert t["function"]["parameters"]["type"] == "object"
    lookup = tools.SPECS["lookup"]["parameters"]["properties"]["tool"]["enum"]
    import bookkeeper_tools as bt
    assert sorted(lookup) == sorted(bt.SPECS) and len(lookup) == 11


@pytest.mark.parametrize("name,args,why", [
    ("nope", {}, "unknown tool"), ("list_bills_due", {"before": "soon"}, "date"),
    ("list_bills_due", {"after": "2026-11-01"}, "unknown field"), ("lookup", {}, "missing required"),
    ("lookup", {"tool": "drop_table"}, "must be one of"), ("propose_fix", {"dish": 3}, "string"),
    ("propose_fix", {"dish": "Tiramisu", "new_price_cents": "12"}, "integer"),
    ("forecast_scenario", {"scenario": {"name": "x", "bogus": 1}}, "unknown field"),
    ("forecast_scenario", {"scenario": {"name": "only a name"}}, "changes nothing"),
    ("scan_market", {"items": "Tiramisu"}, "list"), ("get_menu", None, None), ("get_cash_position", [1], "object"),
])
def test_call_never_raises_and_logs_every_call(session, name, args, why):
    r = tools.call(session, name, args)
    assert session.log[-1]["name"] == name and session.log[-1]["result"] == r
    json.dumps(session.log)
    if why:
        assert why in r["error"]


def test_cash_and_bills_in_cents_with_display(session):
    cash = tools.call(session, "get_cash_position", {})
    assert cash["total_cents"] == 1000000 + 2500000 + 500000 - 34900 + 100000
    assert cash["total_usd"] == "$40,651.00" and {a["account"] for a in cash["accounts"]} == {"CHS", "RES"}
    soon = (Session.today().toordinal() + 14)
    from datetime import date
    bills = tools.call(session, "list_bills_due", {"before": date.fromordinal(soon).isoformat()})
    assert [b["doc"] for b in bills["bills"]] == ["MDG-CHS-1", "BPC-CHS-9"]          # pending only, autopay left out
    assert bills["total_cents"] == 173050 and bills["total_usd"] == "$1,730.50"
    assert tools.call(session, "list_bills_due", {})["count"] == 3


def test_lookup_runs_the_bookkeeper_tool(session):
    r = tools.call(session, "lookup", {"tool": "get_bank_transaction", "args": {"bank_id": "T0000000A2"}})
    assert r["tool"] == "get_bank_transaction" and "34900" in json.dumps(r)
    assert "error" in tools.call(session, "lookup", {"tool": "get_cost", "args": {"nope": 1}})


def test_findings_absent_then_interim_checks_pass_only_cautious_adjustments(session, fake_forecast):
    r = tools.call(session, "get_findings", {})
    assert r["available"] is False and r["why"]
    (session.data / "reports").mkdir()
    (session.data / "reports" / "R-1.json").write_text(json.dumps(BK_REPORT))
    r = tools.call(session, "get_findings", {})
    assert r["available"] and [f["finding_id"] for f in r["findings"]] == ["B1", "B2"]
    assert {c["status"] for c in r["checks"]} == {"downgrade"}
    assert [(a["kind"], a["recoverable"]) for a in r["forecast_uses"]] == [("ongoing_overcharge", False)]
    assert r["findings"][0]["bank_ids"] == ["T0000000A1"]                  # Nessie id mapped to our bank_id
    base = tools.call(session, "forecast_base", {})
    assert base["adjustments_used"] == [{"finding_id": "B1", "kind": "ongoing_overcharge"}]
    assert [a.kind for a in fake_forecast.calls[-1]["adjustments"]] == ["ongoing_overcharge"]


def test_scan_market_offline_read_only_with_fresh_request_ids(session, roles):
    before = dump_hash(session.cost_db)
    a = tools.call(session, "scan_market", {})
    b = tools.call(session, "scan_market", {"items": ["Margherita Pizza"]})
    assert "error" not in a and a["request_id"] != b["request_id"]
    m = next(p for p in a["positions"] if p["our_item"] == "Margherita Pizza")
    assert (m["position"], m["competitor_median_cents"], m["n_prices"]) == ("below_market", 2100, 3)
    assert a["medians"]["Margherita Pizza"]["competitor_median_usd"] == "$21.00"
    assert (session.root / a["filename"]).exists() and a["offline"] is True
    assert dump_hash(session.cost_db) == before and set(roles) == {"reader"}
    assert "error" in tools.call(session, "scan_market", {"items": ["Pineapple Pizza"]})


def test_menu_food_cost_and_upcoming(session):
    menu = tools.call(session, "get_menu", {})
    by = {d["item"]: d for d in menu["dishes"]}
    assert by["Lobster Ravioli"]["food_cost_pct"] >= 0.8 and by["Margherita Pizza"]["food_cost_pct"] < 0.3
    assert "Add Grilled Chicken" not in by                                  # modifiers have no market twin
    assert tools.call(session, "list_upcoming_changes", {})["changes"] == []


def test_forecast_scenario_reports_before_after_and_effect(session):
    r = tools.call(session, "forecast_scenario", {"scenario": {"name": "up", "price_changes_cents": {"Tiramisu": 1400}}})
    assert r["before"]["run_out_likely"] == "2027-06-25" and r["after"]["run_out_likely"] == "2027-06-26"
    assert r["vs_base"]["base_forecast_id"] == r["base_forecast_id"] != r["forecast_id"]
    assert r["monthly_cash_effect_cents"] == 300 and r["monthly_cash_effect_usd"] == "$3.00"
    base = tools.call(session, "forecast_base", {})
    assert base["forecast_id"] == r["base_forecast_id"] and "fridge" in base["not_simulated"]


def test_propose_fix_reprice_to_the_median_passes_ch(session):
    from changes.executor import Executor, check_title
    from changes.shared import Suggestion

    r = tools.call(session, "propose_fix", {"dish": "Margherita Pizza"})
    assert "error" not in r, r
    s = r["suggestion"]
    assert (s["action"], s["target"], s["new_price_cents"]) == ("reprice_item", "Margherita Pizza", 2100)
    assert s["title"] == "Raise Margherita Pizza from $17.50 to $21.00"
    check_title(s["title"], 1750, 2100)
    assert s["run_out_likely_before"] == "2027-06-25" and s["run_out_likely_after"] == "2027-06-28"
    assert s["monthly_cash_effect_cents"] == 1050 and s["assumptions"]
    assert len(s["based_on"]) == 3 and s["based_on"][2] == "Margherita Pizza"
    assert r["path"].startswith("data/suggestions/orch-") and r["reused"] is False   # root-relative
    saved = Suggestion.model_validate_json((session.root / r["path"]).read_text(encoding="utf-8"))
    Executor(session.root).plan(saved)                                     # CH accepts the file as written
    assert r["question"].startswith("Margherita Pizza (dine-in dinner) $17.50 -> $21.00 from ")
    assert r["approve_command"].startswith("python -m uv run --project changes changes --root ")
    assert r["approve_command"].endswith(f"approve {(session.root / r['path']).resolve()} --apply --live")
    p = tools.call(session, "preview_suggestion", {"path": r["path"]})
    assert p["question"] == r["question"] and p["dry_run"]["applied"] is False


def test_propose_fix_caps_at_30_percent_and_rounds(session):
    r = tools.call(session, "propose_fix", {"dish": "Tiramisu", "new_price_cents": 2500})
    assert r["capped"] is True and r["suggestion"]["new_price_cents"] == 1690       # 1.3 x $13.00
    r = tools.call(session, "propose_fix", {"dish": "Tiramisu", "new_price_cents": 1200})
    assert r["capped"] is False and r["suggestion"]["title"] == "Lower Tiramisu from $13.00 to $12.00"
    assert tools.target_price(1750, 2600, whole_dollars=True) == (2200, True)       # 22.75 cap -> $22
    assert tools.target_price(1750, 1000, whole_dollars=True) == (1300, True)       # 12.25 floor -> $13


@pytest.mark.parametrize("args,why", [
    ({"dish": "Pineapple Pizza"}, "that dish isn't on the menu"),
    ({"dish": "Margherita Pizza", "new_price_cents": 1750}, "already costs"),
    ({"dish": "Lobster Ravioli"}, "no competitor median"),
    ({"dish": "Tiramisu", "action": "remove_item", "new_price_cents": 1400}, "belongs only to reprice_item"),
    ({"dish": "Margherita Pizza", "action": "dispute_charge"}, "must be one of"),
])
def test_propose_fix_refuses_and_writes_nothing(session, args, why):
    r = tools.call(session, "propose_fix", args)
    assert why in r["error"]
    assert not list((session.data / "suggestions").glob("*.json")) if (session.data / "suggestions").exists() else True


def test_propose_fix_never_writes_a_file_ch_refuses(session, monkeypatch):
    from changes.executor import Executor, Refused

    def refuse(self, s):
        raise Refused("the title says $99, but the plan is ...")

    monkeypatch.setattr(Executor, "plan", refuse)
    r = tools.call(session, "propose_fix", {"dish": "Margherita Pizza"})
    assert "CH would refuse it" in r["error"]
    assert not (session.data / "suggestions").exists() or not list((session.data / "suggestions").iterdir())


def test_remove_item_suggestion(session):
    r = tools.call(session, "propose_fix", {"dish": "Lobster Ravioli", "action": "remove_item"})
    assert "error" not in r, r
    saved = json.loads((session.root / r["path"]).read_text(encoding="utf-8"))
    assert r["suggestion"]["action"] == "remove_item" and "new_price_cents" not in saved
    assert r["question"].startswith("Take Lobster Ravioli off the menu from ")


def test_preview_only_reads_suggestion_files(session, tmp_path):
    outside = tmp_path / "elsewhere.json"
    outside.write_text("{}")
    err = tools.call(session, "preview_suggestion", {"path": str(outside)})["error"]
    assert "only suggestion files in data/suggestions" in err and str(tmp_path) not in err
    err = tools.call(session, "preview_suggestion", {"path": "missing.json"})["error"]
    assert "isn't in data/suggestions" in err and "missing" not in err


def test_ids_unique_and_valid():
    s = Session(".")
    ids = [s.new_id("orch") for _ in range(2000)] + [s.new_id("a:b/c" * 30)]
    assert len(set(ids)) == len(ids)
    assert all(ID_RE.fullmatch(i) and ":" not in i and len(i) <= 64 for i in ids)
    assert re.fullmatch(r"orch-\d{8}-\d{6}-[0-9a-f]{8}", ids[0])         # seconds + 32 random bits


def test_suggestion_ids_unique_per_run(session):
    a = tools.call(session, "propose_fix", {"dish": "Tiramisu", "new_price_cents": 1400})
    b = tools.call(session, "propose_fix", {"dish": "Tiramisu", "new_price_cents": 1500})
    assert a["suggestion"]["suggestion_id"] != b["suggestion"]["suggestion_id"] and a["path"] != b["path"]


def test_ids_do_not_collide_across_sessions():
    a, b = Session("."), Session(".")
    assert not {a.new_id("orch") for _ in range(400)} & {b.new_id("orch") for _ in range(400)}


async def test_call_and_acall_inside_a_running_loop(session):
    assert "total_cents" in tools.call(session, "get_cash_position", {})          # sync call, loop running
    r = await tools.acall(session, "scan_market", {"items": ["Margherita Pizza"]})
    assert "error" not in r
    rs = await asyncio.gather(tools.acall(session, "get_menu", {}), tools.acall(session, "list_bills_due", {}))
    assert all("error" not in x for x in rs)
