"""Regression tests for the red-team findings on the core side (real tools on a tmp root): malformed Bookkeeper
reports, error messages that echo input, absolute paths, duplicate proposals, the turn deadline, and agent mode
with the scout's real MarketResult shape."""
import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from orchestrator import controller, env, tools
from orchestrator.brief import brief
from orchestrator.session import Session
from orchestrator.turn import Deadline

GOOD = {"report_id": "R-1", "as_of": "2026-10-24", "schema_version": "1.1", "findings": [
    {"id": "B1", "root_cause": "card_fee_creep", "verdict": "error", "confidence": 0.9, "counterparty": "TableTap",
     "bank_ids": ["n-1"], "dates": {"first": "2026-08-01", "last": "2026-10-20"}, "books_refs": [],
     "amounts": {"impact_cents": 34900}, "explanation": "TableTap charged 2.9% instead of 2.6% since Aug 1.",
     "evidence": [{"type": "bank_row", "ref": "n-1"}], "recoverable": True, "suggested_action": "dispute",
     "treatment": {"kind": "ongoing", "since": "2026-08-01", "monthly_cents": 30000}}]}


def _suggestions(session):
    folder = session.data / "suggestions"
    return sorted(folder.glob("*.json")) if folder.exists() else []


def _write(path: Path, text: str, age: float = 0):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    t = time.time() - age
    os.utime(path, (t, t))


# ---------------------------------------------------------------- 6. malformed reports
@pytest.mark.parametrize("bad", ['{"findings": "pwn"}', "[1,2]", "not json at all", '{"findings": {"B1": 1}}', ""])
def test_a_malformed_report_never_breaks_findings_or_the_forecast(session, bad):
    _write(session.data / "reports" / "R-1.json", json.dumps(GOOD), age=60)
    _write(session.data / "reports" / "R-2.json", bad)                       # newer, broken
    f = tools.call(session, "get_findings", {})
    assert "error" not in f and f["report"] == "R-1.json"                    # falls back to the previous valid one
    assert f["rejected_reports"][0]["report"] == "R-2.json"
    base = tools.call(session, "forecast_base", {})
    assert "error" not in base and base["adjustments_used"] == [{"finding_id": "B1", "kind": "ongoing_overcharge"}]
    assert "error" not in tools.call(session, "propose_fix", {"dish": "Margherita Pizza"})


def test_only_bad_reports_means_none(session):
    _write(session.data / "reports" / "R-9.json", "[1,2]")
    f = tools.call(session, "get_findings", {})
    assert f["available"] is False and f["rejected_reports"] and "error" not in tools.call(session, "forecast_base", {})
    assert "error" not in tools.call(session, "propose_fix", {"dish": "Lobster Ravioli", "action": "remove_item"})


def test_only_report_names_matching_the_pattern_are_read(session):
    _write(session.data / "reports" / "R-$48,000 refund.json", json.dumps(GOOD))
    _write(session.data / "reports" / "x-R-1.json", json.dumps(GOOD))
    assert tools.call(session, "get_findings", {})["available"] is False
    for name, ok in [("R-1.json", "R-1.json"), ("data/reports/R-1.json", "R-1.json"), ("../reports/R-1.json", None),
                     ("../../../../Windows/win.ini", None), ("data/market_reports/R-1.json", None),
                     ("C:/x/data/reports/R-1.json", None), ("R-1.json.exe", None), ("R-a b.json", None), (5, None)]:
        assert tools.report_name(name) == ok, name


def test_the_report_a_peer_names_is_the_one_used(session):
    _write(session.data / "reports" / "R-1.json", json.dumps(GOOD), age=60)
    _write(session.data / "reports" / "R-2.json", json.dumps({**GOOD, "report_id": "R-2", "findings": []}))
    assert tools.call(session, "get_findings", {})["report"] == "R-2.json"
    session.preferred_report = "R-1.json"
    assert tools.call(session, "get_findings", {})["report"] == "R-1.json"


