import hashlib
import json
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from cost_price import api
from cost_price.build import build
from cost_price.db import TamperError, append, connect, transaction
from cost_price.owner import OwnerError, init_owner, unlock
from cost_price.verify import seal, seal_path, verify

PASS = "correct horse battery"
def planned(item="Tiramisu", price=1400, on="2026-10-25", channel="dine_in", service="dinner"):
    return {"table": "price_change", "item": item, "channel": channel, "service": service, "effective_from": on,
            "price_cents": price}


def subject(item="Tiramisu", price=1400, on="2026-10-25", **kw):
    return {"suggestion_id": "S1", "action": "reprice_item", "target": item, "new_price_cents": price,
            "planned_change": planned(item, price, on, **kw)}


SUBJECT = subject()


def dump_hash(db):
    con = sqlite3.connect(db)
    try:
        return hashlib.sha256("\n".join(con.iterdump()).encode()).hexdigest()
    finally:
        con.close()


def raw(db):
    """A plain sqlite3 connection with no guards: what someone with the file and their own tools could do."""
    con = sqlite3.connect(db, isolation_level=None)
    con.create_function("cp_role", 0, lambda: "outsider")
    return con


def problems(db, **kw):
    return verify(db, **kw)["problems"]


@pytest.fixture
def key(db):
    with connect(db, "owner") as con:
        init_owner(con, PASS)
        return unlock(con, PASS)


def approve(db, key, sid="S1", subject=SUBJECT, decision="approved", decided_at="2026-10-24T22:00:00Z"):
    with connect(db, "changes") as con, transaction(con):
        return api.record_approval(con, key, suggestion_id=sid, subject=subject, decision=decision, decided_by="owner",
                                   decided_at=decided_at)


def reprice(db, approval_id, cid="C-S1-1", sid="S1", item="Tiramisu", old=1350, new=1400, on="2026-10-25",
            channel="dine_in"):
    with connect(db, "changes") as con, transaction(con):
        api.log_change(con, change_id=cid, suggestion_id=sid, approval_id=approval_id, system="menu_prices", ref=item,
                       field="price_cents", old_value=old, new_value=new)
        api.add_price_change(con, change_id=cid, item=item, effective_from=on, price_cents=new, channel=channel)


def price(db, item="Tiramisu", on="2026-10-25"):
    with connect(db) as con:
        return api.effective_price(con, item, on)


# ---------------------------------------------------------------- build
def test_reference_layer_and_plate_costs(db):
    with connect(db) as con:
        assert api.restaurant(con)["name"] == "Demo Bistro"
        assert api.plate_cost(con, "Margherita Pizza", "2026-09") == 86      # (0.5*0.60 + 0.25*2.00) * 1.08 -> 86.4
        assert api.plate_cost(con, "Margherita Pizza", "2026-10") == 113     # tomato 3.00 in Oct, flour carried forward
        assert api.plate_cost(con, "Tiramisu") == 130
        row = con.execute("SELECT unit_cost_millicents FROM ingredient_cost_month WHERE ingredient='flour'").fetchone()
        assert row[0] == 60000                                               # qty-weighted $0.60/lb


def test_rebuild_is_idempotent_and_deterministic(root, db):
    first = dump_hash(db)
    build(root, seed=root / "seed.json")
    assert dump_hash(db) == first
    for f in ("cost_price.db", "cost_price.db-wal", "cost_price.db-shm"):
        (root / "data" / f).unlink(missing_ok=True)
    build(root, seed=root / "seed.json")
    assert dump_hash(db) == first
    assert problems(db, require_seal=False) == []


def test_rebuild_keeps_history(root, db):
    with connect(db, "scout") as con, transaction(con):
        api.add_check(con, 1, "verified", snapshot_sha256="a" * 64, report_id="M-1")
    build(root, seed=root / "seed.json")
    with connect(db) as con:
        assert con.execute("SELECT count(*) FROM observation_check").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM competitor_observation").fetchone()[0] == 3     # seed not doubled


# ---------------------------------------------------------------- competitors: keyed by registrable domain
@pytest.mark.parametrize("a, b, same", [
    ("https://www.alpha.example/menu", "https://alpha.example/other", True),
    ("https://order.alpha.example/x", "https://menu.alpha.example/y", True),       # subdomains: one restaurant
    ("https://www.www.alpha.example", "https://ALPHA.example./", True),
    ("https://www.toasttab.com/rest-a/menu", "https://www.toasttab.com/rest-b", False),   # shared host: two restaurants
    ("https://a.square.site/", "https://b.square.site/", False),
    ("https://shop.example.co.uk", "https://other.example.co.uk", True),
])
def test_domain_keying(a, b, same):
    assert (api.domain_of(a) == api.domain_of(b)) is same


