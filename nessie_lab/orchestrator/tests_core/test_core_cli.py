import json
import sys

from orchestrator import cli


def test_tools_lists_every_tool(capsys):
    assert cli.main(["tools"]) == 0
    out = capsys.readouterr().out
    assert "propose_fix(dish, new_price_cents?, action?)" in out and "scan_market(items?, service?)" in out


def test_call_with_json_text_and_with_a_file(root, capsys, tmp_path, open_gates):
    assert cli.main(["--root", str(root), "call", "get_menu", '{"on": "2026-10-24"}']) == 0
    assert json.loads(capsys.readouterr().out)["on"] == "2026-10-24"
    args = tmp_path / "args.json"
    args.write_text('﻿{"dish": "Tiramisu", "new_price_cents": 1400}', encoding="utf-8")   # PowerShell BOM
    assert cli.main(["--root", str(root), "call", "propose_fix", str(args)]) == 0
    r = json.loads(capsys.readouterr().out)
    assert r["suggestion"]["new_price_cents"] == 1400
    assert cli.main(["--root", str(root), "call", "propose_fix", f"@{args}"]) == 0
    again = json.loads(capsys.readouterr().out)
    assert again["reused"] is True and again["path"] == r["path"]          # the same proposal: no second file
    assert cli.main(["--root", str(root), "call", "get_menu", "{not json"]) == 1
    assert "error" in json.loads(capsys.readouterr().out)
    assert cli.main(["--root", str(root), "suggestions"]) == 0
    assert capsys.readouterr().out.count("reprice_item Tiramisu") == 1


def test_brief_offline_by_default_with_root_after_the_command(root, capsys, monkeypatch):
    seen = []
    import orchestrator.brief as b
    real = b.brief
    monkeypatch.setattr(b, "brief", lambda s: seen.append(s) or real(s))
    assert cli.main(["brief", "--root", str(root)]) == 0
    assert seen[0].offline is True and seen[0].root == root.resolve()
    assert "Cash on " in capsys.readouterr().out


def test_ask_falls_back_to_the_brief_without_a_turn_module(root, capsys, monkeypatch, no_network):
    monkeypatch.setitem(sys.modules, "orchestrator.turn", None)            # import fails
    assert cli.main(["--root", str(root), "ask", "how are we doing?"]) == 0
    out = capsys.readouterr().out
    assert "no LLM turn available" in out and "Cash on " in out


def test_serve_without_agent_module(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "orchestrator.agent", None)
    assert cli.main(["serve"]) == 1
    assert "not available" in capsys.readouterr().out
