"""Incremental Bookkeeper support: bk_adapter, bk_state, bk_runner, the CLI commands and the tools that read the merged
state. Reports are fakes in the bk branch's schema-1.1 shape (bookkeeper/contracts.py: Report); the Bookkeeper itself
never runs (a fake subprocess runner writes the report it would have written). No ASI, no network."""
import asyncio
import json
import re
import subprocess
from datetime import date
from types import SimpleNamespace

import pytest

from orchestrator import bk_adapter as A
from orchestrator import bk_runner as R
from orchestrator import bk_state as S
from orchestrator import cli, tools

LATEST = "2026-10-22"                       # MAX(date) of bank_tx in conftest.BANK_TX


@pytest.fixture(autouse=True)
def no_subprocess(monkeypatch):
    """No test here may start a real process (the Bookkeeper, uv): only the fake runners below."""
    def boom(*a, **kw):
        raise AssertionError("a real subprocess was started")
    monkeypatch.setattr(R.subprocess, "run", boom)


def finding(fid, cause, counterparty, treatment, impact, bank_ids=(), books_refs=(), **extra):
    return {"id": fid, "root_cause": cause, "verdict": "error", "confidence": 0.9, "counterparty": counterparty,
            "dates": {"first": "2026-08-01", "last": "2026-10-20"}, "bank_ids": list(bank_ids),
            "books_refs": list(books_refs), "amounts": {"impact_cents": impact},
            "explanation": f"{counterparty}: {cause.replace('_', ' ')}.",
            "evidence": [{"type": "bank_row", "ref": b} for b in bank_ids] or [{"type": "invoice", "ref": books_refs[0]}],
            "treatment": treatment, "recoverable": True, "suggested_action": "ask the owner",
            "assumption_if_unconfirmed": None, **extra}


FEE = lambda fid, **kw: finding(fid, "card_fee_creep", "TableTap", {"kind": "ongoing", "since": "2026-08-01",  # noqa: E731
                                                                      "monthly_cents": 30000}, 34900, ["n-1"], **kw)
OWED = lambda fid: finding(fid, "missed_payment", "Mission Dry Goods", {"kind": "owed", "cents": 48000,  # noqa: E731
                                                                        "due": "2026-10-03"}, 48000, (), ["MDG-CHS-1"])
OWED2 = lambda fid: finding(fid, "missed_payment", "Sonoma Dairy Supply", {"kind": "owed", "cents": 9900,  # noqa: E731
                                                                           "due": "2026-10-10"}, 9900, (), ["SDS-1"])
DUP = lambda fid: finding(fid, "duplicate_charge", "TableTap", {"kind": "exclude", "bank_ids": ["n-2"]},  # noqa: E731
                          34900, ["n-2"])
NEW = lambda fid: finding(fid, "supplier_overcharge", " Bay Produce Co ", {"kind": "ongoing", "since": "2026-09-01",  # noqa: E731
                                                                          "monthly_cents": 5000}, 10000, ["n-3"])


def report(report_id, as_of, findings, start="2025-10-27", **extra):
    return {"report_id": report_id, "request_id": f"req-{report_id}", "schema_version": "1.1",
            "generated_at": "2026-10-24T12:00:00+00:00", "as_of": as_of, "window": {"start": start, "end": as_of},
            "sources": {"nessie_tag": "boh-1loc", "bank_snapshot": "data/x.db", "bank_rows": 3, "books_db": "data/books.db"},
            "bookkeeper": {"version": "0.1.0", "model": "asi1", "trace_id": "TR-" + "0" * 32},
            "verification": {"passed": False, "checked_at": None, "findings_checked": 0, "findings_rejected": 0,
                             "notes": []},
            "balances": {"as_of": as_of, "accounts": [], "total_cents": 0},
            "coverage": {"bank_rows_reviewed": 3, "matched_to_documents": 1, "investigated": 2, "cleared_as_ok": 0},
            "summary": {"errors": len(findings), "needs_owner": 0, "total_impact_cents": 0, "recoverable_cents": 0,
                        "ongoing_monthly_cents": 0},
            "findings": findings, "cleared": [], "limitations": [], **extra}


