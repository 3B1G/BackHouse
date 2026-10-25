"""cost_price v3 (signed planned_change) and the round-2 red-team findings (p1-p9), one regression test each."""
import json
import os
import sqlite3
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import changes.executor as executor
from conftest import APPROVED_AT, NOW, FakeNessie, approval, suggestion
from cost_price import api
from cost_price.db import connect, transaction
from changes.executor import DRY_RUN, Executor, Refused
from changes.shared import Approval
from test_executor import STRETCH, approved, audit_lines, ex, price, revert_ok

PIZZA = dict(table="price_change", item="Margherita Pizza", channel="dine_in", service="dinner",
             effective_from="2026-10-25", price_cents=1950)


def signed(db, sid):
    with connect(db) as con:
        return json.loads(api.latest_approval(con, sid)["subject_json"])


def intents(db, sid):
    with connect(db) as con:
        return {b: (i["from_date"], i["to_date"], i["approval_id"]) for b, i in api.intents(con, sid).items()}


class FailingPut(FakeNessie):
    """The PUT never reaches the bank while `fail` is set."""
    fail = True

    def set_payment_date(self, bill_id, day):
        if self.fail:
            raise RuntimeError("500")
        super().set_payment_date(bill_id, day)


# ---------------------------------------------------------------- A: cost_price v3
def test_menu_subjects_sign_the_planned_change(root, key, db):
    e = ex(root, key)
    assert e.subject(suggestion())["planned_change"] == PIZZA
    assert e.subject(suggestion(action="remove_item", target="Tiramisu"))["planned_change"] == dict(
        table="availability_change", item="Tiramisu", effective_from="2026-10-25", available=0)
    assert "planned_change" not in e.subject(suggestion(**STRETCH))
    assert e.apply(approved(e), live=True).applied
    with connect(db) as con:
        row = dict(con.execute("SELECT * FROM price_change").fetchone())
        assert api.planned_change("price_change", row) == signed(db, "S1")["planned_change"] == PIZZA
        assert set(api.change_validity(con).values()) == {None}
    revert = e.subject({"revert": "C-S1-1"})
    assert revert == {"revert": "C-S1-1", "planned_change": {**PIZZA, "price_cents": 1750}}
    assert e.revert(revert_ok(e, "C-S1-1"), live=True).applied
    assert price(root, "Margherita Pizza", "2026-10-25") == 1750


def test_bill_revert_subject_is_just_the_revert(root, key):
    e = ex(root, key)
    e.apply(approved(e, suggestion(**{**STRETCH, "target": "Bay Produce Co"})), live=True)
    assert e.subject({"revert": "C-S2-b-4"}) == {"revert": "C-S2-b-4"}


def test_date_rollover_needs_a_new_approval(root, key):
    approved(ex(root, key))                                                    # signed: effective 2026-10-25
    later = ex(root, key, today=date(2026, 10, 25), now=datetime(2026, 10, 25, 16, 0, tzinfo=timezone.utc))
    r = later.apply(suggestion(), live=True)
    assert not r.applied and "date rolled over" in r.error and "2026-10-26" in r.error and later.log() == []
    later.record_approval(suggestion(), Approval(suggestion_id="S1", decision="approved",
                                                 decided_at=datetime(2026, 10, 25, 15, 0, tzinfo=timezone.utc)))
    assert later.apply(suggestion(), live=True).applied
    assert price(root, "Margherita Pizza", "2026-10-25") == 1750 and price(root, "Margherita Pizza", "2026-10-26") == 1950


# ---------------------------------------------------------------- B1: superseded intents (p1, p8)
def test_superseded_intent_never_overrides_the_new_approval(root, key, db):                  # p1
    n = FailingPut()
    e = ex(root, key, n)
    approved(e, suggestion(**{**STRETCH, "extra_days": 7}))
    assert not e.apply(suggestion(**{**STRETCH, "extra_days": 7}), live=True).applied
    assert intents(db, "S2") == {"b-1": ("2026-10-31", "2026-11-07", 1)}
    two = suggestion(**{**STRETCH, "extra_days": 2})                           # the owner changes their mind
    with pytest.raises(Refused, match="superseded approval #1.*new suggestion id"):
        approved(e, two)
    n.fail = False
    r = e.apply(two, live=True)
    assert not r.applied and "new suggestion id" in r.error and n.puts == []
    fresh = suggestion(**{**STRETCH, "suggestion_id": "S2b", "extra_days": 2})
    assert e.subject(fresh)["planned_bills"][0] == dict(bill_id="b-1", from_date="2026-10-31", to_date="2026-11-02")
    assert e.apply(approved(e, fresh), live=True).applied
    assert n.puts == [("b-1", "2026-11-02"), ("b-2", "2026-11-09")]


