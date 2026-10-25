import json
import os
import sqlite3
from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import NOW, FakeNessie, approval, dump_hash, suggestion
from cost_price import api
from cost_price.db import connect, transaction
from changes.executor import DRY_RUN, Executor, Refused, receipt_id, receipt_name

TODAY = date(2026, 10, 24)
STRETCH = dict(suggestion_id="S2", action="stretch_supplier", target="Mission Dry Goods", extra_days=10, title="t")


def ex(root, key=None, nessie=None, today=TODAY, now=NOW):
    return Executor(root, key, nessie=nessie or FakeNessie(), today=today, now=now)


def price(root, item, on):
    with connect(root / "data" / "cost_price.db") as con:
        return api.effective_price(con, item, on)


def approved(e, s=None):
    """What the CLI's `approve` does after a person says yes."""
    s = s or suggestion()
    e.record_approval(s, approval(s.suggestion_id))
    return s


def revert_ok(e, cid):
    e.record_approval({"revert": cid}, approval(f"revert:{cid}"))
    return cid


def audit_lines(root):
    path = root / "data" / "audit" / "changes.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()] if path.exists() else []


def listing(root):
    return sorted(os.listdir(root / "data"))


def resync(root, nessie):
    """What `python nessie_sync.py` does: the sync DB mirrors the bank."""
    with sqlite3.connect(root / "data" / "nessie_sync_boh-1loc.db") as con:
        for b, d in nessie.bills.items():
            con.execute("UPDATE bank_bills SET payment_date=? WHERE nessie_id=?", (d, b))
    con.close()


# ---------------------------------------------------------------- dry runs
def test_dry_run_needs_no_key_and_writes_nothing(root, db):
    before = dump_hash(db)
    files = listing(root)
    r = Executor(root, nessie=FakeNessie(), today=TODAY).apply(suggestion())         # no key, no approval
    assert not r.applied and r.error == DRY_RUN
    assert [(c.old_value, c.new_value) for c in r.changes] == [(1750, 1950)]
    r = Executor(root, today=TODAY).apply(suggestion(**STRETCH))
    assert [(c.ref, c.new_value) for c in r.changes] == [("b-1", "2026-11-10"), ("b-2", "2026-11-17")]
    assert Executor(root).revert("C-S1-1").error == "refused: no change C-S1-1"      # dry-run refusal: no trace
    assert Executor(root).log() == [] and "Tiramisu" in Executor(root).menu("2026-10-25")
    assert listing(root) == files                                                    # p6: no -wal/-shm left behind
    assert dump_hash(db) == before                                                   # byte-identical content
    assert not (root / "data" / "audit").exists()


def test_dry_run_keeps_existing_wal_files(root, db):
    with connect(db, "changes") as con:                       # a writer is open: -wal and -shm exist
        files = listing(root)
        assert {"cost_price.db-wal", "cost_price.db-shm"} <= set(files)
        assert Executor(root, today=TODAY).apply(suggestion()).error == DRY_RUN
        assert listing(root) == files


def test_dry_run_after_approval_writes_nothing_either(root, key, db):
    e = ex(root, key)
    s = approved(e)
    before, lines, files = dump_hash(db), audit_lines(root), listing(root)
    assert e.apply(s).error == DRY_RUN
    assert listing(root) == files and dump_hash(db) == before and audit_lines(root) == lines


# ---------------------------------------------------------------- approvals
def test_live_apply_needs_no_key_but_needs_the_signed_approval(root, key):
    assert "no recorded owner approval" in ex(root).apply(suggestion(), live=True).error
    approved(ex(root, key))
    r = ex(root).apply(suggestion(), live=True)                                # no key: signature checked, not made
    assert r.applied
    with pytest.raises(PermissionError, match="owner key"):
        ex(root).record_approval(suggestion(suggestion_id="S9"), approval("S9"))


def test_reprice_takes_effect_the_next_day_and_keeps_history(root, key, db):
    e = ex(root, key)
    r = e.apply(approved(e), live=True)
    assert r.applied and r.error is None
    assert price(root, "Margherita Pizza", "2026-10-24") == 1750               # history unchanged
    assert price(root, "Margherita Pizza", "2026-10-25") == 1950
    with sqlite3.connect(root / "data" / "books.db") as con:                   # books.db untouched
        assert con.execute("SELECT count(*) FROM menu_prices").fetchone()[0] == 3


