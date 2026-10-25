"""CH: the only code that writes. Applies one owner-approved Suggestion and reports an ExecutionResult.

Every record lives in data/cost_price.db (the shared cost_price package), written only through
connect(db, "changes"), transaction(con) and cost_price.api:

  reprice_item      price_change (dine-in dinner price from the next day)                    cost_price.db
  remove_item       availability_change available=0 (off the menu from the next day)          cost_price.db
  stretch_supplier  payment_date of that supplier's pending bills, + N days                  Nessie (PUT /bills/{id})
  pay_bill          payment_date of one pending bill -> today                                Nessie
  dispute_charge    dispute (Nessie cannot reverse a purchase)                                cost_price.db
plus approval, nessie_intent, change_log and applied. Never: Nessie balances or purchase status, fake deposits,
books.db (regenerated). Default is a dry run that writes nothing anywhere and returns the planned changes.

Approvals are Ed25519-signed by the owner key (cost_price.owner.unlock, needs the passphrase) over the exact subject
shown: the suggestion, plus for menu changes the planned_change row (with its effective_from) and for bill moves the
planned bills with their from and to dates. A live apply needs no key: api.current_approval checks the signature with
the stored public key, and any reason is a refusal. It is re-checked (with the approval's age) before every external
call and every write, so a rejection mid-run stops what hasn't started."""
import base64
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from cost_price import api
from cost_price.db import connect, transaction
from cost_price.owner import canonical

from .shared import AuditEntry, Approval, Change, ExecutionResult, Suggestion

MAX_PRICE_MOVE = 0.30               # a reprice beyond ±30% is a typo or a bad suggestion, not a market adjustment
MAX_STRETCH_DAYS = 30               # beyond this a supplier stops delivering
MAX_APPROVAL_AGE = timedelta(hours=24)      # an older approval was given for a bank/menu that has since moved on
MAX_CLOCK_SKEW = timedelta(minutes=5)       # an approval dated further in the future is refused
LOCK_STALE = timedelta(minutes=10)          # a per-suggestion lock older than this was left by a crashed run
LOCK_EMPTY_GRACE = 5.0                      # seconds: an empty lock may be one whose holder is writing its pid
LOCK_RE = re.compile(r"^pid (\d+) at ")
RESTAURANT_TZ = ZoneInfo("America/Los_Angeles")
DRY_RUN = "dry run: nothing written; apply with live=True"
SUGGESTION_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")      # no ':': 'revert:<change_id>' is CH's own namespace
CHANGE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,93}$")          # 'revert:' + 93 = the 100-char suggestion_id limit
BANK_ID_RE = re.compile(r"^T[0-9A-F]{9}$")
ALL_COURSES = ("starter", "main", "dessert", "drink", "bottle", "modifier", "event")
BILL_ACTIONS = ("stretch_supplier", "pay_bill")
MENU_ACTIONS = ("reprice_item", "remove_item")
DOLLARS_RE = re.compile(r"\$\d+(\.\d{2})?")        # a dollar figure in the AI's free-text title
FIELD_ACTIONS = {"new_price_cents": "reprice_item", "extra_days": "stretch_supplier"}   # the one action each fits
OBSERVED = "-observed"              # change_id suffix: bill found at its target, but CH has no successful PUT of its own
CHANNEL, SERVICE = "dine_in", "dinner"

# Test-only: honour Executor(now=..., today=...) in live runs and approvals. The test suite's conftest sets it; in
# production a live run or an approval with an injected clock is refused, so a caller can't fake an approval's age.
ALLOW_INJECTED_CLOCK = False


class Refused(ValueError):
    """The suggestion can't be applied as written. Nothing was changed."""


def valid_id(value: str, what="suggestion_id") -> str:
    pattern = CHANGE_ID_RE if what == "change_id" else SUGGESTION_ID_RE
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise Refused(f"{what} {value!r} must match {pattern.pattern}")
    return value


def subject_id(subject) -> str:
    return subject.suggestion_id if hasattr(subject, "suggestion_id") else f"revert:{subject['revert']}"


def utc_ts(dt: datetime) -> str:
    """The schema's timestamp form, YYYY-MM-DDTHH:MM:SSZ."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def bank_day(value) -> str | None:
    """A payment_date as Nessie (or the sync DB) returned it, as YYYY-MM-DD; None if it isn't a date at all."""
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except (TypeError, ValueError):
        return None


def planned_change(op) -> dict:
    """The planned_change the owner signs for one menu op, in exactly api.planned_change's shape."""
    if op["kind"] == "price":
        return api.planned_change("price_change", dict(item=op["item"], channel=CHANNEL, service=SERVICE,
                                                       effective_from=op["on"], price_cents=op["price"]))
    return api.planned_change("availability_change", dict(item=op["item"], effective_from=op["on"],
                                                          available=op["available"]))