def test_superseded_intent_that_agrees_with_the_new_plan_is_reused(root, key):
    n = FailingPut()
    e = ex(root, key, n)
    s = approved(e, suggestion(**STRETCH))
    e.apply(s, live=True)                                                     # intent b-1 under approval 1
    e.record_approval(s, approval("S2", "rejected"))
    approved(e, s)                                                            # the same plan, approval 3
    n.fail = False
    assert e.apply(s, live=True).applied and n.puts == [("b-1", "2026-11-10"), ("b-2", "2026-11-17")]
    assert [x["approval_id"] for x in e.log()] == [3, 3]


def test_stale_pay_bill_intent_is_never_put_in_the_past(root, key):                         # p8
    n = FailingPut()
    s = suggestion(suggestion_id="P1", action="pay_bill", target="MDG-CHS-2", title="pay")
    e = ex(root, key, n)
    approved(e, s)
    assert not e.apply(s, live=True).applied                                  # intent b-2 -> 2026-10-24
    day4 = ex(root, key, n, today=date(2026, 10, 27), now=NOW + timedelta(days=3))
    with pytest.raises(Refused, match="new suggestion id"):
        day4.record_approval(s, Approval(suggestion_id="P1", decision="approved", decided_at=APPROVED_AT +
                                         timedelta(days=3)))
    n.fail = False
    assert "new suggestion id" in day4.apply(s, live=True).error and n.puts == []


def test_no_bill_is_put_before_today(root, key):
    n = FakeNessie()
    e = ex(root, key, n)
    e.apply(approved(e, suggestion(**{**STRETCH, "target": "Bay Produce Co"})), live=True)   # b-4 11-02 -> 11-12
    late = ex(root, key, n, today=date(2026, 11, 5), now=datetime(2026, 11, 5, 18, 0, tzinfo=timezone.utc))
    late.record_approval({"revert": "C-S2-b-4"}, Approval(suggestion_id="revert:C-S2-b-4", decision="approved",
                                                          decided_at=datetime(2026, 11, 5, 17, 0, tzinfo=timezone.utc)))
    r = late.revert("C-S2-b-4", live=True)
    assert not r.applied and "before today" in r.error and n.puts == [("b-4", "2026-11-12")]


def test_intent_contradicting_extra_days_is_refused(root, key, db):
    e = ex(root, key)
    s = approved(e, suggestion(**STRETCH))                                    # +10 days
    with connect(db, "changes") as con, transaction(con):                     # a writer records +20 for b-1
        api.add_intent(con, suggestion_id="S2", approval_id=1, bill_id="b-1", from_date="2026-10-31",
                       to_date="2026-11-20")
    r = e.apply(s, live=True)
    assert not r.applied and "isn't +10 days" in r.error and e.nessie.puts == []


# ---------------------------------------------------------------- B2: landed move after a rejection (p2)
class LostAfterPut(FakeNessie):
    """The PUT lands, then the connection drops: no answer, and the re-read fails too."""
    down = False

    def set_payment_date(self, bill_id, day):
        super().set_payment_date(bill_id, day)
        self.down = True
        raise ConnectionError("reset")

    def get_bill(self, bill_id):
        if self.down:
            raise ConnectionError("reset")
        return super().get_bill(bill_id)