BASE = report("R-base", "2026-10-20", [FEE("F1"), OWED("F2"), DUP("F3"), OWED2("F4")])
RERUN = report("R-rerun", "2026-10-21", [OWED("F1"), FEE("F2"), DUP("F3"), NEW("F4")])      # renumbered, OWED2 gone
DELTA = report("R-delta", LATEST, [FEE("F1", status="resolved")], mode="delta",
               coverage={"bank_rows_reviewed": 1, "matched_to_documents": 0, "investigated": 1, "cleared_as_ok": 0,
                         "window": {"start": LATEST, "end": LATEST}})


def put(root, rep, name=None):
    path = root / "data" / "reports" / (name or f"{rep['report_id']}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(rep), encoding="utf-8")
    return path


def by_cause(state):
    return {e["finding"]["root_cause"] + ":" + e["finding"]["counterparty"].strip(): e for e in state["findings"].values()}


def uses(session):
    r = tools.call(session, "get_findings", {})
    assert r["available"], r
    return r, sorted((a["kind"], a.get("amount_cents") or a.get("monthly_cents")) for a in r["forecast_uses"])


# ---------------------------------------------------------------- ingest: full, full again, delta
def test_baseline_full_ingest(session):
    out = S.ingest(session.root, put(session.root, BASE))
    state = S.load(session.root)
    assert out == {"noop": False, "report_id": "R-base", "mode": "full", "new": 4, "updated": 0, "closed": 0,
                   "not_rereported": 0, "covered_through": "2026-10-20", "verified": False, "rounds": None,
                   "verification": "not verified"}
    assert state["baseline"] == {"report_id": "R-base", "filename": "R-base.json", "as_of": "2026-10-20",
                                 "window": {"start": "2025-10-27", "end": "2026-10-20"}}
    assert S.counts(state)["open"] == 4 and state["reports"][0]["mode"] == "full"
    r, used = uses(session)
    assert r["source"] == "merged Bookkeeper state" and r["coverage"]["days_behind"] == 2
    assert used == [("ongoing_overcharge", 30000), ("unpaid_bill", 9900), ("unpaid_bill", 48000)]


def test_second_full_report_matches_by_key_adds_new_and_marks_missing(session):
    S.ingest(session.root, put(session.root, BASE))
    out = S.ingest(session.root, put(session.root, RERUN))
    state = S.load(session.root)
    assert (out["new"], out["updated"], out["not_rereported"]) == (1, 3, 0 + 1)
    f = by_cause(state)
    assert len(state["findings"]) == 5                                     # renumbered F1/F2 matched, not duplicated
    owed = f["missed_payment:Mission Dry Goods"]
    assert (owed["first_seen"], owed["last_seen"], owed["finding"]["id"]) == ("2026-10-20", "2026-10-21", "F1")
    gone = f["missed_payment:Sonoma Dairy Supply"]
    assert gone["status"] == "not_rereported" and gone["rereported"] is False and "R-rerun" in gone["note"]
    assert f["supplier_overcharge:Bay Produce Co"]["status"] == "open"
    assert state["baseline"]["report_id"] == "R-base" and state["covered_through"] == "2026-10-21"
    _, used = uses(session)
    assert ("unpaid_bill", 9900) not in used                               # not re-reported: never in the forecast
    assert used == [("ongoing_overcharge", 5000), ("ongoing_overcharge", 30000), ("unpaid_bill", 48000)]


def test_secondary_match_on_bank_id_overlap(session):
    S.ingest(session.root, put(session.root, BASE))
    moved = DUP("F9")
    moved["counterparty"] = "TABLETAP INC"                                 # key changes; same cause, same bank row
    S.ingest(session.root, put(session.root, report("R-moved", "2026-10-21", [FEE("F1"), OWED("F2"), moved, OWED2("F3")])))
    state = S.load(session.root)
    assert len(state["findings"]) == 4 and S.counts(state)["open"] == 4


def test_delta_with_resolved_status_closes_and_leaves_the_forecast(session):
    S.ingest(session.root, put(session.root, BASE))
    S.ingest(session.root, put(session.root, RERUN))
    out = S.ingest(session.root, put(session.root, DELTA))
    state = S.load(session.root)
    assert (out["mode"], out["closed"], out["covered_through"]) == ("delta", 1, LATEST)
    fee = by_cause(state)["card_fee_creep:TableTap"]
    assert fee["status"] == "resolved"
    assert state["reports"][-1]["capabilities"] == {"delta": True, "status": True, "finding_key": False}
    r, used = uses(session)
    assert ("ongoing_overcharge", 30000) not in used and r["coverage"]["days_behind"] == 0
    assert used == [("ongoing_overcharge", 5000), ("unpaid_bill", 48000)]
    base = tools.call(session, "forecast_base", {})                       # the forecast takes the merged open set
    assert sorted(a["kind"] for a in base["adjustments_used"]) == ["ongoing_overcharge", "unpaid_bill"]
    assert "bookkeeper_state.json" in base["adjustments_note"]


def test_delta_needs_a_baseline_and_no_gap(session):
    with pytest.raises(S.StateError, match="baseline"):
        S.ingest(session.root, put(session.root, DELTA))
    S.ingest(session.root, put(session.root, report("R-old", "2026-10-10", [FEE("F1")])))
    with pytest.raises(S.StateError, match="missing"):
        S.ingest(session.root, put(session.root, DELTA))
    with pytest.raises(S.StateError, match="older"):
        S.ingest(session.root, put(session.root, report("R-older", "2026-10-01", [])))


def test_ids_are_namespaced_so_passed_adjustments_never_collide(session):
    import contracts as K

    S.ingest(session.root, put(session.root, BASE))
    S.ingest(session.root, put(session.root, RERUN))                      # both reports used F1..F4
    state = S.load(session.root)
    merged = S.open_findings_report(state)
    keys = [k for k, _ in S.open_entries(state)]
    assert [f["id"] for f in merged["findings"]] == [k[:10] for k in keys]
    assert merged["summary"]["ongoing_monthly_cents"] == 35000 and merged["summary"]["errors"] == 4
    findings, adjustments, rejected = K.from_bk_report(merged, {"n-1": "T0000000A1", "n-2": "T0000000A2",
                                                                "n-3": "T0000000A3"})
    assert not rejected
    passed = K.passed_adjustments(adjustments, [K.CheckResult(subject="bookkeeper", ref=f.finding_id, status="pass")
                                                for f in findings])
    assert len({a.finding_id for a in passed}) == len(passed) == 4
    assert S.short_ids(["abcdefghij1", "abcdefghij2"]) == {"abcdefghij1": "abcdefghij1", "abcdefghij2": "abcdefghij2"}


def test_reingest_is_a_noop(session):
    path = put(session.root, BASE)
    S.ingest(session.root, path)
    before = S.state_path(session.root).read_bytes()
    assert S.ingest(session.root, path)["noop"] is True
    assert S.ingest(session.root, "data/reports/R-base.json")["noop"] is True
    assert S.state_path(session.root).read_bytes() == before
    path.write_text(json.dumps(report("R-base", "2026-10-20", [])), encoding="utf-8")     # same name, new content
    with pytest.raises(S.StateError, match="immutable"):
        S.ingest(session.root, path)


# ---------------------------------------------------------------- the state file
def test_writes_are_atomic(session, monkeypatch):
    S.ingest(session.root, put(session.root, BASE))
    before = S.state_path(session.root).read_bytes()
    real = S.os.replace

    def crash(src, dst):
        if str(dst).endswith(S.STATE_NAME):
            raise OSError("power cut")
        return real(src, dst)

    monkeypatch.setattr(S.os, "replace", crash)
    with pytest.raises(OSError):
        S.ingest(session.root, put(session.root, RERUN))
    assert S.state_path(session.root).read_bytes() == before                # the old state, whole
    assert not list(S.folder(session.root).glob("*.tmp")) and not (S.folder(session.root) / "bookkeeper_state.lock").exists()


def test_corrupt_state_is_recovered_from_backup_or_refused(session):
    S.ingest(session.root, put(session.root, BASE))
    S.ingest(session.root, put(session.root, RERUN))                      # the .bak holds the state after BASE
    path = S.state_path(session.root)
    path.write_text("{not json", encoding="utf-8")
    assert tools.call(session, "bookkeeper_status", {})["available"] is False     # read-only: refuses, never repairs
    state = S.load(session.root)
    assert state["covered_through"] == "2026-10-20" and "recovered" in state["notes"][0]
    assert list(S.folder(session.root).glob("bookkeeper_state.json.corrupt-*"))
    path.write_text("[]", encoding="utf-8")
    path.with_name(path.name + ".bak").unlink()
    with pytest.raises(S.StateError, match="corrupt.*bookkeeper-setup --force"):
        S.load(session.root)
    r = tools.call(session, "get_findings", {})
    assert r["source"] == "latest Bookkeeper report" and "corrupt" in r["state_error"]   # today's behaviour, flagged


def test_lock_is_exclusive(session):
    with S.lock(session.root):
        with pytest.raises(S.StateError, match="held by another run"):
            with S.lock(session.root, wait=0.1):
                pass


@pytest.mark.parametrize("name", ["../R-1.json", "data/reports/../books.db", "data/reports/../R-1.json",
                                  "data/reports/X-1.json", "R-1.json.txt", "..\\R-1.json", "data/R-1.json",
                                  "data/reports/sub/R-1.json", "R-a b.json"])
def test_filename_validation_refuses_traversal_and_other_names(session, name):
    put(session.root, BASE, "R-1.json")
    (session.root / "data" / "R-1.json").write_text(json.dumps(BASE), encoding="utf-8")
    with pytest.raises(S.StateError):
        S.ingest(session.root, name)
    with pytest.raises(S.StateError):
        S.ingest(session.root, session.root.parent / "R-1.json")             # absolute, outside data/reports
    assert not S.state_path(session.root).exists()


def test_schema_validation(session):
    bad = report("R-bad", "2026-10-20", [{"id": "F1", "root_cause": "x", "treatment": {"kind": "teleport"}}])
    with pytest.raises(S.StateError, match="treatment.kind"):
        S.ingest(session.root, put(session.root, bad))
    with pytest.raises(S.StateError, match="schema_version"):
        S.ingest(session.root, put(session.root, {**BASE, "report_id": "R-v2", "schema_version": "2.0"}))


# ---------------------------------------------------------------- adapter
def test_adapter_keys_capabilities_and_requests(monkeypatch):
    assert A.finding_key(FEE("F1")) == A.finding_key(FEE("F7")) != A.finding_key(OWED("F1"))
    assert A.finding_key({**FEE("F1"), "finding_key": "bk-42"}) == "bk-42"
    assert A.finding_key(NEW("F1")) == A.finding_key({**NEW("F2"), "counterparty": "bay produce co"})
    assert A.capabilities(BASE) == {"delta": False, "status": False, "finding_key": False}
    assert A.capabilities({**BASE, "window": {"start": "a", "end": "b", "since": "c"}})["delta"]
    assert A.capabilities(BASE, {"since": "2026-10-21"})["delta"]
    assert A.status(FEE("F1")) == "open" and A.status(FEE("F1", status="superseded")) == "superseded"
    monkeypatch.delenv(A.INCREMENTAL_ENV, raising=False)
    plain = A.build_request(date(2026, 10, 22), date(2026, 10, 21), "R-x", request_id="r1")
    assert plain == {"request_id": "r1", "nessie_tag": "boh-1loc", "as_of": "2026-10-22"}   # ReportRequest's fields only
    monkeypatch.setenv(A.INCREMENTAL_ENV, "1")
    inc = A.build_request(date(2026, 10, 22), date(2026, 10, 21), "R-x", request_id="r1")
    assert inc["since"] == "2026-10-21" and inc["prior_report_id"] == "R-x"


# ---------------------------------------------------------------- runner (the subprocess is faked)
class FakeRunner:
    """subprocess.run stand-in: records argv and writes the report the Bookkeeper would have written."""

    def __init__(self, root, make=None, returncode=0, stderr="", raise_timeout=False):
        self.root, self.make, self.returncode, self.stderr, self.raise_timeout = root, make, returncode, stderr, raise_timeout
        self.calls = []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if self.raise_timeout:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        if self.returncode:
            return subprocess.CompletedProcess(argv, self.returncode, "", self.stderr)
        rid = argv[argv.index("--request-id") + 1]
        name = R.expected_report(rid)
        as_of = argv[argv.index("--as-of") + 1]
        rep = (self.make or (lambda a, args: report(name[:-5], a, [FEE("F1"), OWED("F2")])))(as_of, argv)
        rep["report_id"] = name[:-5]
        put(self.root, rep, name)
        out = {"request_id": rid, "report_id": name[:-5], "filename": f"data\\reports\\{name}", "as_of": as_of,
               "verified": False}
        return subprocess.CompletedProcess(argv, 0, "INFO noise\n" + json.dumps(out, indent=2), "")


@pytest.fixture
def bk(tmp_path, monkeypatch):
    project = tmp_path / "bookkeeper_project"
    project.mkdir()
    (project / "pyproject.toml").write_text("[project]\nname='restaurant-bookkeeper'\n", encoding="utf-8")
    monkeypatch.setattr(R, "bookkeeper_project", lambda: project)
    monkeypatch.setenv("ORCHESTRATOR_BK_UV", "uv")
    monkeypatch.setenv("ASI1_API_KEY", "test-key-not-real")
    monkeypatch.delenv(A.INCREMENTAL_ENV, raising=False)
    monkeypatch.delenv(A.AUTO_ENV, raising=False)
    return project


def test_runner_builds_the_cli_and_clamps_as_of(session, bk):
    fake = FakeRunner(session.root)
    path = R.run_bookkeeper(session.root, as_of=date(2026, 12, 1), runner=fake)
    argv, kw = fake.calls[0]
    assert argv[:6] == ["uv", "run", "--project", str(bk), "bookkeeper", "--root"]
    i = argv.index("run", 4)
    rid = argv[i + 2]
    assert argv[i:] == ["run", "--request-id", rid, "--as-of", LATEST, "--tag", "boh-1loc"]       # clamped
    assert re.fullmatch(r"orch-bk-2026-10-22-[0-9a-f]{8}", rid) and "--since" not in argv
    assert path.name == R.expected_report(rid) and kw["timeout"] == R.UPDATE_TIMEOUT
    assert "VIRTUAL_ENV" not in kw["env"] and kw["cwd"].endswith("nessie_lab")


def test_runner_sends_since_and_prior_only_with_incremental(session, bk, monkeypatch):
    fake = FakeRunner(session.root)
    R.run_bookkeeper(session.root, since=date(2026, 10, 21), prior="R-base", runner=fake)
    assert "--since" not in fake.calls[-1][0] and "--prior-report-id" not in fake.calls[-1][0]
    monkeypatch.setenv(A.INCREMENTAL_ENV, "1")
    R.run_bookkeeper(session.root, since=date(2026, 10, 21), prior="R-base", runner=fake)
    argv = fake.calls[-1][0]
    assert argv[-4:] == ["--since", "2026-10-21", "--prior-report-id", "R-base"]


def test_runner_errors_are_clear(session, bk, monkeypatch, tmp_path):
    fake = FakeRunner(session.root)
    monkeypatch.setattr(R, "bookkeeper_project", lambda: tmp_path / "missing")
    with pytest.raises(R.BookkeeperError, match="nessie_lab/bookkeeper.*bookkeeper-ingest"):
        R.run_bookkeeper(session.root, runner=fake)
    monkeypatch.setattr(R, "bookkeeper_project", lambda: bk)
    monkeypatch.delenv("ASI1_API_KEY")
    with pytest.raises(R.BookkeeperError, match="ASI1_API_KEY"):
        R.run_bookkeeper(session.root, runner=fake)
    monkeypatch.setenv("ASI1_API_KEY", "test-key-not-real")
    with pytest.raises(R.BookkeeperError, match=r"within 5 s.*R-[0-9a-f]{64}\.json"):
        R.run_bookkeeper(session.root, timeout=5, runner=FakeRunner(session.root, raise_timeout=True))
    with pytest.raises(R.BookkeeperError, match="Invalid as_of"):
        R.run_bookkeeper(session.root, runner=FakeRunner(session.root, returncode=1,
                                                         stderr="Bookkeeper failed (ValueError): Invalid as_of\n"))
    assert fake.calls == []


def test_transport_failure_is_resumed_by_trace(session, bk):
    calls = []
    good = FakeRunner(session.root)

    def runner(argv, **kw):
        calls.append(argv)
        if len(calls) == 1:
            return subprocess.CompletedProcess(argv, 1, "", "ERROR Request x failed: The ASI API call timed out. "
                                               f"Retry the request. (trace TR-{'a' * 32})\nBookkeeper failed "
                                               "(ReadTimeout): The ASI API call timed out. Retry the request.\n")
        assert argv[-3:] == ["resume", "--trace-id", "TR-" + "a" * 32]
        fixed = list(calls[0])
        return good(fixed, **kw)

    path = R.run_bookkeeper(session.root, runner=runner)
    assert len(calls) == 2 and path.is_file()


# ---------------------------------------------------------------- setup / update / ingest / status
def test_setup_then_update_says_up_to_date(session, bk, capsys, monkeypatch):
    fake = FakeRunner(session.root)
    monkeypatch.setattr(R.subprocess, "run", fake)
    assert cli.main(["--root", str(session.root), "bookkeeper-setup", "--timeout", "60"]) == 0
    out = capsys.readouterr().out
    assert "one full run over the whole bank window" in out and "Backfill done in" in out
    assert fake.calls[0][1]["timeout"] == 60
    assert cli.main(["--root", str(session.root), "bookkeeper-setup"]) == 0
    assert "baseline exists" in capsys.readouterr().out and len(fake.calls) == 1
    assert cli.main(["--root", str(session.root), "bookkeeper-update"]) == 0
    assert "up to date: covers through 2026-10-22" in capsys.readouterr().out and len(fake.calls) == 1


def test_update_full_rerun_without_incremental_and_delta_with_it(session, bk, monkeypatch):
    S.ingest(session.root, put(session.root, BASE))
    fake = FakeRunner(session.root)
    out = R.update(session.root, runner=fake, progress=lambda *_: None)
    assert out["status"] == "updated" and out["mode"] == "full" and "--since" not in fake.calls[-1][0]
    assert S.load(session.root)["covered_through"] == LATEST

    S.state_path(session.root).unlink()
    S.ingest(session.root, put(session.root, report("R-b2", "2026-10-20", [FEE("F1")])))
    monkeypatch.setenv(A.INCREMENTAL_ENV, "1")
    delta = lambda as_of, argv: report("x", as_of, [OWED("F1")], mode="delta",  # noqa: E731
                                       window={"start": "2025-10-27", "end": as_of, "since": "2026-10-21"})
    out = R.update(session.root, runner=FakeRunner(session.root, delta), progress=lambda *_: None)
    assert out["mode"] == "delta" and out["new"] == 1
    st = S.load(session.root)
    assert S.counts(st)["open"] == 2 and st["covered_through"] == LATEST     # the delta kept the earlier finding


def test_cli_ingest_and_status(session, capsys):
    put(session.root, BASE)
    assert cli.main(["--root", str(session.root), "bookkeeper-ingest", "data/reports/R-base.json"]) == 0
    assert "Ingested R-base (full): 4 new" in capsys.readouterr().out
    assert cli.main(["--root", str(session.root), "bookkeeper-ingest", "data/reports/../books.db"]) == 1
    assert "only Bookkeeper reports" in capsys.readouterr().out
    assert cli.main(["--root", str(session.root), "bookkeeper-status"]) == 0
    out = capsys.readouterr().out
    assert "Covered through: 2026-10-20" in out and "2 day(s) behind" in out and "4 open" in out
    assert "bookkeeper-update" in out


# ---------------------------------------------------------------- the LLM side
def test_llm_tools_have_no_setup_or_update():
    names = [t["function"]["name"] for t in tools.TOOLS]
    assert "bookkeeper_status" in names and set(names) == set(tools.HANDLERS)
    assert not [n for n in names if re.search(r"setup|update|ingest|backfill|run_", n)]
    assert tools.SPECS["bookkeeper_status"]["parameters"]["properties"] == {}


def test_bookkeeper_status_tool_is_read_only(session):
    r = tools.call(session, "bookkeeper_status", {})
    assert r["available"] is False and r["latest_bank_date"] == LATEST
    S.ingest(session.root, put(session.root, BASE))
    before = S.state_path(session.root).read_bytes()
    r = tools.call(session, "bookkeeper_status", {})
    assert (r["covered_through"], r["days_behind"], r["findings"]["open"]) == ("2026-10-20", 2, 4)
    assert S.state_path(session.root).read_bytes() == before


def test_get_findings_without_state_keeps_the_latest_report_behaviour(session):
    put(session.root, BASE)
    r = tools.call(session, "get_findings", {})
    assert r["source"] == "latest Bookkeeper report" and r["report"] == "R-base.json"
    assert [f["finding_id"] for f in r["findings"]] == ["F1", "F2", "F3", "F4"]


def test_brief_shows_coverage(session):
    S.ingest(session.root, put(session.root, BASE))
    from orchestrator.brief import brief
    text = brief(session, propose=False)["text"]
    assert "Coverage: Bookkeeper covers through 2026-10-20, 2 day(s) behind the bank" in text


async def test_pre_turn_notes_when_behind_and_never_runs_a_full_rerun(session, bk, monkeypatch):
    S.ingest(session.root, put(session.root, BASE))
    fake = FakeRunner(session.root)
    stop = SimpleNamespace(remaining=lambda: 60.0, expired=lambda: False)
    notes = await R.pre_turn(session, stop, runner=fake)
    assert notes == ["Bookkeeper covers through 2026-10-20; run `orchestrator bookkeeper-update`"]
    monkeypatch.setenv(A.AUTO_ENV, "1")
    monkeypatch.setenv(A.INCREMENTAL_ENV, "1")
    notes = await R.pre_turn(session, stop, runner=fake)                   # no delta capability seen: never full
    assert "never runs automatically" in notes[1] and fake.calls == []
    monkeypatch.delenv(A.INCREMENTAL_ENV)
    assert R.update(session.root, runner=fake, allow_full=False)["status"] == "skipped" and fake.calls == []


async def test_auto_update_runs_a_delta_within_the_deadline(session, bk, monkeypatch):
    S.ingest(session.root, put(session.root, report("R-b", "2026-10-20", [FEE("F1")])))
    S.ingest(session.root, put(session.root, report("R-d", "2026-10-21", [], mode="delta",
                                                     window={"start": "2025-10-27", "end": "2026-10-21",
                                                             "since": "2026-10-21"})))
    monkeypatch.setenv(A.AUTO_ENV, "1")
    monkeypatch.setenv(A.INCREMENTAL_ENV, "1")
    delta = lambda as_of, argv: report("x", as_of, [], mode="delta",  # noqa: E731
                                       window={"start": "2025-10-27", "end": as_of, "since": "2026-10-22"})
    fake = FakeRunner(session.root, delta)
    short = SimpleNamespace(remaining=lambda: 10.0, expired=lambda: False)
    assert "not enough time" in (await R.pre_turn(session, short, runner=fake))[1] and fake.calls == []
    stop = SimpleNamespace(remaining=lambda: 60.0, expired=lambda: False)
    notes = await R.pre_turn(session, stop, runner=fake)
    argv, kw = fake.calls[0]
    assert argv[argv.index("--since") + 1] == "2026-10-22" and kw["timeout"] == 60 - R.AUTO_RESERVE_SECONDS
    assert "auto-update" in notes[0] and S.load(session.root)["covered_through"] == LATEST


async def test_turn_carries_the_behind_note(session, no_llm_key):
    from orchestrator import turn as T

    S.ingest(session.root, put(session.root, BASE))
    out = await T.turn(session, "how are we doing", deadline=60)
    assert out["notes"][0] == "Bookkeeper covers through 2026-10-20; run `orchestrator bookkeeper-update`"
    assert out["text"].endswith(T.FOOTER)


async def test_agent_mode_reads_the_state_instead_of_asking_for_a_full_run(session):
    from unittest.mock import AsyncMock

    from orchestrator.agent import PeerDispatch

    S.ingest(session.root, put(session.root, BASE))
    ctx = SimpleNamespace(send_and_receive=AsyncMock())
    d = PeerDispatch(ctx, bookkeeper="agent1qbk")
    r = await d(session, "get_findings", {})
    assert ctx.send_and_receive.await_count == 0 and r["source"] == "merged Bookkeeper state"
    assert "merged state" in d.notes[0] and not d.degraded