def test_idn_is_stored_as_punycode(db):
    assert api.domain_of("https://café.example/menu") == "xn--caf-dma.example"


def test_competitors_merge_by_domain(db):
    with connect(db, "scout") as con, transaction(con):
        same = api.add_competitor(con, name="Alpha (renamed)", menu_url="https://alpha.example/other",
                                  found_via="apify_google_maps")
    assert same == 1


# ---------------------------------------------------------------- our menu and the effective-price rule
def test_menu_and_effective_price(db):
    with connect(db) as con:
        prices, latest = api.menu(con, "2026-10-25")
        assert prices == {"Margherita Pizza": 1750, "Tiramisu": 1350} and latest == "2026-04-01"
        assert api.effective_price(con, "Tiramisu", "2026-03-31") == 1300
        assert api.effective_price(con, "Margherita Pizza", "2026-10-25", "delivery") == 2000
        with pytest.raises(ValueError, match="not on our dinner menu"):
            api.menu(con, "2026-10-25", items=["Pizza Hawaii"])


# ---------------------------------------------------------------- roles and the authorizer
@pytest.mark.parametrize("role, sql", [
    ("reader", "INSERT INTO market_summary (id) VALUES (1)"),
    ("scout", "DELETE FROM competitor_observation"),
    ("scout", "UPDATE competitor_observation SET price_cents = 1"),
    ("scout", "INSERT INTO price_change (id) VALUES (1)"),
    ("scout", "DROP TRIGGER competitor_observation_no_delete"),
    ("scout", "DROP TABLE competitor_observation"),
    ("scout", "ALTER TABLE competitor ADD COLUMN x"),
    ("scout", "CREATE TABLE evil (x)"),
    ("scout", "CREATE TEMP TABLE evil (x)"),
    ("scout", "PRAGMA writable_schema = ON"),
    ("scout", "PRAGMA ignore_check_constraints = ON"),
    ("scout", "PRAGMA foreign_keys = OFF"),
    ("scout", "PRAGMA recursive_triggers = OFF"),
    ("scout", "PRAGMA user_version = 99"),
    ("scout", "ATTACH DATABASE ':memory:' AS x"),
    ("scout", "VACUUM INTO 'copy.db'"),
    ("changes", "INSERT INTO competitor_observation (id) VALUES (99)"),
    ("changes", "DELETE FROM approval"),
    ("changes", "INSERT INTO owner (id) VALUES (2)"),
    ("build", "DELETE FROM competitor"),
    ("build", "INSERT INTO approval (id) VALUES (1)"),
])
def test_authorizer_refuses(db, role, sql):
    with connect(db, role) as con, pytest.raises(sqlite3.DatabaseError, match="not authorized|authorization denied"):
        con.execute(sql)                                                     # refused by the role, not a constraint


def test_reader_is_read_only_even_without_the_authorizer(db):
    with connect(db) as con:
        con.set_authorizer(None)
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            con.execute("DELETE FROM menu_price_base")


def test_unknown_role_and_missing_db(db, tmp_path):
    with pytest.raises(ValueError):
        with connect(db, "admin"):
            pass
    with pytest.raises(FileNotFoundError):
        with connect(tmp_path / "nope.db"):
            pass


# ---------------------------------------------------------------- red team F1: REPLACE deletes
@pytest.mark.parametrize("sql", [
    "INSERT OR REPLACE INTO market_summary SELECT * FROM market_summary",
    "REPLACE INTO competitor_observation SELECT * FROM competitor_observation WHERE id = 4",
])
def test_replace_cannot_delete_history(db, sql):
    with connect(db, "scout") as con, transaction(con):
        api.add_market_summary(con, report_id="M-1", our_item="Tiramisu", service="dinner", menu_as_of="2026-10-24",
                               our_price_cents=1350, competitor_median_cents=None, price_gap_cents=None, n_prices=0,
                               position="insufficient_data")
        api.add_observation(con, **OBS)                                      # id 4, a scout-written row
    with connect(db, "scout") as con, pytest.raises(sqlite3.DatabaseError, match="append-only|hash chain"):
        con.execute(sql)
    with connect(db) as con:
        assert con.execute("SELECT count(*) FROM competitor_observation").fetchone()[0] == 4
        assert con.execute("SELECT count(*) FROM market_summary").fetchone()[0] == 1