def test_landed_move_is_logged_even_after_a_rejection(root, key):                           # p2
    n = LostAfterPut()
    e = ex(root, key, n)
    s = approved(e, suggestion(**STRETCH))
    assert "retry" in e.apply(s, live=True).error and n.bills["b-1"] == "2026-11-10" and e.log() == []
    assert [(p["bill_id"], p["to_date"]) for p in e.pending()] == [("b-1", "2026-11-10")]
    e.record_approval(s, approval("S2", "rejected"))
    n.down = False
    r = e.apply(s, live=True)
    assert not r.applied and "rejected" in r.error and "C-S2-b-1-observed" in r.error
    assert [(x["change_id"], x["approval_id"], x["old_value"], x["new_value"]) for x in e.log()] == [
        ("C-S2-b-1-observed", 1, "2026-10-31", "2026-11-10")]
    assert e.pending() == [] and n.puts == [("b-1", "2026-11-10")]


# ---------------------------------------------------------------- B3/B4: bank answers (p3)
def test_payment_date_with_a_time_part_is_normalised(root, key):                             # p3
    class WithTime(FakeNessie):
        def get_bill(self, bill_id):
            return {**super().get_bill(bill_id), "payment_date": self.bills[bill_id] + "T00:00:00"}

    n = WithTime()
    e = ex(root, key, n)
    assert e.apply(approved(e, suggestion(**STRETCH)), live=True).applied
    assert [x["change_id"] for x in e.log()] == ["C-S2-b-1", "C-S2-b-2"]


@pytest.mark.parametrize("bad", ["soon", None, "2026-13-01", ""])
def test_unreadable_payment_date_is_refused(root, key, bad):
    class Garbled(FakeNessie):
        def get_bill(self, bill_id):
            return {**super().get_bill(bill_id), "payment_date": bad}

    e = ex(root, key, Garbled())
    r = e.apply(approved(e, suggestion(**STRETCH)), live=True)
    assert not r.applied and "unreadable payment_date" in r.error and e.nessie.puts == []


def test_non_dict_answer_is_reported_and_audited(root, key):                                  # p3b
    class Weird(FakeNessie):
        def get_bill(self, bill_id):
            return ["error"]

    e = ex(root, key, Weird())
    r = e.apply(approved(e, suggestion(**STRETCH)), live=True)
    assert not r.applied and "TypeError" in r.error and e.nessie.puts == []
    assert audit_lines(root)[-1]["action"] == "change"


def test_unexpected_exception_is_reported_but_keyboard_interrupt_is_not_swallowed(root, key, monkeypatch):
    e = ex(root, key)
    s = approved(e)
    monkeypatch.setattr(api, "add_price_change", lambda *a, **kw: (_ for _ in ()).throw(LookupError("odd")))
    r = e.apply(s, live=True)
    assert not r.applied and "LookupError" in r.error and audit_lines(root)[-1]["action"] == "change"

    class Interrupt(FakeNessie):
        def get_bill(self, bill_id):
            raise KeyboardInterrupt

    e = ex(root, key, Interrupt())
    s2 = approved(e, suggestion(**STRETCH))
    with pytest.raises(KeyboardInterrupt):
        e.apply(s2, live=True)
    assert os.listdir(root / "data" / "changes-locks") == []                  # the claim is released anyway


# ---------------------------------------------------------------- B5: who moved it (p7)
def test_move_by_someone_else_is_logged_as_observed(root, key):                              # p7
    n = FailingPut()
    e = ex(root, key, n)
    s = approved(e, suggestion(**{**STRETCH, "target": "Bay Produce Co"}))
    assert not e.apply(s, live=True).applied and n.puts == []
    n.bills["b-4"] = "2026-11-12"                                             # the supplier portal moved it to the target
    assert e.apply(s, live=True).applied
    assert [x["change_id"] for x in e.log()] == ["C-S2-b-4-observed"]


def test_ch_put_confirmed_on_a_later_run_is_logged_as_chs(root, key):
    class ReadsDownAfterPut(FakeNessie):
        down = 0

        def set_payment_date(self, bill_id, day):
            super().set_payment_date(bill_id, day)
            self.down = 1

        def get_bill(self, bill_id):
            if self.down:
                self.down -= 1
                raise ConnectionError("reset")
            return super().get_bill(bill_id)

    n = ReadsDownAfterPut()
    e = ex(root, key, n)
    s = approved(e, suggestion(**{**STRETCH, "target": "Bay Produce Co"}))
    assert "retry" in e.apply(s, live=True).error                             # PUT answered ok, re-read failed
    assert e.apply(s, live=True).applied
    assert [x["change_id"] for x in e.log()] == ["C-S2-b-4"]                  # the audit has CH's successful PUT