def test_old_approval_never_backdates(root, key):
    e = ex(root, key, today=date(2026, 10, 25), now=datetime(2026, 10, 25, 17, 0, tzinfo=timezone.utc))
    e.apply(approved(e), live=True)                                            # approved 10-24, applied 10-25
    assert price(root, "Margherita Pizza", "2026-10-25") == 1750
    assert price(root, "Margherita Pizza", "2026-10-26") == 1950


def test_approval_older_than_24h_is_refused(root, key):                       # p10
    e = ex(root, key, today=date(2026, 11, 20), now=datetime(2026, 11, 20, 17, 0, tzinfo=timezone.utc))
    r = e.apply(approved(e), live=True)
    assert not r.applied and "more than 24 h old" in r.error and e.log() == []


def test_applying_twice_returns_the_first_result(root, key):
    e = ex(root, key)
    s = approved(e)
    first = e.apply(s, live=True)
    assert e.apply(s, live=True) == first
    assert len(e.log()) == 1


def test_approval_is_bound_to_the_content_the_owner_saw(root, key):
    e = ex(root, key)
    approved(e)                                                                # owner approved $19.50
    r = e.apply(suggestion(new_price_cents=1550), live=True)                         # someone swaps in $15.50
    assert not r.applied and "changed after the owner approved" in r.error
    r = e.apply(suggestion(suggestion_id="S7"), live=True)                     # nobody approved S7
    assert not r.applied and "no recorded owner approval" in r.error
    assert e.log() == []


@pytest.mark.tampered
def test_forged_approval_is_refused(root, key):
    class Forger:                                                              # can write the DB, lacks the passphrase
        public_hex = key.public_hex

        def sign(self, payload):
            return "ab" * 64

    s = approved(ex(root, Forger()))
    r = ex(root).apply(s, live=True)
    assert not r.applied and "not signed by the owner's key" in r.error
    assert ex(root).log() == []


def test_rejection_and_mismatched_approval(root, key):
    e = ex(root, key)
    s = suggestion()
    e.record_approval(s, approval(decision="rejected"))
    assert "rejected" in e.apply(s, live=True).error
    with pytest.raises(ValueError, match="approval is for S9"):
        e.record_approval(s, approval(sid="S9"))
    approved(e)
    e.record_approval(s, approval(decision="rejected"))                        # the latest decision counts
    assert "rejected" in e.apply(s, live=True).error


def test_path_traversal_id_is_refused(root, key):
    e = ex(root, key)
    for sid in ("../../evil", "a/b", "x" * 101, ""):
        assert "must match" in e.apply(suggestion(suggestion_id=sid)).error
        with pytest.raises(ValueError, match="must match"):
            e.record_approval(suggestion(suggestion_id=sid), approval(sid))
    assert "must match" in e.revert("..\\C-S1-1").error


# ---------------------------------------------------------------- p5: revert namespace
def test_suggestion_ids_cannot_squat_the_revert_namespace(root, key):
    e = ex(root, key)
    e.apply(approved(e), live=True)                                            # C-S1-1: 1750 -> 1950
    squat = suggestion(suggestion_id="revert:C-S1-1", target="Tiramisu", new_price_cents=1400, title="Tiramisu to $14")
    with pytest.raises(Refused, match="must match"):
        e.record_approval(squat, approval("revert:C-S1-1"))
    assert "must match" in e.apply(squat, live=True).error
    r = e.revert(revert_ok(e, "C-S1-1"), live=True)
    assert r.applied and [(c.ref, c.new_value) for c in r.changes] == [("Margherita Pizza", 1750)]
    assert price(root, "Margherita Pizza", "2026-10-26") == 1750


def test_applied_lookup_is_keyed_by_action_and_target(root, key):
    e = ex(root, key)
    e.apply(approved(e), live=True)
    other = approved(e, suggestion(action="remove_item", target="Tiramisu"))   # same id S1
    r = e.apply(other, live=True)
    assert not r.applied and "already applied as reprice_item" in r.error