def test_replace_on_a_unique_hash_is_refused(db):
    with connect(db, "scout") as con, transaction(con):
        api.add_observation(con, **OBS)                                      # id 4, the victim
    with connect(db, "scout") as con:
        victim = dict(con.execute("SELECT * FROM competitor_observation WHERE id = 4").fetchone())
        last = con.execute("SELECT max(id), (SELECT row_hash FROM competitor_observation ORDER BY id DESC LIMIT 1) "
                           "FROM competitor_observation").fetchone()
        victim.update(id=last[0] + 1, prev_hash=last[1], price_cents=1)          # same row_hash as row 4
        cols = list(victim)
        with pytest.raises(sqlite3.DatabaseError, match="append-only"):
            con.execute(f"INSERT OR REPLACE INTO competitor_observation ({', '.join(cols)}) VALUES "
                        f"({', '.join('?' * len(cols))})", [victim[c] for c in cols])


# ---------------------------------------------------------------- role triggers (F9 and planting seed rows)
def test_build_role_writes_only_seed_rows(db):
    with connect(db, "build") as con, transaction(con), pytest.raises(sqlite3.IntegrityError, match="only the build"):
        api.add_competitor(con, name="X", menu_url="https://x.example/", found_via="apify_google_maps")


def test_scout_cannot_plant_seed_observations(db):
    with connect(db, "scout") as con, transaction(con), pytest.raises(sqlite3.IntegrityError, match="only the build"):
        api.add_observation(con, competitor_id=1, our_item="Tiramisu", their_item="Tiramisu", price_cents=500,
                            service="unknown", source_url="https://alpha.example/menu", source_type="restaurant_site",
                            quote="Tiramisu 5", match_confidence=0.9, reader="seed", retrieved_at="2026-10-24T22:00:00Z")


def test_outside_tools_cannot_insert_into_role_guarded_tables(db):
    con = sqlite3.connect(db, isolation_level=None)                          # no cp_role function at all
    with pytest.raises(sqlite3.OperationalError, match="cp_role"):
        con.execute("INSERT INTO competitor_observation (id) VALUES (99)")
    con.close()


# ---------------------------------------------------------------- constraints (F6, F8)
OBS = dict(competitor_id=1, our_item="Tiramisu", their_item="Tiramisu", price_cents=1100, service="unknown",
           source_url="https://alpha.example/menu", source_type="restaurant_site", quote="Tiramisu 11",
           match_confidence=0.8, reader="rule", retrieved_at="2026-10-24T22:00:00Z")


@pytest.mark.parametrize("values, err", [
    (dict(price_cents=0), sqlite3.IntegrityError),
    (dict(retrieved_at="2026-02-30T00:00:00Z"), sqlite3.IntegrityError),
    (dict(retrieved_at="2026-10-24 21:16:13"), sqlite3.IntegrityError),
    (dict(service="brunch"), sqlite3.IntegrityError),
    (dict(source_url="file:///etc/passwd"), sqlite3.IntegrityError),
    (dict(match_confidence=1.5), sqlite3.IntegrityError),
    (dict(quote="x" * 301), sqlite3.IntegrityError),
    (dict(competitor_id=999), sqlite3.IntegrityError),
    (dict(quote="Tiramisu 11\x00" + "X" * 5000), ValueError),               # NUL would hide everything after it
    (dict(their_item="Tira\ud800misu"), ValueError),                       # lone surrogate
    (dict(match_confidence=float("nan")), ValueError),
    (dict(match_confidence=float("inf")), ValueError),
    (dict(price_cents=True), ValueError),
    (dict(price_cents=11.5), ValueError),
])
def test_bad_observations_are_rejected(db, values, err):
    with connect(db, "scout") as con, pytest.raises(err):
        with transaction(con):
            api.add_observation(con, **{**OBS, **values})


def test_int_into_real_column_still_verifies(db):
    with connect(db, "scout") as con, transaction(con):
        api.add_competitor(con, name="Gamma", menu_url="https://gamma.example/", found_via="apify_google_maps",
                           distance_miles=1)
    assert problems(db, require_seal=False) == []