class Executor:
    def __init__(self, root: Path, key=None, nessie=None, tag="boh-1loc", today: date | None = None,
                 now: datetime | None = None):
        self.root = Path(root)
        self.key = key                                         # OwnerKey: needed only to record approvals
        self.db = self.root / "data" / "cost_price.db"
        self.sync_db = self.root / "data" / f"nessie_sync_{tag}.db"
        self.audit_path = self.root / "data" / "audit" / "changes.jsonl"
        self.lock_dir = self.root / "data" / "changes-locks"
        self._nessie = nessie
        self._today = today
        self._now = now

    @property
    def nessie(self):
        if self._nessie is None:
            from .shared import LiveNessie
            self._nessie = LiveNessie(self.root)
        return self._nessie

    @property
    def now(self) -> datetime:
        return self._now or datetime.now(timezone.utc)

    @property
    def today(self) -> date:
        return self._today or self.now.astimezone(RESTAURANT_TZ).date()

    def _real_clock(self):
        """Live runs and approvals use the real clock: an injected one is for tests only."""
        if (self._now or self._today) and not ALLOW_INJECTED_CLOCK:
            raise Refused("now=/today= are for tests only; a live run or an approval uses the real clock")

    def _start(self) -> date:
        """First day a menu change is in force: tomorrow in San Francisco. It is part of the signed plan, so an apply
        on a later day re-plans a later date and is refused until the owner approves again."""
        return self.today + timedelta(days=1)

    @contextmanager
    def _reader(self):
        """A reader that leaves the data directory exactly as it was (see quiet_path)."""
        with quiet_path(self.db) as path, connect(path) as con:
            yield con

    # ------------------------------------------------------------ approvals (human only)
    def subject(self, obj) -> dict:
        """Exactly what the owner is shown and signs: the suggestion, plus a fresh plan: planned_change for a menu
        change (api.planned_change), planned_bills for bill moves; for an undo, {"revert": change_id} plus
        planned_change when it undoes a menu change."""
        return self.plan(obj)[0]

    def plan(self, obj) -> tuple[dict, list[dict] | None]:
        """(subject, ops): the signed subject and the fresh plan it was built from (the ops also carry old values, for
        the CLI's question line). ops is None when nothing is planned before approval (dispute_charge, other)."""
        if isinstance(obj, dict):
            cid = valid_id(obj.get("revert"), "change_id")
            with self._reader() as con:
                ops = self._plan_revert(con, cid, self._start())
            return self._subject(obj, ops), ops
        valid_id(obj.suggestion_id)
        check_fields(obj)
        ops = None
        if obj.action in BILL_ACTIONS + MENU_ACTIONS:
            with self._reader() as con:
                ops = self._plan(con, obj, self._start(), current=None)
        return self._subject(obj, ops), ops

    @staticmethod
    def _subject(obj, ops) -> dict:
        out = {"revert": obj["revert"]} if isinstance(obj, dict) else obj.model_dump(mode="json")
        if ops and ops[0]["kind"] in ("price", "availability"):
            out["planned_change"] = planned_change(ops[0])
        elif not isinstance(obj, dict) and obj.action in BILL_ACTIONS:
            out["planned_bills"] = [dict(bill_id=o["bill_id"], from_date=o["expect"], to_date=o["to"]) for o in ops]
        return out

    def record_approval(self, subject, a: Approval, shown: dict | None = None):
        """Sign and store the owner's decision on exactly what they were shown (`shown`, from subject())."""
        if self.key is None:
            raise PermissionError("recording an approval needs the owner key (cost_price.owner.unlock)")
        self._real_clock()
        sid = valid_id(subject_id(subject)) if not isinstance(subject, dict) else None
        signed = self.subject(subject)
        sid = sid or f"revert:{signed['revert']}"
        if a.suggestion_id != sid:
            raise ValueError(f"approval is for {a.suggestion_id}, not {sid}")
        if _aware(a.decided_at) - self.now > MAX_CLOCK_SKEW:
            raise Refused(f"decided_at {utc_ts(a.decided_at)} is in the future")
        if shown is not None and canonical(shown) != canonical(signed):
            raise Refused("the plan changed while the owner was deciding; run approve again")
        with connect(self.db, "changes") as con, transaction(con):
            api.record_approval(con, self.key, suggestion_id=sid, subject=signed, decision=a.decision,
                                decided_by=a.decided_by, decided_at=utc_ts(a.decided_at),
                                note=a.note[:500] if a.note else None)
        self._audit("owner", "approval", sid, f"{a.decided_by} {a.decision} {sid}",
                    dict(a.model_dump(mode="json"), subject=signed))

    # ------------------------------------------------------------ public
    def apply(self, s: Suggestion, live=False) -> ExecutionResult:
        return self._run(s, s.suggestion_id, live, action=s.action, target=s.target)

    def revert(self, change_id: str, live=False) -> ExecutionResult:
        """Undo one change with a new change, approved like any other (subject {"revert": change_id, ...})."""
        return self._run({"revert": change_id}, f"revert:{change_id}", live, action="revert", target=change_id)

    def log(self) -> list[dict]:
        cols = ["change_id", "suggestion_id", "approval_id", "at", "system", "ref", "field", "old_value", "new_value",
                "reverts"]
        with self._reader() as con:
            return [{c: r[c] for c in cols} for r in api.change_log(con)]

    def pending(self) -> list[dict]:
        """Bill moves CH recorded an intent for but never logged: the bank may or may not have them. A live apply of
        that suggestion (or revert) re-reads each one and logs it if the bill is at its target."""
        with self._reader() as con:
            rows = con.execute("""SELECT * FROM nessie_intent i WHERE NOT EXISTS (SELECT 1 FROM change_log c
                                  WHERE c.suggestion_id = i.suggestion_id AND c.system = 'nessie' AND c.ref = i.bill_id)
                                  ORDER BY i.id""").fetchall()
            return [{k: r[k] for k in ("suggestion_id", "approval_id", "bill_id", "from_date", "to_date", "at")}
                    for r in rows]

    def menu(self, on: str) -> dict[str, int]:
        with self._reader() as con:
            return api.menu(con, on, courses=ALL_COURSES)[0]

    # ------------------------------------------------------------ the one path for apply and revert
    def _run(self, subject, sid, live, action, target) -> ExecutionResult:
        try:
            valid_id(target, "change_id") if action == "revert" else valid_id(sid)
            if live:
                self._real_clock()
        except Refused as exc:
            return self._refuse(sid, str(exc), live)
        try:
            with self._claim(sid) if live else _nothing():
                return self._run_claimed(subject, sid, live, action, target)
        except Refused as exc:
            return self._refuse(sid, str(exc), live)

    def _run_claimed(self, subject, sid, live, action, target) -> ExecutionResult:
        found = []
        try:
            if live:
                with connect(self.db, "changes") as con:
                    prior = _prior(con, sid, action, target)
                if prior:
                    return prior                                       # applying twice changes nothing
                found = self._reconcile(sid)                           # moves that landed, logged before any check
            with (connect(self.db, "changes") if live else self._reader()) as con:
                prior = _prior(con, sid, action, target)
                if prior:
                    return prior
                latest = api.latest_approval(con, sid)
                current = latest["id"] if latest and latest["decision"] == "approved" else None
                start = self._start()
                ops = (self._plan_revert(con, target, start) if action == "revert"
                       else self._plan(con, subject, start, current))
                signed = self._subject(subject, ops)
                if not live:
                    return ExecutionResult(suggestion_id=sid, applied=False, changes=[o["change"] for o in ops],
                                           error=DRY_RUN)
                aid, why = self._approval(con, sid, signed)
                if why:
                    note = f" (logged bill moves found at their target: {', '.join(found)})" if found else ""
                    return self._refuse(sid, _explain(why, latest, signed) + note, live)
        except Refused as exc:
            return self._refuse(sid, str(exc), live)
        except Exception as exc:                          # missing DB, null columns: refuse, don't crash
            return self._refuse(sid, f"could not plan ({type(exc).__name__}: {exc})", live)
        return self._execute(sid, aid, signed, ops, action=action, target=target, decided_by=latest["decided_by"])

    def _approval(self, con, sid, signed) -> tuple[int | None, str | None]:
        """api.current_approval plus the approval's age: what every write and every PUT re-checks."""
        aid, why = api.current_approval(con, sid, signed)
        if why:
            return None, why
        decided = api.latest_approval(con, sid)["decided_at"]
        at = _aware(datetime.fromisoformat(decided))
        if at - self.now > MAX_CLOCK_SKEW:
            return None, f"the approval is dated {decided}, more than {MAX_CLOCK_SKEW} in the future; check the clock"
        if self.now - at > MAX_APPROVAL_AGE:
            return None, (f"the approval of {decided} is more than "
                          f"{MAX_APPROVAL_AGE.total_seconds() / 3600:.0f} h old; approve again")
        return aid, None

    @contextmanager
    def _claim(self, sid):
        """One live run per suggestion: data/changes-locks/<base32 id>.lock, created exclusively, removed at the end.
        The lock records its holder's pid. An existing lock is cleared when that pid is no longer alive (a run killed
        by taskkill /F or SIGKILL), when it can't be parsed, or when it is older than LOCK_STALE (pid reuse)."""
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        path = self.lock_dir / f"{_b32(sid)}.lock"
        busy = Refused(f"another live apply of {sid} is already running (lock {path.name})")
        for attempt in range(3):
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    seen, age = path.read_bytes(), time.time() - path.stat().st_mtime
                except FileNotFoundError:
                    continue                                   # released meanwhile: try again
                except PermissionError:                        # Windows: its holder has it open right now
                    raise busy from None
                if attempt == 2 or not _lock_is_dead(seen, age):
                    raise busy from None
                _clear_lock(path, seen, busy)
        else:
            raise busy
        try:
            os.write(fd, f"pid {os.getpid()} at {utc_ts(datetime.now(timezone.utc))}\n".encode("ascii"))
            os.close(fd)
            yield
        finally:
            path.unlink(missing_ok=True)

    # ------------------------------------------------------------ planning: read only, raises Refused
    def _plan(self, con, s: Suggestion, start: date, current: int | None) -> list[dict]:
        on = start.isoformat()
        check_fields(s)
        if s.action == "reprice_item":
            old, new = api.effective_price(con, s.target, on), s.new_price_cents
            if old is None:
                raise Refused(f"'{s.target}' has no dine-in dinner price")
            if not new or new <= 0:
                raise Refused("reprice_item needs new_price_cents")
            if new == old:
                raise Refused(f"'{s.target}' already costs {new} cents")
            if abs(new - old) / old > MAX_PRICE_MOVE:
                raise Refused(f"{old} -> {new} cents moves the price more than {MAX_PRICE_MOVE:.0%}")
            check_title(s.title, old, new)
            return [self._price_op(s.target, old, new, on)]
        if s.action == "remove_item":
            if s.target not in api.menu(con, on, courses=ALL_COURSES)[0]:
                raise Refused(f"'{s.target}' is not on the menu")
            return [self._avail_op(s.target, "yes", "no", on)]
        if s.action in BILL_ACTIONS:
            return self._plan_bills(con, s, current)
        if s.action == "dispute_charge":
            if not BANK_ID_RE.fullmatch(s.target):
                raise Refused(f"'{s.target}' is not a bank_id (T + 9 hex)")
            with self._sync() as sync:
                row = sync.execute("SELECT amount, memo FROM bank_tx WHERE bank_id=?", (s.target,)).fetchone()
            if not row:
                raise Refused(f"no bank row {s.target}")
            return [self._dispute_op(s.target, None, "open", f"{s.title} | {row[1]} {row[0]} cents")]
        raise Refused(f"action '{s.action}' is not something CH can apply")

    def _plan_bills(self, con, s: Suggestion, current: int | None) -> list[dict]:
        """Bills to move, each (from, to). An intent keeps its recorded absolute target only if it was written under
        the current approval, or its move is known to have landed (logged, or the sync DB shows it at the target);
        everything else is planned from scratch. An intent of a superseded approval that disagrees with the fresh plan
        blocks that bill (one intent per suggestion and bill), so the suggestion is refused: use a new id."""
        sid, today = s.suggestion_id, self.today.isoformat()
        logged = api.logged_refs(con, sid, "nessie")
        synced = self._sync_dates()
        kept, stale = {}, {}
        for b, i in api.intents(con, sid).items():
            if i["approval_id"] == current or b in logged or synced.get(b) == i["to_date"]:
                kept[b] = (i["from_date"], i["to_date"])
            else:
                stale[b] = i
        fresh = {}
        if s.action == "stretch_supplier":
            days = s.extra_days
            if not days or not 0 < days <= MAX_STRETCH_DAYS:
                raise Refused(f"stretch_supplier needs extra_days, 1..{MAX_STRETCH_DAYS}")
            for b, (f, t) in kept.items():
                if (date.fromisoformat(t) - date.fromisoformat(f)).days != days:
                    raise Refused(f"bill {b} was recorded moving {f} -> {t}, which isn't +{days} days: "
                                  f"resync Nessie and use a new suggestion id")
            for b, d, _ in self._pending_bills(payee=s.target):
                if b not in kept:
                    fresh[b] = (d, (date.fromisoformat(d) + timedelta(days=days)).isoformat())
            if not kept and not fresh:
                raise Refused(f"no pending Nessie bills for '{s.target}'")
        else:
            for b, (f, t) in kept.items():
                if t >= f:
                    raise Refused(f"bill {b} was recorded moving {f} -> {t}, which isn't paying it earlier")
                if b not in logged and synced.get(b) != t and t < today:
                    raise Refused(f"bill {b} was to be paid on {t}, which has passed: use a new suggestion id")
            if not kept:
                bills = self._pending_bills(doc=s.target)
                if len(bills) != 1:
                    raise Refused(f"pay_bill needs exactly one pending bill for '{s.target}', found {len(bills)}")
                b, d, n = bills[0]
                if d <= today:
                    raise Refused(f"{n} is already scheduled for {d}")
                fresh[b] = (d, today)
        for b, i in stale.items():
            if b in fresh and fresh[b] != (i["from_date"], i["to_date"]):
                raise Refused(f"bill {b} has an unconfirmed move {i['from_date']} -> {i['to_date']} from superseded "
                              f"approval #{i['approval_id']}, and CH records one target per bill per suggestion: "
                              f"run `changes pending`, resync Nessie, and resubmit under a new suggestion id")
        plan = {**fresh, **kept}
        return [self._bill_op(b, expect=f, to=t) for b, (f, t) in sorted(plan.items(), key=lambda x: (x[1], x[0]))]

    def _plan_revert(self, con, change_id, start: date) -> list[dict]:
        c = api.change(con, change_id)
        if not c:
            raise Refused(f"no change {change_id}")
        ref, field, old, new, on = c["ref"], c["field"], c["old_value"], c["new_value"], start.isoformat()
        if c["system"] == "nessie":
            ops = [self._bill_op(ref, expect=new, to=old)]             # refused at run time if the bill moved since
        elif field == "price_cents":
            now = api.effective_price(con, ref, on)
            if now != int(new):
                raise Refused(f"'{ref}' is now {now} cents, not {new}: a later change would be undone")
            ops = [self._price_op(ref, int(new), int(old), on)]
        elif field == "on_menu":                                       # swap back: removed again if it was removed
            ops = [self._avail_op(ref, new, old, on)]
        else:
            ops = [self._dispute_op(ref, new, old or "withdrawn", "revert")]
        for o in ops:
            o["reverts"] = change_id
        return ops

    @contextmanager
    def _sync(self):
        """The Nessie sync DB (written by nessie_sync.py, not cost_price), read-only and without leaving files."""
        if not self.sync_db.exists():
            raise Refused(f"no Nessie sync at {self.sync_db.name}; run nessie_sync.py first")
        with quiet_path(self.sync_db) as path:
            con = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
            try:
                yield con
            finally:
                con.close()

    def _sync_dates(self) -> dict[str, str | None]:
        if not self.sync_db.exists():
            return {}
        with self._sync() as con:
            return {b: bank_day(d) for b, d in con.execute("SELECT nessie_id, payment_date FROM bank_bills "
                                                           "WHERE nessie_id IS NOT NULL")}

    def _pending_bills(self, payee=None, doc=None):
        """[(nessie_id, payment_date, nickname)] from the last Nessie sync. Pending AP bills only, never autopays."""
        with self._sync() as con:
            rows = con.execute("SELECT nessie_id, payment_date, nickname, payee FROM bank_bills WHERE status='pending'"
                               " AND nessie_id IS NOT NULL AND payment_date IS NOT NULL ORDER BY payment_date").fetchall()
        if payee:
            hits = [(b, d, n) for b, d, n, p in rows if (p or "").removesuffix(" [SIM]").lower() == payee.lower()]
        else:
            hits = [(b, d, n) for b, d, n, p in rows if b == doc or (n or "").split(" due ")[0] == doc]
        out = []
        for b, d, n in hits:
            if bank_day(d) is None:
                raise Refused(f"the sync DB has an unreadable payment_date {d!r} for bill {b}: resync Nessie")
            out.append((b, bank_day(d), n))
        return sorted(out, key=lambda x: (x[1], x[0]))

    # each op: the Change to report, plus how to record it in cost_price.db
    @staticmethod
    def _price_op(item, old, new, on):
        return dict(kind="price", item=item, on=on, price=new,
                    change=Change(system="menu_prices", ref=item, field="price_cents", old_value=old, new_value=new))

    @staticmethod
    def _avail_op(item, old, new, on):
        return dict(kind="availability", item=item, on=on, available=0 if new == "no" else 1,
                    change=Change(system="menu_prices", ref=item, field="on_menu", old_value=old, new_value=new))

    @staticmethod
    def _dispute_op(bank_id, old, state, note):
        return dict(kind="dispute", bank_id=bank_id, state=state, note=note[:500],
                    change=Change(system="other", ref=bank_id, field="dispute", old_value=old, new_value=state))

    @staticmethod
    def _bill_op(bill_id, expect, to):
        return dict(kind="nessie", bill_id=bill_id, expect=expect, to=to,
                    change=Change(system="nessie", ref=bill_id, field="payment_date", old_value=expect, new_value=to))

    # ------------------------------------------------------------ execution
    def _execute(self, sid, aid, signed, ops, action, target, decided_by) -> ExecutionResult:
        done, error = [], None
        try:
            for n, op in enumerate(ops, 1):
                if op["kind"] == "nessie":
                    error = self._move_bill(sid, aid, signed, op)
                    if error:
                        break
                else:
                    with connect(self.db, "changes") as con, transaction(con):
                        _, why = self._approval(con, sid, signed)
                        if why:
                            error = f"stopped: {why}"
                            break
                        self._log(con, sid, aid, op, f"C-{sid}-{n}")
                done.append(op)
            if error is None:
                with connect(self.db, "changes") as con, transaction(con):
                    _, why = self._approval(con, sid, signed)
                    if why:
                        error = f"stopped: {why}; the moves already made are logged"
                    else:
                        result = ExecutionResult(suggestion_id=sid, applied=True, changes=[o["change"] for o in done])
                        api.mark_applied(con, suggestion_id=sid, action=action, target=target, decided_by=decided_by,
                                         result_json=result.model_dump_json())
        except sqlite3.Error as exc:                      # a trigger or lock refused a write: report, don't crash
            error = f"database refused a write ({type(exc).__name__}: {exc}); what is logged stands"
        except Exception as exc:                          # anything else (a bank answer of the wrong shape, ...);
            error = f"stopped by {type(exc).__name__}: {exc}; what is logged stands"   # never KeyboardInterrupt
        result = ExecutionResult(suggestion_id=sid, applied=error is None, changes=[o["change"] for o in done],
                                 error=error)
        self._audit("executor", "change", sid, f"{action} on {target}: " + (error or f"{len(done)} change(s) applied"),
                    result.model_dump(mode="json"))
        return result

    def _get_day(self, bill) -> tuple[dict, str | None]:
        """GET /bills/{id}: (the bill, its payment_date as YYYY-MM-DD or None). Raises on anything but an object."""
        live = self.nessie.get_bill(bill)
        if not isinstance(live, dict):
            raise TypeError(f"Nessie returned a {type(live).__name__} for bill {bill}, not an object")
        return live, bank_day(live.get("payment_date"))

    def _move_bill(self, sid, aid, signed, op) -> str | None:
        """One bill, safe to retry. The intent (from, to) is committed before the PUT; on a retry a bill at intent.to
        is logged as done, a bill at intent.from is PUT to intent.to, anything else is stale. None = moved and logged."""
        bill = op["bill_id"]
        with connect(self.db, "changes") as con:
            if bill in api.logged_refs(con, sid, "nessie"):
                return None
            intent = api.intents(con, sid).get(bill)
        frm, to = (intent["from_date"], intent["to_date"]) if intent else (op["expect"], op["to"])
        if (frm, to) != (op["expect"], op["to"]):
            return f"bill {bill}: recorded intent {frm} -> {to} differs from the plan; nothing moved"
        aids = (aid, intent["approval_id"]) if intent else (aid,)
        try:
            live, day = self._get_day(bill)
        except Exception as exc:
            return f"Nessie call failed on {bill} ({type(exc).__name__}: {exc}); retry to finish"
        if intent and day == to:                                       # moved by an earlier run, or by someone else
            return self._log_bill(sid, aids, op, frm, to, by_ch=self._put_on_record(sid, bill, to))
        if day is None:
            return f"bill {bill} has an unreadable payment_date {live.get('payment_date')!r}: nothing moved"
        if day != frm:
            return f"bill {bill} is now due {day}, not {frm}: resync Nessie and re-plan"
        if live.get("status") != "pending":
            return f"bill {bill} is '{live.get('status')}', not pending: nothing moved"
        if to < self.today.isoformat():
            return f"bill {bill} would be moved to {to}, before today ({self.today}): nothing moved"
        with connect(self.db, "changes") as con, transaction(con):    # the last check before the bank call
            _, why = self._approval(con, sid, signed)
            if why:
                return f"stopped before {bill}: {why}"
            if not intent:
                api.add_intent(con, suggestion_id=sid, approval_id=aid, bill_id=bill, from_date=frm, to_date=to)
        put_error = None
        try:
            self.nessie.set_payment_date(bill, to)
        except Exception as exc:                          # the PUT may still have landed: the re-read decides
            put_error = exc
        else:                                             # on record, so a later run knows this move was CH's
            self._audit("executor", "nessie_put", sid, f"PUT {bill} payment_date={to}: ok",
                        dict(bill_id=bill, to_date=to, approval_id=aid))
        try:
            after = self._get_day(bill)[1]
        except Exception as exc:
            return f"Nessie call failed on {bill} ({type(put_error or exc).__name__}); retry to finish"
        if after != to:
            if put_error:
                return f"Nessie call failed on {bill} ({type(put_error).__name__}); retry to finish"
            return f"Nessie did not keep the new payment_date on {bill}"
        return self._log_bill(sid, aids, op, frm, to, by_ch=put_error is None)

    def _put_on_record(self, sid, bill, to) -> bool:
        """Did an earlier run of CH get a successful answer to this exact PUT? (from the audit trail)"""
        try:
            lines = self.audit_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return False
        for line in lines:
            try:
                e = json.loads(line)
                if (e.get("action"), e.get("ref"), e["data"].get("bill_id"), e["data"].get("to_date")) == (
                        "nessie_put", sid, bill, to):
                    return True
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
        return False

    def _log_bill(self, sid, aids, op, frm, to, by_ch=True):
        """Log one bill move. by_ch=False: the bill was found at its target but CH has no successful PUT of its own
        (its PUT errored, or never ran): change_id gets the -observed suffix. Logged under the current approval, or,
        if that was revoked meanwhile, under the intent's own approval (cost_price's intent exemption)."""
        cid = f"C-{sid}-{op['bill_id']}" + ("" if by_ch else OBSERVED)
        error = None
        for a in dict.fromkeys(x for x in aids if x is not None):
            try:
                with connect(self.db, "changes") as con, transaction(con):
                    api.log_change(con, change_id=cid, suggestion_id=sid, approval_id=a, system="nessie",
                                   ref=op["bill_id"], field="payment_date", old_value=frm, new_value=to,
                                   reverts=op.get("reverts"), at=utc_ts(self.now))
                return None
            except sqlite3.IntegrityError as exc:
                error = exc
        raise error

    def _reconcile(self, sid) -> list[str]:
        """Before any approval check: every intent of this suggestion with no change_log row is re-read at the bank,
        and logged under the intent's own approval if the bill is at its target. A move that happened is always on
        record, even if the owner has since rejected or the approval expired."""
        with connect(self.db, "changes") as con:
            open_ = {b: i for b, i in api.intents(con, sid).items() if b not in api.logged_refs(con, sid, "nessie")}
        found = []
        reverts = sid.removeprefix("revert:") if sid.startswith("revert:") else None
        for b, i in sorted(open_.items()):
            try:
                day = self._get_day(b)[1]
            except Exception:
                continue                                  # unreachable: the move itself retries and reports it
            if day == i["to_date"]:
                by_ch = self._put_on_record(sid, b, day)
                op = dict(bill_id=b, reverts=reverts)
                self._log_bill(sid, (i["approval_id"],), op, i["from_date"], day, by_ch=by_ch)
                found.append(f"C-{sid}-{b}" + ("" if by_ch else OBSERVED))
        if found:
            self._audit("executor", "reconciled", sid, f"logged {len(found)} bill move(s) found at their target",
                        dict(change_ids=found))
        return found

    def _log(self, con, sid, aid, op, cid):
        """A menu or dispute change: the log row and its child row, in exactly the shapes cost_price's triggers and
        readers accept (child row = log row = the signed planned_change; reverts restore the reverted old_value)."""
        if api.change(con, cid):
            return
        c = op["change"]
        api.log_change(con, change_id=cid, suggestion_id=sid, approval_id=aid, system=c.system, ref=c.ref,
                       field=c.field, old_value=c.old_value, new_value=c.new_value, reverts=op.get("reverts"),
                       at=utc_ts(self.now))
        if op["kind"] == "price":
            api.add_price_change(con, change_id=cid, item=op["item"], effective_from=op["on"], price_cents=op["price"],
                                 channel=CHANNEL, service=SERVICE)
        elif op["kind"] == "availability":
            api.add_availability_change(con, change_id=cid, item=op["item"], effective_from=op["on"],
                                        available=op["available"])
        elif op["kind"] == "dispute":
            api.add_dispute(con, change_id=cid, bank_id=op["bank_id"], state=op["state"], note=op["note"],
                            opened_at=utc_ts(self.now))

    def _refuse(self, sid, why, live=True) -> ExecutionResult:
        result = ExecutionResult(suggestion_id=sid, applied=False, error=f"refused: {why}")
        if live:                                          # a dry run leaves no trace
            self._audit("executor", "refused", sid, why, result.model_dump(mode="json"))
        return result

    def _audit(self, actor, action, ref, summary, data):
        """Append-only JSON lines in the shared AuditEntry shape, for audit.py to collect."""
        entry = AuditEntry(at=datetime.now(timezone.utc), actor=actor, action=action, ref=ref, summary=summary, data=data)
        self.audit_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.audit_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry.model_dump(mode="json")) + "\n")


