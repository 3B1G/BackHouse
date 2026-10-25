"""Round-3 red-team findings: the y/N question comes from the plan, not the AI's title (1a-1d), and a killed run's
lock doesn't block the retry (2). One regression test each."""
import json
import os
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

import changes.executor as executor
from conftest import PASS, approval, suggestion
from cost_price import api, tty
from cost_price.db import connect
from changes import cli
from changes.executor import DRY_RUN, RESTAURANT_TZ, Executor, Refused
from test_executor import STRETCH, TODAY, approved, ex

TIRAMISU = dict(target="Tiramisu", new_price_cents=1690)
POC = dict(**TIRAMISU, title="Raise Tiramisu from $13 to $14")                # the owner would have signed $16.90


@pytest.fixture
def owner(monkeypatch, tmp_path):
    """A person at a console who answers y; every prompt they were shown is recorded."""
    prompts = []

    def ask(prompt=""):
        prompts.append(prompt)
        return "y" if prompt.startswith("Approve") else ""
    monkeypatch.setattr(tty, "is_console", lambda: True)
    monkeypatch.setattr(tty.getpass, "getpass", lambda prompt="": PASS)
    monkeypatch.setattr("builtins.input", ask)
    monkeypatch.chdir(tmp_path)
    return prompts


def approve(root, tmp_path, s):
    path = tmp_path / "s.json"
    path.write_text(s.model_dump_json(), encoding="utf-8")
    cli.main(["--root", str(root), "approve", str(path)])


def tomorrow():
    return (datetime.now(timezone.utc).astimezone(RESTAURANT_TZ).date() + timedelta(days=1)).isoformat()


def line(e, obj):
    return cli.question(*e.plan(obj), TODAY)


# ---------------------------------------------------------------- 1a: the question line is the plan
def test_question_line_is_built_from_the_plan_not_the_title(root, db, tmp_path, owner, capsys):
    approve(root, tmp_path, suggestion(**TIRAMISU, title="Tiramisu: a small raise"))
    out = capsys.readouterr().out
    assert [p for p in owner if p.startswith("Approve")] == [
        f"Approve: Tiramisu (dine-in dinner) $13.00 -> $16.90 from {tomorrow()}? [y/N] "]
    assert 'AI\'s title: "Tiramisu: a small raise"' in out
    with connect(db) as con:
        assert json.loads(api.latest_approval(con, "S1")["subject_json"])["planned_change"]["price_cents"] == 1690


def test_question_line_for_every_action(root, key):
    e = ex(root, key)
    assert line(e, suggestion(**STRETCH)) == "Mission Dry Goods: 2 bills paid 10 days later (10-31 -> 11-10, 11-07 -> 11-17)"
    assert line(e, suggestion(suggestion_id="P1", action="pay_bill", target="MDG-CHS-2", title="pay")) == \
        "Pay MDG-CHS-2 today (2026-10-24)"
    assert line(e, suggestion(action="remove_item", target="Lobster Ravioli")) == \
        "Take Lobster Ravioli off the menu from 2026-10-25"
    assert line(e, suggestion(action="dispute_charge", target="T68E9A4CFB", title="d")) == "Dispute T68E9A4CFB"
    s = suggestion(**TIRAMISU, title="Raise Tiramisu from $13 to $16.90")
    assert line(e, s) == "Tiramisu (dine-in dinner) $13.00 -> $16.90 from 2026-10-25"
    assert e.apply(approved(e, s), live=True).applied
    assert line(e, {"revert": "C-S1-1"}) == "Undo C-S1-1: Tiramisu back to $13.00 from 2026-10-25"


# ---------------------------------------------------------------- 1b: dollar figures in a reprice title
def test_title_dollars_must_match_the_planned_prices(root, key, db, tmp_path, owner, capsys):
    e = ex(root, key)
    with pytest.raises(Refused, match=r"title says \$14, but the plan is \$13.00 -> \$16.90"):
        e.record_approval(suggestion(**POC), approval())
    assert "title says $14" in e.apply(suggestion(**POC)).error                 # a dry run refuses it too
    with pytest.raises(SystemExit):                                             # the PoC, end to end: never asked
        approve(root, tmp_path, suggestion(**POC))
    assert "title says $14" in capsys.readouterr().err and owner == []
    with connect(db) as con:
        assert api.latest_approval(con, "S1") is None
    for ok in ("Raise Tiramisu from $13 to $16.90", "Tiramisu $13.00 -> $16.90", "Tiramisu to $16.90", "Tiramisu up"):
        e.subject(suggestion(**TIRAMISU, title=ok))