def test_nul_rejected_by_the_schema_too(db):
    con = raw(db)
    con.execute("DROP TRIGGER competitor_observation_role")                  # even without the role guard
    with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
        con.execute("""INSERT INTO competitor_observation (id, competitor_id, our_item, their_item, price_cents, service,
                       source_url, source_type, quote, match_confidence, reader, retrieved_at, created_at, prev_hash,
                       row_hash) SELECT 4, 1, 'Tiramisu', 'T', 100, 'unknown', 'https://a.example/', 'restaurant_site',
                       'ok' || char(0) || 'hidden', 0.5, 'rule', '2026-10-24T22:00:00Z', '2026-10-24T22:00:00Z',
                       (SELECT row_hash FROM competitor_observation WHERE id = 3), ?""", ("c" * 64,))
    con.close()


def test_append_requires_a_transaction_and_a_history_table(db):
    with connect(db, "scout") as con:
        with pytest.raises(RuntimeError, match="inside transaction"):
            api.add_check(con, 1, "verified")
        with transaction(con), pytest.raises(ValueError, match="not a history table"):
            append(con, "menu_price_base", {})


# ---------------------------------------------------------------- tamper evidence (F4, F5)
def test_chain_must_continue(db):
    with connect(db, "scout") as con, transaction(con), pytest.raises(sqlite3.IntegrityError, match="hash chain"):
        con.execute("""INSERT INTO observation_check (id, observation_id, status, checked_at, prev_hash, row_hash)
                       VALUES (1, 1, 'verified', '2026-10-24T22:00:00Z', ?, ?)""", ("f" * 64, "e" * 64))


def test_edit_with_outside_tools_is_detected_and_writers_refuse(db):
    con = raw(db)
    con.execute("DROP TRIGGER competitor_observation_no_update")
    con.execute("UPDATE competitor_observation SET price_cents = 500 WHERE id = 1")
    con.close()
    found = problems(db, require_seal=False)
    assert any("schema differs" in p for p in found)
    assert any("competitor_observation id 1: row edited" in p for p in found)
    with pytest.raises(TamperError):
        with connect(db, "scout"):
            pass


def test_trigger_body_swapped_under_the_same_name_is_detected(db):
    con = raw(db)
    for t in ("no_update", "no_delete", "chain"):
        con.execute(f"DROP TRIGGER competitor_observation_{t}")
        con.execute(f"CREATE TRIGGER competitor_observation_{t} BEFORE INSERT ON competitor_observation WHEN 0 "
                    "BEGIN SELECT 1; END")
    con.close()
    assert any("schema differs" in p for p in problems(db, require_seal=False))
    with pytest.raises(TamperError, match="schema differs"):
        with connect(db, "scout"):
            pass


def test_deleting_a_row_breaks_the_chain(db):
    con = raw(db)
    con.execute("DROP TRIGGER competitor_observation_no_delete")
    con.execute("DELETE FROM competitor_observation WHERE id = 2")
    con.close()
    found = problems(db, require_seal=False)
    assert any("id 3: chain broken" in p for p in found) and any("not consecutive" in p for p in found)


def test_seal_catches_a_truncated_chain(db, key):
    with connect(db, "scout") as con, transaction(con):
        api.add_check(con, 1, "rejected", reason="price not on page")
    seal(db, key)
    assert problems(db) == []
    con = raw(db)                                                            # cut off the inconvenient last row
    con.execute("DROP TRIGGER observation_check_no_delete")
    con.execute("DELETE FROM observation_check WHERE id = 1")
    con.execute("CREATE TRIGGER observation_check_no_delete BEFORE DELETE ON observation_check "
                "BEGIN SELECT RAISE(ABORT, 'observation_check is append-only'); END")
    con.close()
    assert any("sealed row 1 is missing" in p for p in problems(db))


def test_seal_catches_a_swapped_database(root, db, key, tmp_path):
    seal(db, key)
    sealed = seal_path(db).read_text(encoding="utf-8")
    for f in ("cost_price.db", "cost_price.db-wal", "cost_price.db-shm"):
        (root / "data" / f).unlink(missing_ok=True)
    build(root, seed=root / "seed.json")                                    # a fresh DB with the attacker's owner
    with connect(db, "owner") as con:
        init_owner(con, "attacker passphrase")
    seal_path(db).write_text(sealed, encoding="utf-8")
    assert any("owner key differs" in p for p in problems(db))