# ---------------------------------------------------------------- B6: the clock (p4)
def test_injected_clock_is_refused_outside_tests(root, key, monkeypatch):                    # p4
    approved(ex(root, key))
    monkeypatch.setattr(executor, "ALLOW_INJECTED_CLOCK", False)
    for e in (Executor(root, now=NOW), Executor(root, today=date(2026, 10, 24))):
        r = e.apply(suggestion(), live=True)
        assert not r.applied and "for tests only" in r.error
        with pytest.raises(Refused, match="for tests only"):
            Executor(root, key, now=NOW).record_approval(suggestion(suggestion_id="S5"), approval("S5"))
    assert Executor(root, now=NOW).apply(suggestion()).error == DRY_RUN       # a dry run may use any clock
    assert ex(root).log() == []


def test_future_approval_is_refused(root, key, db):
    e = ex(root, key)
    with pytest.raises(Refused, match="in the future"):
        e.record_approval(suggestion(), Approval(suggestion_id="S1", decision="approved", decided_at=NOW + timedelta(hours=1)))
    with connect(db, "changes") as con, transaction(con):                      # signed through the API directly
        api.record_approval(con, key, suggestion_id="S1", subject=e.subject(suggestion()), decision="approved",
                            decided_by="owner", decided_at="2099-01-01T00:00:00Z")
    r = e.apply(suggestion(), live=True)
    assert not r.applied and "in the future" in r.error and e.log() == []
    e.record_approval(suggestion(), Approval(suggestion_id="S1", decision="approved", decided_at=NOW + timedelta(minutes=4)))
    assert e.apply(suggestion(), live=True).applied                            # within the 5 minute skew


def test_expiry_is_rechecked_before_each_put(root, key):
    holder = {}

    class SlowBank(FakeNessie):
        def set_payment_date(self, bill_id, day):
            super().set_payment_date(bill_id, day)
            holder["e"]._now = APPROVED_AT + timedelta(hours=25)              # the first PUT took a day

    n = SlowBank()
    e = holder["e"] = ex(root, key, n)
    r = e.apply(approved(e, suggestion(**STRETCH)), live=True)
    assert not r.applied and "more than 24 h old" in r.error
    assert n.puts == [("b-1", "2026-11-10")] and [x["ref"] for x in e.log()] == ["b-1"]


# ---------------------------------------------------------------- B7: one live run per suggestion (p6)
def test_concurrent_apply_is_refused(root, key):                                             # p6
    seen = {}

    class Racing(FakeNessie):
        def get_bill(self, bill_id):
            if "second" not in seen:
                seen["second"] = ex(root, nessie=FakeNessie()).apply(suggestion(**STRETCH), live=True)
            return super().get_bill(bill_id)

    n = Racing()
    e = ex(root, key, n)
    assert e.apply(approved(e, suggestion(**STRETCH)), live=True).applied
    assert not seen["second"].applied and "already running" in seen["second"].error
    assert n.puts == [("b-1", "2026-11-10"), ("b-2", "2026-11-17")]
    assert os.listdir(root / "data" / "changes-locks") == []


def test_stale_lock_is_cleared(root, key):
    e = ex(root, key)
    s = approved(e)
    locks = root / "data" / "changes-locks"
    locks.mkdir()
    lock = locks / f"{executor._b32('S1')}.lock"
    lock.write_text(f"pid {os.getpid()} at 2026-10-24T20:00:00Z\n")            # a live pid: only age clears it
    assert "already running" in e.apply(s, live=True).error
    old = time.time() - 11 * 60
    os.utime(lock, (old, old))
    assert e.apply(s, live=True).applied and not lock.exists()


def test_pending_cli_lists_unlogged_moves(root, key, capsys):
    from changes import cli
    e = ex(root, key, LostAfterPut())
    e.apply(approved(e, suggestion(**STRETCH)), live=True)
    cli.main(["--root", str(root), "pending"])
    rows = [json.loads(x) for x in capsys.readouterr().out.splitlines()]
    assert [(r["suggestion_id"], r["bill_id"], r["to_date"]) for r in rows] == [("S2", "b-1", "2026-11-10")]