# ---------------------------------------------------------------- 1c: fields that don't belong to the action
@pytest.mark.parametrize("kw, field", [
    (dict(action="remove_item", target="Tiramisu", new_price_cents=1500), "new_price_cents"),
    (dict(action="pay_bill", target="MDG-CHS-2", new_price_cents=1500), "new_price_cents"),
    (dict(action="dispute_charge", target="T68E9A4CFB", new_price_cents=1500), "new_price_cents"),
    (dict(action="pay_bill", target="MDG-CHS-2", extra_days=5), "extra_days"),
    (dict(target="Tiramisu", new_price_cents=1400, extra_days=5, title="t"), "extra_days"),
])
def test_fields_that_do_not_belong_to_the_action_are_refused(root, key, kw, field):
    e = ex(root, key)
    s = suggestion(**kw)
    with pytest.raises(Refused, match=f"{field} belongs only to"):              # at approve
        e.record_approval(s, approval())
    for live in (False, True):                                                  # and at apply
        r = e.apply(s, live=live)
        assert not r.applied and f"{field} belongs only to" in r.error and r.error != DRY_RUN
    assert e.log() == []


# ---------------------------------------------------------------- 1d: the AI's claims are labelled, still signed
def test_ai_claims_are_labelled_and_signed(root, db, tmp_path, owner, capsys):
    approve(root, tmp_path, suggestion(**TIRAMISU, title="t", monthly_cash_effect_cents=-12000,
                                       run_out_likely_before=date(2026, 12, 1)))
    assert ("AI/forecast claims, not checked by CH: monthly cash effect -$120.00; run out likely before 2026-12-01"
            in capsys.readouterr().out)
    with connect(db) as con:
        signed = json.loads(api.latest_approval(con, "S1")["subject_json"])
    assert signed["monthly_cash_effect_cents"] == -12000 and signed["run_out_likely_before"] == "2026-12-01"


# ---------------------------------------------------------------- 2: a killed run's lock
HOLDER = ("import sys, time\nfrom changes.executor import Executor\n"
          "with Executor(sys.argv[1])._claim(sys.argv[2]):\n    print('held', flush=True)\n    time.sleep(120)\n")


def python(*argv, **kw):
    """The real interpreter, so Popen.pid is the process that holds the lock (a Windows venv's python.exe is a
    launcher that runs it as a child); this test's sys.path makes `changes` importable."""
    exe = getattr(sys, "_base_executable", None) or sys.executable
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    return subprocess.Popen([exe, *argv], env=env, **kw)


def hold_lock(root, sid):
    p = python("-c", HOLDER, str(root), sid, stdout=subprocess.PIPE, text=True)
    assert p.stdout.readline().strip() == "held"
    return p


def lock_path(root, sid):
    return root / "data" / "changes-locks" / f"{executor._b32(sid)}.lock"


def test_lock_of_a_killed_run_is_cleared(root, key):
    e = ex(root, key)
    s = approved(e)
    p = hold_lock(root, "S1")
    try:
        r = e.apply(s, live=True)                                               # a live holder still blocks
        assert not r.applied and "already running" in r.error
    finally:
        p.kill()                                                                # taskkill /F: no cleanup runs
        p.wait(timeout=30)
    assert f"pid {p.pid} at " in lock_path(root, "S1").read_text()
    assert e.apply(s, live=True).applied and not lock_path(root, "S1").exists()
    assert os.listdir(root / "data" / "changes-locks") == []


def test_unparsable_lock_is_cleared(root, key):
    e = ex(root, key)
    s = approved(e)
    lock_path(root, "S1").parent.mkdir()
    lock_path(root, "S1").write_text("crashed")
    assert e.apply(s, live=True).applied and not lock_path(root, "S1").exists()


def test_pid_alive():
    assert executor.pid_alive(os.getpid())
    p = python("-c", "pass")
    p.wait(timeout=30)
    assert not executor.pid_alive(p.pid) and not executor.pid_alive(0)