def test_edited_seal_is_detected(db, key):
    seal(db, key)
    doc = json.loads(seal_path(db).read_text(encoding="utf-8"))
    doc["heads"]["competitor"]["id"] = 1
    seal_path(db).write_text(json.dumps(doc), encoding="utf-8")
    assert any("seal signature is invalid" in p for p in problems(db))


def test_seal_with_injected_table_name_is_rejected(db, key):
    seal(db, key)
    doc = json.loads(seal_path(db).read_text(encoding="utf-8"))
    doc["heads"]["owner; DROP TABLE x"] = {"id": 1, "row_hash": "0" * 64}
    seal_path(db).write_text(json.dumps(doc), encoding="utf-8")
    assert any("different tables" in p for p in problems(db))


# ---------------------------------------------------------------- owner key and approvals (F2, F3)
def test_owner_key(db):
    with connect(db, "owner") as con:
        fp = init_owner(con, PASS)
        assert len(fp) == 24
        with pytest.raises(sqlite3.IntegrityError, match="already set up"):
            init_owner(con, "another passphrase")
        assert unlock(con, PASS).public_hex
        with pytest.raises(OwnerError, match="wrong passphrase"):
            unlock(con, "wrong passphrase!")
    with connect(db, "owner") as con:
        assert "sealed_key" not in repr(unlock(con, PASS))


def test_short_passphrase_refused(db):
    with connect(db, "owner") as con, pytest.raises(OwnerError, match="at least"):
        init_owner(con, "short")


def test_genuine_approval_and_change(db, key):
    aid = approve(db, key)
    with connect(db) as con:
        assert api.current_approval(con, "S1", SUBJECT) == (aid, None)
        assert "changed after" in api.current_approval(con, "S1", {**SUBJECT, "new_price_cents": 1500})[1]
        assert "no recorded" in api.current_approval(con, "S9", SUBJECT)[1]
    reprice(db, aid)
    assert price(db, on="2026-10-24") == 1350 and price(db) == 1400
    assert problems(db, require_seal=False) == []


def test_replayed_approval_does_not_revive_a_rejection(db, key):
    approve(db, key)
    approve(db, key, decision="rejected", decided_at="2026-10-24T23:00:00Z")
    with connect(db, "changes") as con, transaction(con):                    # copy the old approval to the end
        old = dict(con.execute("SELECT * FROM approval WHERE id = 1").fetchone())
        fields = {k: old[k] for k in ("suggestion_id", "subject_json", "subject_sha256", "decision", "decided_by",
                                      "decided_at", "note", "created_at")}
        append(con, "approval", fields, finalize=lambda row: {"signature_hex": "aa" * 32 + old["signature_hex"][64:]})
    with connect(db) as con:
        assert "not signed" in api.current_approval(con, "S1", SUBJECT)[1]
    assert any("approval id 3: not signed" in p for p in problems(db, require_seal=False))


def test_unsigned_approval_cannot_change_what_readers_see(db, key):
    class Forger:                                                            # a key that isn't the owner's
        public_hex = key.public_hex

        def sign(self, payload):
            return "ab" * 64

    aid = approve(db, Forger(), subject=subject(price=1))
    reprice(db, aid, new=1)                                                  # the DB-side guards let it through...
    assert price(db) == 1350                                                 # ...but no reader honours it
    assert any("C-S1-1" in p for p in problems(db, require_seal=False))


def test_change_must_match_what_was_signed(db, key):
    aid = approve(db, key)                                                   # owner approved 1400
    reprice(db, aid, new=99999)                                              # a writer logs something else
    assert price(db) == 1350
    assert any("doesn't match the planned change" in p for p in problems(db, require_seal=False))


def test_backdated_change_is_ignored(db, key):
    aid = approve(db, key, subject=subject(on="2026-10-01"))                 # even signed, it predates the approval
    reprice(db, aid, on="2026-10-01")
    assert price(db, on="2026-10-10") == 1350 and price(db, on="2026-10-25") == 1350