def check_fields(s: Suggestion):
    """new_price_cents belongs to reprice_item only, extra_days to stretch_supplier only: a stray value would be shown
    and signed without doing anything. Checked at approve (plan) and at apply (_plan); contracts.Suggestion could
    enforce it too."""
    for field, action in FIELD_ACTIONS.items():
        if getattr(s, field, None) is not None and s.action != action:
            raise Refused(f"{field} belongs only to {action}, not to {s.action}: remove it from the suggestion")


def check_title(title: str, old: int, new: int):
    """A reprice title may name dollar figures only if each one is the planned old or new price, so the AI's title
    can't tell the owner a different number from the one they sign."""
    for m in DOLLARS_RE.finditer(title or ""):
        whole, _, frac = m.group()[1:].partition(".")
        if int(whole) * 100 + int(frac or 0) not in (old, new):
            raise Refused(f"the title says {m.group()}, but the plan is {usd(old)} -> {usd(new)}: "
                          f"fix the title or new_price_cents")


def usd(cents: int) -> str:
    return f"${cents / 100:,.2f}"


def pid_alive(pid: int) -> bool:
    """Is process `pid` still running? Windows: OpenProcess + GetExitCodeProcess == STILL_ACTIVE (os.kill would
    terminate it there). Elsewhere: os.kill(pid, 0)."""
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = wintypes.HANDLE
        k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        k32.CloseHandle.argtypes = (wintypes.HANDLE,)
        handle = k32.OpenProcess(0x1000, False, pid)              # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5                   # ERROR_ACCESS_DENIED: it exists, someone else's
        try:
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True                                       # can't tell: keep it (LOCK_STALE still applies)
            return code.value == 259                              # STILL_ACTIVE
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _lock_is_dead(content: bytes, age: float) -> bool:
    """Clear it? Older than LOCK_STALE, or unparsable, or its pid is gone. An empty lock younger than LOCK_EMPTY_GRACE
    belongs to a holder that hasn't written its pid yet."""
    if age > LOCK_STALE.total_seconds():
        return True
    if not content:
        return age > LOCK_EMPTY_GRACE
    m = LOCK_RE.match(content.decode("ascii", "replace"))
    return m is None or not pid_alive(int(m.group(1)))