@pytest.mark.parametrize("kw, why", [
    (dict(new_price_cents=1750), "already costs"),
    (dict(new_price_cents=3000), "more than 30%"),
    (dict(new_price_cents=1200), "more than 30%"),
    (dict(target="Pizza Hawaii"), "no dine-in dinner price"),
    (dict(action="other"), "not something CH can apply"),
    (dict(action="stretch_supplier", target="Mission Dry Goods", extra_days=31), "1..30"),
    (dict(action="stretch_supplier", target="Nobody Inc", extra_days=5), "no pending Nessie bills"),
    (dict(action="pay_bill", target="NOPE-1"), "exactly one pending bill"),
    (dict(action="dispute_charge", target="T000000000"), "no bank row"),
    (dict(action="dispute_charge", target="nope"), "not a bank_id"),
    (dict(action="remove_item", target="Pizza Hawaii"), "not on the menu"),
])
def test_bad_suggestions_are_refused(root, key, kw, why):
    e = ex(root, key)
    s = suggestion(**kw)
    if s.action in ("stretch_supplier", "pay_bill", "reprice_item", "remove_item"):   # the plan is signed: refused at approve
        with pytest.raises(Refused, match=why.replace(".", r"\.")):
            approved(e, s)
        assert why in e.apply(s, live=True).error
    else:
        r = e.apply(approved(e, s), live=True)
        assert not r.applied and why in r.error
    assert e.log() == []


def test_missing_sync_db_is_a_refusal_not_a_crash(root, key):
    e = ex(root, key)
    s = suggestion(**STRETCH)
    s3 = approved(e, suggestion(suggestion_id="S3", action="dispute_charge", target="T68E9A4CFB"))
    (root / "data" / "nessie_sync_boh-1loc.db").unlink()
    with pytest.raises(Refused, match="run nessie_sync.py"):
        approved(e, s)
    assert "run nessie_sync.py" in e.apply(s, live=True).error
    assert "run nessie_sync.py" in Executor(root).apply(s).error              # dry run too
    assert "run nessie_sync.py" in e.apply(s3, live=True).error


def test_missing_cost_price_db_is_a_refusal(tmp_path):
    r = Executor(tmp_path).apply(suggestion())
    assert not r.applied and "FileNotFoundError" in r.error


# ---------------------------------------------------------------- menu changes and reverts
def test_remove_item_and_revert_of_revert(root, key, db):
    e = ex(root, key)
    e.apply(approved(e, suggestion(action="remove_item", target="Lobster Ravioli")), live=True)
    assert "Lobster Ravioli" in e.menu("2026-10-24")
    assert "Lobster Ravioli" not in e.menu("2026-10-25")
    assert e.revert(revert_ok(e, "C-S1-1"), live=True).applied
    assert "Lobster Ravioli" in e.menu("2026-10-25")
    cid = e.log()[-1]["change_id"]
    assert e.revert(revert_ok(e, cid), live=True).applied                      # undo the undo: off the menu again
    assert "Lobster Ravioli" not in e.menu("2026-10-25")
    with connect(db) as con:
        assert [r[0] for r in con.execute("SELECT available FROM availability_change ORDER BY id")] == [0, 1, 0]
        assert set(api.change_validity(con).values()) == {None}                # every row in the signed shape


def test_price_revert_refuses_to_undo_a_later_change(root, key):
    e = ex(root, key)
    e.apply(approved(e), live=True)                                             # 1750 -> 1950
    e.apply(approved(e, suggestion(suggestion_id="S2", new_price_cents=2100)), live=True)   # 1950 -> 2100
    with pytest.raises(Refused, match="later change would be undone"):          # the plan is signed: refused at approve
        revert_ok(e, "C-S1-1")
    r = e.revert("C-S1-1", live=True)
    assert not r.applied and "later change would be undone" in r.error
    assert price(root, "Margherita Pizza", "2026-10-25") == 2100


def test_revert_needs_its_own_approval(root, key):
    e = ex(root, key)
    e.apply(approved(e), live=True)
    r = e.revert("C-S1-1", live=True)
    assert not r.applied and "no recorded owner approval" in r.error


def test_revert_restores_price_and_bill(root, key):
    n = FakeNessie()
    e = ex(root, key, n)
    e.apply(approved(e), live=True)
    s = suggestion(suggestion_id="S2", action="stretch_supplier", target="Bay Produce Co", extra_days=7, title="t")
    e.apply(approved(e, s), live=True)
    assert e.revert(revert_ok(e, "C-S1-1"), live=True).applied
    assert e.revert(revert_ok(e, "C-S2-b-4"), live=True).applied
    assert price(root, "Margherita Pizza", "2026-10-25") == 1750
    assert n.bills["b-4"] == "2026-11-02"
    assert [r["reverts"] for r in e.log()] == [None, None, "C-S1-1", "C-S2-b-4"]