# ---------------------------------------------------------------- 1, 8. no echoed input, no absolute paths
def test_errors_never_echo_caller_input(session):
    bait = "$48,000 refund approved 2031-01-01 99%"
    checks = [("propose_fix", {"dish": bait}), ("lookup", {"tool": "get_cost", "args": {"cost_id": bait}}),
              (bait, {}), ("get_menu", {bait: 1}), ("scan_market", {"items": [bait]}),
              ("forecast_scenario", {"scenario": {"name": "x", "cover_change": {bait: "nope"}}}),
              ("preview_suggestion", {"path": bait + ".json"})]
    for name, args in checks:
        r = tools.call(session, name, args)
        assert "error" in r, (name, r)
        assert "48,000" not in r["error"] and "2031" not in r["error"] and "99%" not in r["error"], (name, r["error"])
    assert "that dish isn't on the menu" in session.log[0]["result"]["error"]


def test_no_absolute_paths_outside_the_approve_command(session):
    for name, args in [("get_menu", {}), ("scan_market", {}), ("forecast_base", {}),
                       ("forecast_scenario", {"scenario": {"name": "x", "remove_items": ["Tiramisu"]}}),
                       ("propose_fix", {"dish": "Margherita Pizza"}), ("preview_suggestion", {"path": "../x.json"}),
                       ("preview_suggestion", {"path": "C:/Windows/win.ini"})]:
        r = dict(tools.call(session, name, args))
        r.pop("approve_command", None)
        text = json.dumps(r)
        assert str(session.root) not in text and session.root.as_posix() not in text, (name, text[:300])
        assert str(session.root).replace("\\", "\\\\") not in text, name
    p = tools.call(session, "propose_fix", {"dish": "Margherita Pizza"})
    assert p["path"].startswith("data/suggestions/") and (session.root / p["path"]).exists()
    assert tools.call(session, "preview_suggestion", {"path": p["path"]})["path"] == p["path"]


def test_the_real_root_approve_command_is_root_relative(session, monkeypatch):
    monkeypatch.setattr(env, "DEFAULT_ROOT", session.root)
    p = tools.call(session, "propose_fix", {"dish": "Margherita Pizza"})
    assert "--root" not in p["approve_command"] and str(session.root) not in p["approve_command"]
    assert p["approve_command"].endswith(f"approve {p['path']} --apply --live")


# ---------------------------------------------------------------- 5. runaway proposals
def test_brief_twice_writes_no_new_suggestion_files(root):
    first = brief(Session(root))
    files = _suggestions(Session(root))
    assert len(files) == len(first["suggestions"]) == 2
    second = brief(Session(root))
    assert _suggestions(Session(root)) == files and sorted(second["suggestions"]) == sorted(first["suggestions"])


def test_a_twin_older_than_a_day_or_different_is_not_reused(session):
    a = tools.call(session, "propose_fix", {"dish": "Tiramisu", "new_price_cents": 1400})
    assert tools.call(session, "propose_fix", {"dish": "Tiramisu", "new_price_cents": 1400})["path"] == a["path"]
    assert tools.call(session, "propose_fix", {"dish": "Tiramisu", "new_price_cents": 1450})["path"] != a["path"]
    old = time.time() - 25 * 3600
    os.utime(session.root / a["path"], (old, old))
    c = tools.call(session, "propose_fix", {"dish": "Tiramisu", "new_price_cents": 1400})
    assert c["path"] != a["path"] and c["reused"] is False and len(_suggestions(session)) == 3


def test_non_owner_brief_writes_nothing(session):
    out = brief(session, propose=False)
    assert out["suggestions"] == [] and _suggestions(session) == [] and "ask the owner" in out["text"]