def test_price_change_needs_a_matching_log_row(db, key):
    aid = approve(db, key)
    with connect(db, "changes") as con, transaction(con):
        api.log_change(con, change_id="C-S1-b-1", suggestion_id="S1", approval_id=aid, system="nessie", ref="b-1",
                       field="payment_date", old_value="2026-10-31", new_value="2026-11-10")
    with connect(db, "changes") as con, pytest.raises(sqlite3.IntegrityError, match="no matching change_log"):
        with transaction(con):                                               # hang a price on a bill move
            api.add_price_change(con, change_id="C-S1-b-1", item="Tiramisu", effective_from="2026-10-25",
                                 price_cents=99999)
    with connect(db, "changes") as con, pytest.raises(sqlite3.IntegrityError, match="no matching change_log"):
        with transaction(con):
            api.add_availability_change(con, change_id="C-S1-b-1", item="Tiramisu", effective_from="2026-10-25",
                                        available=False)


def test_change_needs_the_latest_approved_decision(db, key):
    with connect(db, "changes") as con, pytest.raises(sqlite3.IntegrityError, match="latest approved|another suggestion"):
        with transaction(con):                                               # no approval exists at all
            api.log_change(con, change_id="C-S1-1", suggestion_id="S1", approval_id=1, system="menu_prices",
                           ref="Tiramisu", field="price_cents", old_value=1350, new_value=1400)
    aid = approve(db, key)
    approve(db, key, decision="rejected", decided_at="2026-10-24T23:00:00Z")
    with connect(db, "changes") as con, pytest.raises(sqlite3.IntegrityError, match="latest approved"):
        with transaction(con):                                               # citing the older approval
            api.log_change(con, change_id="C-S1-1", suggestion_id="S1", approval_id=aid, system="menu_prices",
                           ref="Tiramisu", field="price_cents", old_value=1350, new_value=1400)


def test_bank_move_started_before_a_rejection_can_still_be_logged(db, key):
    aid = approve(db, key, subject={"action": "stretch_supplier"})
    with connect(db, "changes") as con, transaction(con):
        api.add_intent(con, suggestion_id="S1", approval_id=aid, bill_id="b-1", from_date="2026-10-31",
                       to_date="2026-11-10")
    approve(db, key, subject={"action": "stretch_supplier"}, decision="rejected", decided_at="2026-10-24T23:00:00Z")
    with connect(db, "changes") as con, transaction(con):                    # the PUT already happened: log it
        api.log_change(con, change_id="C-S1-b-1", suggestion_id="S1", approval_id=aid, system="nessie", ref="b-1",
                       field="payment_date", old_value="2026-10-31", new_value="2026-11-10")
    with connect(db, "changes") as con, pytest.raises(sqlite3.IntegrityError, match="latest approved"):
        with transaction(con):                                               # but nothing new may start
            api.add_intent(con, suggestion_id="S1", approval_id=aid, bill_id="b-2", from_date="2026-11-07",
                           to_date="2026-11-17")


def test_valid_revert_restores_the_price(db, key):
    reprice(db, approve(db, key))
    rid = approve(db, key, sid="revert:C-S1-1", decided_at="2026-10-25T10:00:00Z",
                  subject={"revert": "C-S1-1", "planned_change": planned(price=1350, on="2026-10-26")})
    with connect(db, "changes") as con, transaction(con):
        api.log_change(con, change_id="C-revert:C-S1-1-1", suggestion_id="revert:C-S1-1", approval_id=rid,
                       system="menu_prices", ref="Tiramisu", field="price_cents", old_value=1400, new_value=1350,
                       reverts="C-S1-1")
        api.add_price_change(con, change_id="C-revert:C-S1-1-1", item="Tiramisu", effective_from="2026-10-26",
                             price_cents=1350)
    assert price(db, on="2026-10-25") == 1400 and price(db, on="2026-10-26") == 1350
    assert problems(db, require_seal=False) == []


# ---------------------------------------------------------------- two writers at once
def test_concurrent_writers_wait_instead_of_failing(db):
    script = textwrap.dedent(f"""
        import sys, time
        from cost_price import api
        from cost_price.db import connect, transaction
        with connect({str(db)!r}, "scout") as con:
            for i in range(15):
                with transaction(con):
                    api.add_check(con, 1, "verified", report_id="P" + sys.argv[1] + "-" + str(i))
                    time.sleep(0.01)
    """)
    procs = [subprocess.Popen([sys.executable, "-c", script, str(n)]) for n in range(3)]
    assert [p.wait(timeout=60) for p in procs] == [0, 0, 0]
    with connect(db) as con:
        assert con.execute("SELECT count(*) FROM observation_check").fetchone()[0] == 45
    assert problems(db, require_seal=False) == []


