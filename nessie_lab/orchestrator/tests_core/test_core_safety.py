"""The orchestrator never writes cost_price.db, never talks to Nessie, never approves or applies, and its brief needs
no LLM and no network; every money figure it prints is in a tool result."""
import json
import re
import shutil
import sys
from pathlib import Path

import pytest

from conftest import dump_hash
from orchestrator import controller, env, tools
from orchestrator.brief import brief
from orchestrator.session import Session

SRC = Path(__file__).resolve().parents[1] / "src" / "orchestrator"
CORE = ["env.py", "session.py", "fmt.py", "tools.py", "controller.py", "brief.py", "checks_interim.py", "checks_bk.py", "api.py", "cli.py"]
MONEY = re.compile(r"-?\$\d[\d,]*\.\d{2}")


def _all_results(session) -> str:
    return json.dumps([e["result"] for e in session.log], ensure_ascii=False)


def test_brief_without_llm_or_network(session, no_network, no_llm_key, roles, tripwire):
    before = dump_hash(session.cost_db)
    out = brief(session)
    assert set(out) == {"text", "facts", "suggestions"}
    text = out["text"]
    assert "Cash on " in text and "likely reaches $0 on 2027-06-25" in text
    assert "Not simulated by default" in text and "fridge" in text
    assert "no Bookkeeper report yet" in text
    assert "Margherita Pizza: ours $17.50, competitor median $21.00 from 3 restaurants (below market)" in text
    assert "Take Lobster Ravioli off the menu" in text and " approve " in text and "--apply --live" in text
    assert all(not Path(p).is_absolute() and (session.root / p).exists() for p in out["suggestions"])
    assert len(out["suggestions"]) == 2
    assert no_network == [] and tripwire == []
    assert set(roles) == {"reader"}
    assert dump_hash(session.cost_db) == before


def test_every_money_figure_in_the_brief_is_in_a_tool_result(session):
    out = brief(session)
    results = _all_results(session)
    figures = MONEY.findall(out["text"])
    assert figures, out["text"]
    missing = [f for f in figures if f not in results]
    assert missing == [], missing
    for d in re.findall(r"\b\d{4}-\d{2}-\d{2}\b", out["text"]):              # dates too
        assert d in results, d


def test_controller_ranks_and_includes_the_removal(session):
    fixes = controller.propose_fixes(session, max_fixes=3)
    assert [(f["suggestion"]["action"], f["suggestion"]["target"]) for f in fixes] == [
        ("reprice_item", "Margherita Pizza"), ("remove_item", "Lobster Ravioli")]
    assert len({f["suggestion"]["suggestion_id"] for f in fixes}) == 2
    assert controller.propose_fixes(session, max_fixes=0)[0]["suggestion"]["action"] == "remove_item"
    scans = [e for e in session.log if e["name"] == "scan_market"]
    assert len(scans) == 1                                                  # one scan, reused


def test_only_reader_connections_across_every_tool(session, roles, tripwire):
    for name, args in [("get_menu", {}), ("list_upcoming_changes", {}), ("scan_market", {}),
                       ("propose_fix", {"dish": "Margherita Pizza"}), ("forecast_base", {})]:
        assert "error" not in tools.call(session, name, args), name
    path = next(e["result"]["path"] for e in session.log if e["name"] == "propose_fix")
    assert "error" not in tools.call(session, "preview_suggestion", {"path": path})
    assert roles and set(roles) == {"reader"}
    assert tripwire == []


def test_never_imports_nessie_client(session):
    brief(session)
    assert not [m for m in sys.modules if m.endswith("nessie_client")]
    for name in CORE:
        source = (SRC / name).read_text(encoding="utf-8")
        assert "nessie_client" not in source and "LiveNessie" not in source, name


def test_core_source_never_calls_approve_apply_live_or_unlock():
    pattern = re.compile(r"live\s*=\s*True|record_approval|\bunlock\(|read_passphrase|is_console|ask_owner|"
                         r"read_key|\.revert\(|connect\([^)]*\"(scout|changes|owner|build)\"")
    for name in CORE:
        hits = pattern.findall((SRC / name).read_text(encoding="utf-8"))
        assert not hits, (name, hits)


def test_dry_run_must_report_dry_run(session, monkeypatch):
    from changes.executor import Executor
    from changes.shared import ExecutionResult

    monkeypatch.setattr(Executor, "apply", lambda self, s, live=False: ExecutionResult(
        suggestion_id=s.suggestion_id, applied=False, error="refused: something"))
    r = tools.call(session, "propose_fix", {"dish": "Margherita Pizza"})
    assert "dry run did not pass" in r["error"]
    assert not (session.data / "suggestions").exists() or not list((session.data / "suggestions").iterdir())


REAL_DATA = env.NESSIE_LAB / "data"


@pytest.mark.realdata
@pytest.mark.skipif(not (REAL_DATA / "cost_price.db").exists() or not (REAL_DATA / "features.db").exists(),
                    reason="generated nessie_lab/data not present")
def test_brief_on_a_copy_of_the_real_data(tmp_path, no_network, no_llm_key, roles, tripwire):
    """The real Forecaster, Market scout (saved pages) and CH dry run on a scratch copy (data/grader/ is skipped)."""
    shutil.copytree(REAL_DATA, tmp_path / "data", ignore=lambda d, names: ["grader"] if Path(d) == REAL_DATA else [])
    assert not (tmp_path / "data" / "grader").exists()
    session = Session(tmp_path)
    out = brief(session)
    text = out["text"]
    assert "Cash on 2026-10-24: $157,631.48" in text
    assert "Lobster Ravioli" in text and out["suggestions"]
    missing = [f for f in MONEY.findall(text) if f not in _all_results(session)]
    assert missing == []
    assert set(roles) == {"reader"} and tripwire == [] and no_network == []
    from changes.executor import Executor
    from changes.shared import Suggestion
    for p in out["suggestions"]:
        Executor(tmp_path).plan(Suggestion.model_validate_json((tmp_path / p).read_text(encoding="utf-8")))