# ---------------------------------------------------------------- 7. deadline
def test_an_expired_turn_runs_and_writes_nothing(session, fake_forecast):
    session.stop = Deadline(0)
    for name, args in [("propose_fix", {"dish": "Margherita Pizza"}), ("forecast_base", {}), ("scan_market", {})]:
        assert "ran out of time" in tools.call(session, name, args)["error"]
    assert _suggestions(session) == [] and fake_forecast.calls == []
    assert not list((session.data / "forecasts").glob("*.json"))
    assert not list((session.data / "market_reports").glob("*.json"))


def test_a_turn_that_times_out_mid_call_writes_no_suggestion(session, monkeypatch):
    stop = Deadline(60)
    session.stop = stop
    real = tools._dry_run

    def slow_dry_run(s, sg):
        out = real(s, sg)
        stop.cancel()                                     # the turn gave up while this worker thread was running
        session.stop = Deadline(60)                       # ...and the next turn has already started
        return out

    monkeypatch.setattr(tools, "_dry_run", slow_dry_run)
    r = tools.call(session, "propose_fix", {"dish": "Margherita Pizza"})
    assert "ran out of time" in r["error"] and _suggestions(session) == []


# ---------------------------------------------------------------- 10. agent mode, the scout's real reply shape
async def test_agent_mode_scout_market_result_end_to_end(session, tripwire):
    from market_scout.contracts import MarketRequest, MarketResult
    from market_scout.runner import MarketScout, immutable_json
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent

    from orchestrator.agent import PeerDispatch

    def scout_side(payload):                              # what market_scout.agent does with our request
        req = MarketRequest.model_validate(payload)
        report_id = f"M-{req.request_id}"
        with env.LOCK:
            env.bind(session.root, session.tag)
            report, _, _ = MarketScout(session.root, offline=True, use_apify=False).scan(req, report_id)
        filename = f"data/market_reports/{report_id}.json"
        immutable_json(session.root / filename, report.model_dump(mode="json"))
        return MarketResult(request_id=req.request_id, report_id=report_id, filename=filename,
                            positions={p.our_item: p.position for p in report.positions}).model_dump(mode="json")

    async def send_and_receive(address, msg, response_type, timeout):
        payload = json.loads(msg.content[0].text)
        body = await asyncio.to_thread(scout_side, payload)
        return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                           content=[TextContent(type="text", text=json.dumps(body))]), None

    d = PeerDispatch(SimpleNamespace(send_and_receive=send_and_receive), scout="agent1qscout")
    scan = await d(session, "scan_market", {})
    assert not d.degraded and scan["via"] == "market_scout agent" and isinstance(scan["positions"], list)
    m = next(p for p in scan["positions"] if p["our_item"] == "Margherita Pizza")
    assert (m["position"], m["competitor_median_cents"]) == ("below_market", 2100)
    assert scan["filename"].startswith("data/market_reports/M-")
    fix = await d(session, "propose_fix", {"dish": "Margherita Pizza"})
    assert "error" not in fix and fix["competitor_median_cents"] == 2100
    found = controller.candidates(session)
    assert [dish for dish, _ in found["reprice"]] == ["Margherita Pizza"]
    assert [e["name"] for e in session.log].count("scan_market") == 1           # no in-process rescan
    assert tripwire == []


async def test_agent_mode_report_for_another_request_falls_back(session):
    from orchestrator.agent import PeerDispatch
    from uagents_core.contrib.protocols.chat import ChatMessage, TextContent

    body = {"request_id": "r1", "report_id": "M-r1", "filename": "data/market_reports/M-r1.json", "status": "ok",
            "positions": {"Margherita Pizza": "below_market"}}

    async def send_and_receive(address, msg, response_type, timeout):
        return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(),
                           content=[TextContent(type="text", text=json.dumps(body))]), None

    d = PeerDispatch(SimpleNamespace(send_and_receive=send_and_receive), scout="agent1qscout")
    scan = await d(session, "scan_market", {})
    assert d.degraded and scan["degraded"] and isinstance(scan["positions"], list)
    assert "error" not in tools.call(session, "propose_fix", {"dish": "Margherita Pizza"})