def _clear_lock(path: Path, seen: bytes, busy: Refused):
    """Remove the dead lock `path` (content `seen`) without removing a live one made since: rename it away first (only
    one process wins), and put it back if what was renamed isn't what was judged dead."""
    grave = path.with_name(f"{path.name}.{os.getpid()}.dead")
    try:
        os.replace(path, grave)
    except FileNotFoundError:
        return                                                    # someone else cleared it
    except PermissionError:
        raise busy from None
    if grave.read_bytes() == seen:
        grave.unlink(missing_ok=True)
        return
    try:
        os.rename(grave, path)                                    # a new holder's lock: give it back
    except OSError:
        grave.unlink(missing_ok=True)
    raise busy


@contextmanager
def _nothing():
    yield


def _aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _prior(con, sid, action, target) -> ExecutionResult | None:
    """The stored result of an earlier apply of this same thing. Keyed by (suggestion_id, action, target), so a
    suggestion can't stand in for a revert (or another suggestion) that reuses its id."""
    row = con.execute("SELECT action, target, result_json FROM applied WHERE suggestion_id = ?", (sid,)).fetchone()
    if row is None:
        return None
    if (row["action"], row["target"]) != (action, target):
        raise Refused(f"{sid} was already applied as {row['action']} on {row['target']!r}, not {action} on {target!r}")
    return ExecutionResult.model_validate_json(row["result_json"])