# ---------------------------------------------------------------- bills
def test_stretch_moves_only_that_suppliers_pending_bills(root, key, db):
    n = FakeNessie()
    e = ex(root, key, n)
    s = suggestion(**STRETCH)
    assert e.subject(s)["planned_bills"] == [dict(bill_id="b-1", from_date="2026-10-31", to_date="2026-11-10"),
                                             dict(bill_id="b-2", from_date="2026-11-07", to_date="2026-11-17")]
    assert e.apply(approved(e, s), live=True).applied
    assert n.puts == [("b-1", "2026-11-10"), ("b-2", "2026-11-17")]             # not the autopay b-3, not b-4
    assert sorted(x["change_id"] for x in e.log()) == ["C-S2-b-1", "C-S2-b-2"]
    with connect(db) as con:
        assert {b: (i["from_date"], i["to_date"]) for b, i in api.intents(con, "S2").items()} == {
            "b-1": ("2026-10-31", "2026-11-10"), "b-2": ("2026-11-07", "2026-11-17")}


def test_stretch_refuses_a_bill_changed_since_the_sync(root, key):
    n = FakeNessie()
    n.bills["b-2"] = "2026-12-01"                                               # someone moved it in the bank
    e = ex(root, key, n)
    r = e.apply(approved(e, suggestion(**STRETCH)), live=True)
    assert not r.applied and "resync" in r.error
    assert n.puts == [("b-1", "2026-11-10")] and len(e.log()) == 1              # the first move is logged, not lost


class FlakyNessie(FakeNessie):
    """The PUT fails before reaching the bank."""

    def __init__(self, fail_on):
        super().__init__()
        self.fail_on = fail_on

    def set_payment_date(self, bill_id, day):
        if bill_id == self.fail_on:
            self.fail_on = None
            raise ConnectionError("network down")
        super().set_payment_date(bill_id, day)


def test_network_error_mid_stretch_logs_what_moved_and_retry_never_double_moves(root, key):
    n = FlakyNessie(fail_on="b-2")
    e = ex(root, key, n)
    s = approved(e, suggestion(**STRETCH))
    r = e.apply(s, live=True)
    assert not r.applied and "ConnectionError" in r.error
    assert [x["ref"] for x in e.log()] == ["b-1"]                               # b-1 moved and is on record
    resync(root, n)                                                             # the sync DB now shows b-1 moved
    r = e.apply(s, live=True)
    assert r.applied
    assert n.bills["b-1"] == "2026-11-10" and n.bills["b-2"] == "2026-11-17"    # +10 each, never +20
    assert sorted(x["change_id"] for x in e.log()) == ["C-S2-b-1", "C-S2-b-2"]


def test_interrupted_stretch_without_resync_never_double_moves(root, key):
    n = FlakyNessie(fail_on="b-2")
    e = ex(root, key, n)
    s = approved(e, suggestion(**STRETCH))
    e.apply(s, live=True)
    assert e.apply(s, live=True).applied                                        # sync DB still shows b-1's old date
    assert n.puts == [("b-1", "2026-11-10"), ("b-2", "2026-11-17")]


class LostResponse(FakeNessie):
    """p3: the PUT lands at the bank, but its response is lost; optionally the bank is unreachable right after."""

    def __init__(self, reads_down=0):
        super().__init__()
        self.fail, self.reads_down = True, reads_down

    def set_payment_date(self, bill_id, day):
        super().set_payment_date(bill_id, day)
        if self.fail:
            self.fail = False
            raise TimeoutError("read timed out")

    def get_bill(self, bill_id):
        if not self.fail and self.reads_down:
            self.reads_down -= 1
            raise ConnectionError("network down")
        return super().get_bill(bill_id)


def test_lost_put_response_confirmed_by_the_reread_is_logged(root, key):
    n = LostResponse()
    e = ex(root, key, n)
    r = e.apply(approved(e, suggestion(**STRETCH)), live=True)
    assert r.applied and n.puts == [("b-1", "2026-11-10"), ("b-2", "2026-11-17")]
    assert [x["ref"] for x in e.log()] == ["b-1", "b-2"]


def test_lost_put_response_then_resync_and_retry_moves_once(root, key):     # p3
    n = LostResponse(reads_down=1)
    e = ex(root, key, n)
    s = approved(e, suggestion(**STRETCH))
    r = e.apply(s, live=True)
    assert not r.applied and "retry" in r.error and e.log() == []               # moved, but nobody could confirm it
    resync(root, n)                                                             # the operator resyncs: b-1 at 11-10
    r = e.apply(s, live=True)
    assert r.applied
    assert n.bills == {**FakeNessie().bills, "b-1": "2026-11-10", "b-2": "2026-11-17"}   # never 11-20
    assert n.puts == [("b-1", "2026-11-10"), ("b-2", "2026-11-17")]
    assert [(x["ref"], x["old_value"], x["new_value"]) for x in e.log()] == [
        ("b-1", "2026-10-31", "2026-11-10"), ("b-2", "2026-11-07", "2026-11-17")]


