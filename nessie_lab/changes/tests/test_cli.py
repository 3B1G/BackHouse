import json
import os
import subprocess
import sys

import pytest

from conftest import NOW, PASS, FakeNessie, approval, dump_hash, suggestion
from cost_price import api, tty
from cost_price.db import connect
from changes import cli
from changes.executor import Executor, receipt_name


@pytest.fixture
def owner_at_keyboard(monkeypatch, tmp_path):
    """A person at a console: passphrase via getpass, answers via input()."""
    answers, calls = [], []
    monkeypatch.setattr(tty, "is_console", lambda: True)
    monkeypatch.setattr(tty.getpass, "getpass", lambda prompt="": calls.append(prompt) or PASS)
    monkeypatch.setattr("builtins.input", lambda prompt="": answers.pop(0))
    monkeypatch.chdir(tmp_path)
    return answers, calls


@pytest.fixture
def no_console(monkeypatch, tmp_path):
    monkeypatch.setattr(tty, "is_console", lambda: False)
    monkeypatch.setattr(tty.getpass, "getpass", lambda prompt="": pytest.fail("getpass must not be called"))
    monkeypatch.setattr("builtins.input", lambda prompt="": pytest.fail("input must not be called"))
    monkeypatch.chdir(tmp_path)


def write(tmp_path, s):
    path = tmp_path / "s.json"
    path.write_text(s.model_dump_json(), encoding="utf-8")
    return path


def approvals(db):
    with connect(db) as con:
        return con.execute("SELECT count(*) FROM approval").fetchone()[0]


def run(root, *argv):
    cli.main(["--root", str(root), *argv])


def test_approve_then_apply_with_one_passphrase(root, db, tmp_path, owner_at_keyboard, capsys):
    answers, calls = owner_at_keyboard
    answers += ["y", ""]
    run(root, "approve", str(write(tmp_path, suggestion())), "--apply", "--live")
    assert len(calls) == 1 and '"applied": true' in capsys.readouterr().out
    receipt = tmp_path / receipt_name("S1")
    assert json.loads(receipt.read_text(encoding="utf-8"))["subject"]["new_price_cents"] == 1950
    with connect(db) as con:
        assert api.effective_price(con, "Margherita Pizza", "2099-01-01") == 1950


def test_approve_shows_and_signs_the_planned_bills(root, db, tmp_path, owner_at_keyboard, capsys):
    owner_at_keyboard[0].extend(["y", ""])
    s = suggestion(suggestion_id="S2", action="stretch_supplier", target="Mission Dry Goods", extra_days=10, title="t")
    run(root, "approve", str(write(tmp_path, s)))
    assert '"planned_bills"' in capsys.readouterr().out
    with connect(db) as con:
        signed = json.loads(api.latest_approval(con, "S2")["subject_json"])
    assert [b["bill_id"] for b in signed["planned_bills"]] == ["b-1", "b-2"]


def test_approve_no_records_a_rejection(root, db, tmp_path, owner_at_keyboard, capsys):
    owner_at_keyboard[0].extend(["n", "too much"])
    run(root, "approve", str(write(tmp_path, suggestion())), "--apply", "--live")
    assert "rejected" in capsys.readouterr().out
    with connect(db) as con:
        assert api.latest_approval(con, "S1")["decision"] == "rejected" and api.change_log(con) == []


def test_wrong_passphrase_is_refused(root, db, tmp_path, owner_at_keyboard, monkeypatch, capsys):
    monkeypatch.setattr(tty.getpass, "getpass", lambda prompt="": "not the passphrase")
    with pytest.raises(SystemExit):
        run(root, "approve", str(write(tmp_path, suggestion())))
    assert "wrong passphrase" in capsys.readouterr().err and approvals(db) == 0


def test_piped_stdin_is_refused(root, db, tmp_path, no_console, capsys):
    for argv in (["approve", str(write(tmp_path, suggestion()))], ["approve", "--revert", "C-S1-1"]):
        with pytest.raises(SystemExit) as exc:
            run(root, *argv)
        assert exc.value.code == 1
        assert "typed at a terminal" in capsys.readouterr().err
    assert approvals(db) == 0 and not list(tmp_path.glob("*.approval.json"))


def test_live_apply_without_a_console_still_needs_the_signed_approval(root, db, tmp_path, no_console, capsys):
    run(root, "apply", str(write(tmp_path, suggestion())), "--live")              # p7: no passphrase is asked...
    assert "no recorded owner approval" in json.loads(capsys.readouterr().out)["error"]
    run(root, "revert", "C-S1-1", "--live")
    assert "no change C-S1-1" in json.loads(capsys.readouterr().out)["error"]
    with connect(db) as con:                                                      # ...and nothing was written
        assert api.change_log(con) == []


def test_redirected_stdin_is_refused_in_a_real_process(root, db, tmp_path):        # p7: `< NUL` / </dev/null
    s = write(tmp_path, suggestion())
    code = "import sys; from changes.cli import main; main(sys.argv[1:])"
    p = subprocess.run([sys.executable, "-c", code, "--root", str(root), "approve", str(s)], cwd=tmp_path,
                       stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60,
                       env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert p.returncode == 1 and "typed at a terminal" in p.stderr
    assert approvals(db) == 0


@pytest.mark.parametrize("argv", [["approve", "--revert", "../../evil"], ["approve", "--revert", "..\\x"],
                                  ["revert", "C/../x", "--live"]])
def test_path_traversal_change_id_is_rejected(root, db, tmp_path, owner_at_keyboard, capsys, argv):
    with pytest.raises(SystemExit):
        run(root, *argv)
    assert "must match" in capsys.readouterr().err
    assert approvals(db) == 0 and not list(tmp_path.rglob("*.approval.json"))
    assert not list(tmp_path.parent.parent.glob("*evil*")) and not list(tmp_path.parent.glob("*evil*"))


def test_path_traversal_suggestion_id_is_rejected(root, db, tmp_path, owner_at_keyboard, capsys):
    with pytest.raises(SystemExit):
        run(root, "approve", str(write(tmp_path, suggestion(suggestion_id="../../evil"))))
    assert "must match" in capsys.readouterr().err and approvals(db) == 0


def test_approval_file_stays_in_the_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    for id_ in ("revert:C-S1-1", "..", "CON", "a.b", "a_b"):
        out = cli.approval_file(id_)
        assert out.parent == tmp_path.resolve() and out.name == receipt_name(id_)
    assert cli.approval_file("a.b") != cli.approval_file("a_b")


def test_revert_via_approve(root, key, tmp_path, owner_at_keyboard, capsys):
    e = Executor(root, key, nessie=FakeNessie(), now=NOW)
    s = suggestion()
    e.record_approval(s, approval())
    assert e.apply(s, live=True).applied
    owner_at_keyboard[0].extend(["y", ""])
    run(root, "approve", "--revert", "C-S1-1", "--apply", "--live")
    assert '"applied": true' in capsys.readouterr().out
    assert (tmp_path / receipt_name("revert:C-S1-1")).exists()


def test_apply_dry_run_needs_no_passphrase(root, db, tmp_path, no_console, capsys):
    before = dump_hash(db)
    files = sorted(os.listdir(root / "data"))
    run(root, "apply", str(write(tmp_path, suggestion())))
    out = json.loads(capsys.readouterr().out)
    assert out["changes"][0]["new_value"] == 1950 and not out["applied"]
    assert sorted(os.listdir(root / "data")) == files
    assert dump_hash(db) == before
    assert not (root / "data" / "audit").exists()