def _explain(why, latest, signed) -> str:
    """Name the cases that matter: same suggestion, but the bills to move or the planned menu row (its date) differ."""
    if not (latest and "changed after" in why):
        return why
    try:
        old = json.loads(latest["subject_json"])
    except ValueError:
        return why
    for key in ("planned_bills", "planned_change"):
        if key in signed and {k: v for k, v in old.items() if k != key} == {k: v for k, v in signed.items()
                                                                            if k != key}:
            if key == "planned_bills":
                return (f"the bills to move now ({_bills(signed)}) differ from the ones the owner approved "
                        f"({_bills(old)}); approve again")
            return (f"the planned change now ({_change(signed)}) differs from the one the owner approved "
                    f"({_change(old)}): the date rolled over or the menu changed; approve again")
    return why


def _bills(subject) -> str:
    return ", ".join(f"{b['bill_id']} {b['from_date']}->{b['to_date']}" for b in subject.get("planned_bills", []))


def _change(subject) -> str:
    p = subject.get("planned_change") or {}
    what = f"{p.get('price_cents')} cents" if p.get("table") == "price_change" else f"available={p.get('available')}"
    return f"{p.get('item')} {what} from {p.get('effective_from')}"


@contextmanager
def quiet_path(path: Path):
    """A path to `path`'s content that SQLite can open read-only without creating files next to it.

    Opening a WAL database read-only creates <db>-wal and <db>-shm when they don't exist (a read-only connection can't
    remove them again), and writes its read locks into an existing -shm. So a WAL database is read from a copy (main
    file plus -wal, never -shm: SQLite rebuilds the index from the -wal) in a temporary directory; the copy is retaken
    if either file changed while it was being made. Rollback-journal databases (the Nessie sync DB) are read in place:
    a read-only open of those creates nothing."""
    path = Path(path)
    with open(path, "rb") as f:
        header = f.read(100)
    if len(header) < 20 or header[18] != 2:
        yield path
        return
    wal = path.with_name(path.name + "-wal")

    def stamp():
        return [(p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None for p in (path, wal)]

    with tempfile.TemporaryDirectory(prefix="changes-read-", ignore_cleanup_errors=True) as tmp:
        copy = Path(tmp) / path.name
        for _ in range(5):
            before = stamp()
            shutil.copyfile(path, copy)
            if before[1] is not None:
                shutil.copyfile(wal, Path(tmp) / wal.name)
            if stamp() == before:
                break
        else:
            raise Refused(f"{path.name} kept changing while being read; try again")
        yield copy


def _b32(id_: str) -> str:
    """Base32 of the id: reversible, one letter case, no path characters; never a Windows reserved name."""
    stem = base64.b32encode(id_.encode("utf-8")).decode("ascii").rstrip("=")
    if stem.upper() in WINDOWS_RESERVED:
        raise Refused(f"file name for {id_!r} would be the reserved device name {stem}")
    return stem


def receipt_name(id_: str) -> str:
    """'<base32 of the id>.approval.json': reversible (receipt_id), collision-free even on case-insensitive file
    systems (base32 is one case), never a path separator, and never a Windows reserved name."""
    return f"{_b32(id_)}.approval.json"


def receipt_id(name: str) -> str:
    stem = name.removesuffix(".approval.json")
    return base64.b32decode(stem + "=" * (-len(stem) % 8)).decode("utf-8")


WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10))}