def test_intent_retry_puts_the_recorded_target_and_refuses_stale_bills(root, key):
    n = FlakyNessie(fail_on="b-1")
    e = ex(root, key, n)
    s = approved(e, suggestion(**STRETCH))
    assert not e.apply(s, live=True).applied                                    # intent b-1 written, PUT failed
    assert e.apply(s, live=True).applied and n.bills["b-1"] == "2026-11-10"     # live = intent.from: PUT intent.to
    n2 = FlakyNessie(fail_on="b-4")
    e2 = ex(root, key, n2)
    s2 = approved(e2, suggestion(**{**STRETCH, "suggestion_id": "S3", "target": "Bay Produce Co"}))
    assert not e2.apply(s2, live=True).applied                                  # intent b-4: 11-02 -> 11-12
    n2.bills["b-4"] = "2026-11-05"                                              # someone else moved it meanwhile
    r = e2.apply(s2, live=True)
    assert not r.applied and "not 2026-11-02" in r.error and n2.puts == []


def test_rejection_mid_run_stops_new_moves_and_logs_done_ones(root, key, db):  # p4
    s = suggestion(**STRETCH)
    owner = ex(root, key)

    class RejectDuringFirstPut(FakeNessie):
        def set_payment_date(self, b, d):
            super().set_payment_date(b, d)
            if len(self.puts) == 1:
                owner.record_approval(s, approval("S2", "rejected"))

    n = RejectDuringFirstPut()
    e = ex(root, key, n)
    approved(e, s)
    r = e.apply(s, live=True)                                                   # reports, doesn't crash
    assert not r.applied and "rejected" in r.error
    assert n.puts == [("b-1", "2026-11-10")]                                    # b-2 never started
    assert [x["ref"] for x in e.log()] == ["b-1"]                               # the move that happened is on record


def test_rejection_after_the_last_put_still_logs_everything(root, key):
    s = suggestion(**STRETCH)
    owner = ex(root, key)

    class RejectDuringLastPut(FakeNessie):
        def set_payment_date(self, b, d):
            super().set_payment_date(b, d)
            if len(self.puts) == 2:
                owner.record_approval(s, approval("S2", "rejected"))

    n = RejectDuringLastPut()
    e = ex(root, key, n)
    approved(e, s)
    r = e.apply(s, live=True)
    assert not r.applied and "rejected" in r.error and len(r.changes) == 2
    assert [x["ref"] for x in e.log()] == ["b-1", "b-2"]


def test_database_error_during_execute_is_reported(root, key, monkeypatch):
    e = ex(root, key)
    s = approved(e)

    def boom(*a, **kw):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(api, "add_price_change", boom)
    r = e.apply(s, live=True)
    assert not r.applied and "database is locked" in r.error and e.log() == []


def test_new_bill_after_approval_is_refused(root, key):                        # p10
    n = FakeNessie()
    e = ex(root, key, n)
    s = approved(e, suggestion(**{**STRETCH, "extra_days": 30}))
    with sqlite3.connect(root / "data" / "nessie_sync_boh-1loc.db") as con:
        con.execute("INSERT INTO bank_bills VALUES ('b-9','pending','Mission Dry Goods','MDG-CHS-9 due 2026-12-01',"
                    "'2026-12-01')")
    con.close()
    n.bills["b-9"], n.status["b-9"] = "2026-12-01", "pending"
    r = e.apply(s, live=True)
    assert not r.applied and "differ from the ones the owner approved" in r.error and n.puts == []


def test_pay_bill_refuses_a_bill_that_is_no_longer_pending(root, key):         # p10
    n = FakeNessie()
    n.status["b-2"] = "cancelled"
    e = ex(root, key, n)
    s = suggestion(suggestion_id="P1", action="pay_bill", target="MDG-CHS-2", title="pay")
    r = e.apply(approved(e, s), live=True)
    assert not r.applied and "not pending" in r.error and n.puts == []