# ---------------------------------------------------------------- red team round 2
def test_one_approval_one_menu_change(db, key):
    aid = approve(db, key)
    reprice(db, aid)
    with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):            # a second change citing the same approval
        reprice(db, aid, cid="C-S1-replay", on="2026-11-01")
    assert problems(db, require_seal=False) == []


def test_replayed_change_written_by_outside_tools_is_ignored(db, key):
    aid = approve(db, key)
    reprice(db, aid)
    later = approve(db, key, sid="S2", subject={**subject(price=1500, on="2026-11-01"), "suggestion_id": "S2"})
    reprice(db, later, cid="C-S2-1", sid="S2", old=1400, new=1500, on="2026-11-01")     # owner later chose 1500
    con = raw(db)
    con.execute("DROP INDEX one_menu_change_per_approval")                   # outside tools: replay S1's approval
    con.close()
    _plant(db, "change_log", dict(change_id="C-S1-replay", suggestion_id="S1", approval_id=aid, at="2026-11-02T00:00:00Z",
                                  system="menu_prices", ref="Tiramisu", field="price_cents", old_value="1500",
                                  new_value="1400"))
    _plant(db, "price_change", dict(change_id="C-S1-replay", item="Tiramisu", channel="dine_in", service="dinner",
                                    effective_from="2026-10-25", price_cents=1400))
    found = problems(db, require_seal=False)
    assert any("schema differs" in p for p in found)                         # the dropped index is noticed
    assert any("C-S1-replay: this approval was already used" in p for p in found)
    con = sqlite3.connect(db)                                                 # and readers still show the owner's 1500
    con.create_function("cp_role", 0, lambda: "reader")
    con.row_factory = sqlite3.Row
    assert api.effective_price(con, "Tiramisu", "2026-11-05") == 1500
    con.close()


def test_change_on_another_channel_is_ignored(db, key):
    aid = approve(db, key, subject=subject(item="Margherita Pizza", price=1950))     # owner signed dine-in only
    reprice(db, aid, item="Margherita Pizza", old=2000, new=1950, channel="delivery")
    with connect(db) as con:
        assert api.effective_price(con, "Margherita Pizza", "2026-10-25", "delivery") == 2000    # unchanged
        assert api.effective_price(con, "Margherita Pizza", "2026-10-25", "dine_in") == 1750
    assert any("doesn't match the planned change" in p for p in problems(db, require_seal=False))


def _plant(db, table, values):
    """Append a correctly chained row with outside tools: drop the guards, insert, put them back verbatim."""
    from cost_price.db import _clean_value, _table_columns, row_hash
    from cost_price.schema import GENESIS
    con = raw(db)
    saved = [r for r in con.execute("SELECT name, sql FROM sqlite_master WHERE type='trigger' AND tbl_name = ?", (table,))]
    for name, _ in saved:
        con.execute(f"DROP TRIGGER {name}")
    last = con.execute(f"SELECT id, row_hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    kinds = dict(_table_columns(table))
    row = {c: values.get(c) for c in kinds if c not in ("id", "prev_hash", "row_hash")}
    row.update(id=(last[0] + 1) if last else 1, prev_hash=last[1] if last else GENESIS)
    row["row_hash"] = row_hash(table, row)
    con.execute(f"INSERT INTO {table} ({', '.join(row)}) VALUES ({', '.join('?' * len(row))})", list(row.values()))
    for _, sql in saved:
        con.execute(sql)
    con.close()


def test_price_row_hung_on_a_bill_move_is_ignored(db, key):
    aid = approve(db, key, subject={"action": "stretch_supplier"})
    with connect(db, "changes") as con, transaction(con):
        api.log_change(con, change_id="C-S1-b-1", suggestion_id="S1", approval_id=aid, system="nessie", ref="b-1",
                       field="payment_date", old_value="2026-10-31", new_value="2026-11-10")
    _plant(db, "price_change", dict(change_id="C-S1-b-1", item="Tiramisu", channel="dine_in", service="dinner",
                                    effective_from="2026-10-25", price_cents=1))
    _plant(db, "availability_change", dict(change_id="C-S1-b-1", item="Margherita Pizza", effective_from="2026-10-25",
                                           available=0))
    with connect(db) as con:                                                 # schema intact: triggers restored verbatim
        assert api.effective_price(con, "Tiramisu", "2026-10-25") == 1350
        assert "Margherita Pizza" in api.menu(con, "2026-10-25")[0]
    assert any("isn't a menu change" in p for p in problems(db, require_seal=False))


def test_rejected_bill_approval_is_no_price_lever(db, key):
    aid = approve(db, key, subject={"action": "stretch_supplier"}, decision="rejected")
    _plant(db, "change_log", dict(change_id="C-S1-x", suggestion_id="S1", approval_id=aid, at="2026-10-24T22:00:00Z",
                                  system="nessie", ref="b-1", field="payment_date", old_value="2026-10-31",
                                  new_value="2026-11-10"))
    found = problems(db, require_seal=False)
    assert any("C-S1-x: approval says rejected" in p for p in found)


def test_seal_catches_reference_layer_edits(db, key):
    seal(db, key)
    con = raw(db)
    con.execute("UPDATE menu_price_base SET price_cents = 500 WHERE item = 'Margherita Pizza' AND service = 'dinner'"
                " AND channel = 'dine_in'")
    con.close()
    assert any("reference:menu_price_base differs from the seal" in p for p in problems(db))


def test_seal_catches_planted_seed_rows(db, key):
    seal(db, key)
    _plant(db, "competitor_observation", dict(competitor_id=1, our_item="Margherita Pizza", their_item="Margherita",
                                              price_cents=4500, service="unknown", source_url="https://alpha.example/menu",
                                              source_type="restaurant_site", quote="Margherita 45", match_confidence=0.9,
                                              reader="seed", retrieved_at="2026-10-24T21:16:13Z",
                                              created_at="2026-10-24T21:16:13Z"))
    assert any("seed:competitor_observation differs from the seal" in p for p in problems(db))


def test_missing_seal_is_a_problem_once_an_owner_exists(db, key):
    seal(db, key)
    seal_path(db).unlink()
    assert any("no seal, but an owner exists" in p for p in problems(db))


def test_hidden_sqlite_named_trigger_is_detected(db):
    con = raw(db)
    con.execute("PRAGMA writable_schema = ON")
    con.execute("""INSERT INTO sqlite_master (type, name, tbl_name, rootpage, sql) VALUES ('trigger', 'sqlite_x',
                   'price_change', 0, 'CREATE TRIGGER sqlite_x BEFORE INSERT ON price_change BEGIN SELECT RAISE(IGNORE); END')""")
    con.close()
    assert any("schema differs" in p for p in problems(db, require_seal=False))


def test_negative_zero_is_not_a_false_alarm(db):
    with connect(db, "scout") as con, transaction(con):
        api.add_observation(con, **{**OBS, "match_confidence": -0.0})
    assert problems(db, require_seal=False) == []


@pytest.mark.parametrize("edit", [
    {"signature": None}, {"owner_public_key": 5}, {"heads": []}, {"digests": "x"}, {"sealed_at": 1},
])
def test_malformed_seal_is_reported_not_a_crash(db, key, edit):
    seal(db, key)
    doc = json.loads(seal_path(db).read_text(encoding="utf-8"))
    doc.update(edit)
    seal_path(db).write_text(json.dumps(doc), encoding="utf-8")
    assert problems(db)


# ---------------------------------------------------------------- red team round 3
def test_latest_market_ignores_scans_of_another_menu_date(db):
    def summary(report, on, price):
        api.add_market_summary(con, report_id=report, our_item="Tiramisu", service="dinner", menu_as_of=on,
                               our_price_cents=price, competitor_median_cents=1200, price_gap_cents=price - 1200,
                               n_prices=3, position="above_market")
    with connect(db, "scout") as con, transaction(con):
        summary("M-today", "2026-10-24", 1350)
        summary("M-old", "2025-12-01", 1300)                                  # a later scan of an old menu
    with connect(db) as con:
        rows = api.latest_market(con, on="2026-10-24")
        assert [(r["report_id"], r["our_price_cents"]) for r in rows] == [("M-today", 1350)]
        assert api.latest_market(con, on="2026-10-25") == []                 # nothing compared tomorrow's menu yet


def test_upcoming_changes_lists_only_valid_future_changes(db, key):
    reprice(db, approve(db, key))                                            # Tiramisu 1400 from 2026-10-25
    with connect(db) as con:
        assert [(u["item"], u["price_cents"], u["effective_from"]) for u in api.upcoming_changes(con, "2026-10-24")] == \
            [("Tiramisu", 1400, "2026-10-25")]
        assert api.upcoming_changes(con, "2026-10-25") == []                 # in force by then, no longer upcoming