def test_pay_bill_uses_today_and_dispute_writes_a_dispute(root, key, db):
    n = FakeNessie()
    e = ex(root, key, n)
    s = suggestion(suggestion_id="S2", action="pay_bill", target="BPC-CHS-9", title="Pay Bay Produce now")
    assert e.subject(s)["planned_bills"] == [dict(bill_id="b-4", from_date="2026-11-02", to_date="2026-10-24")]
    assert e.apply(approved(e, s), live=True).applied and n.puts == [("b-4", "2026-10-24")]
    s = suggestion(suggestion_id="S3", action="dispute_charge", target="T68E9A4CFB", title="Dispute $349")
    r = e.apply(approved(e, s), live=True)
    assert r.applied and r.changes[0].system == "other"
    with connect(db) as con:
        row = dict(con.execute("SELECT * FROM dispute").fetchone())
    assert row["change_id"] == "C-S3-1" and row["bank_id"] == "T68E9A4CFB" and row["state"] == "open"
    assert row["opened_at"] == "2026-10-24T20:00:00Z"
    assert e.revert(revert_ok(e, "C-S3-1"), live=True).applied
    with connect(db) as con:
        assert [r[0] for r in con.execute("SELECT state FROM dispute ORDER BY id")] == ["open", "withdrawn"]


# ---------------------------------------------------------------- storage
@pytest.mark.parametrize("table", ["change_log", "price_change", "approval", "applied", "nessie_intent"])
def test_every_table_is_append_only(root, key, db, table):
    e = ex(root, key)
    e.apply(approved(e), live=True)
    e.apply(approved(e, suggestion(**STRETCH)), live=True)                       # fills nessie_intent too
    con = sqlite3.connect(db, isolation_level=None)                              # even bypassing cost_price's guards
    con.create_function("cp_role", 0, lambda: "outsider")
    with pytest.raises(sqlite3.DatabaseError, match="append-only"):
        con.execute(f"DELETE FROM {table}")
    con.close()


def test_change_log_needs_an_approval_even_outside_ch(root, key, db):
    with connect(db, "changes") as con, pytest.raises(sqlite3.IntegrityError):
        with transaction(con):
            api.log_change(con, change_id="C-X-1", suggestion_id="X", approval_id=1, system="menu_prices",
                           ref="Tiramisu", field="price_cents", old_value=1300, new_value=1)


def test_audit_lines_use_the_shared_shape(root, key):
    e = ex(root, key)
    e.apply(approved(e), live=True)
    e.apply(suggestion(suggestion_id="S9"), live=True)                          # a live refusal is audited
    lines = audit_lines(root)
    assert [(x["actor"], x["action"]) for x in lines] == [("owner", "approval"), ("executor", "change"),
                                                          ("executor", "refused")]
    assert set(lines[0]) == {"at", "actor", "action", "ref", "summary", "data"}


# ---------------------------------------------------------------- p9: receipt names
@pytest.mark.parametrize("id_", ["S1", "revert:C-S1-1", "a.b", "a_b", "A.B", "..", "CON", "x" * 100, "revert:C-revert:C-S1-1-1"])
def test_receipt_names_are_reversible(id_):
    name = receipt_name(id_)
    assert receipt_id(name) == id_ and name.endswith(".approval.json")
    assert set(name.removesuffix(".approval.json")) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_receipt_names_never_collide_even_case_insensitively():
    ids = ["a.b", "a_b", "a:b", "A.B", "a-b", "revert:C-S1-1", "revert_C-S1-1", "S1", "s1"]
    assert len({receipt_name(i).lower() for i in ids}) == len(ids)


def test_reserved_receipt_stems_are_refused(monkeypatch):
    import changes.executor as executor
    monkeypatch.setattr(executor.base64, "b32encode", lambda b: b"NUL=====")
    with pytest.raises(Refused, match="reserved"):
        receipt_name("anything")
    for stem in ("CON", "PRN", "AUX", "NUL", "COM1", "COM9", "LPT1", "LPT9"):
        assert stem in executor.WINDOWS_RESERVED


def test_zero_or_missing_days_are_refused_by_the_contract_itself():
    """contracts v2: extra_days must be > 0 and stretch_supplier must carry it, before CH ever sees the suggestion."""
    from pydantic import ValidationError
    for kw in (dict(extra_days=0), dict()):
        with pytest.raises(ValidationError):
            suggestion(action="stretch_supplier", target="Mission Dry Goods", **kw)
    with pytest.raises(ValidationError):
        suggestion(new_price_cents=None)                                     # a reprice without a price
