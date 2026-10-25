"""Every connection to cost_price.db goes through connect(path, role).

roles    reader  read-only (mode=ro): ASI, the Forecaster, `show`, `verify`
         scout   may INSERT into competitor, competitor_observation, observation_check, market_summary
         changes may INSERT into approval, nessie_intent, change_log, price_change, availability_change, dispute, applied
         owner   may INSERT into owner (one row, once)
         build   may DELETE/INSERT the reference layer and INSERT seed competitors/observations

Each role runs under an SQLite authorizer that refuses everything else: UPDATE or DELETE of history, DROP, ALTER,
CREATE, ATTACH, and any PRAGMA except read-only checks. recursive_triggers is on, so the delete hidden inside an
INSERT OR REPLACE fires the append-only trigger too. Before handing out a connection, the live schema is compared with
the expected one statement by statement: a trigger whose body was swapped for a no-op is caught, and writers refuse to
start. These guards stop agents and code mistakes; someone with the file and their own sqlite3 can still edit it,
which `verify` then detects (and a signed seal, see verify.py, detects even a rewrite of every hash)."""
import hashlib
import json
import math
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .schema import GENESIS, HISTORY, REFERENCE, SCHEMA_VERSION, ddl

INSERTS = {
    "reader": set(),
    "scout": {"competitor", "competitor_observation", "observation_check", "market_summary"},
    "changes": {"approval", "nessie_intent", "change_log", "price_change", "availability_change", "dispute", "applied"},
    "owner": {"owner"},
    "build": set(REFERENCE) | {"competitor", "competitor_observation"},
}
DELETES = {"build": set(REFERENCE)}
TABLES = set(HISTORY) | set(REFERENCE)
READ_PRAGMAS = {"integrity_check", "quick_check", "foreign_key_check", "user_version"}      # no argument = read only
TABLE_PRAGMAS = {"table_info", "table_xinfo", "foreign_key_check"}                          # argument is a table name
ALWAYS_OK = {sqlite3.SQLITE_READ, sqlite3.SQLITE_SELECT, sqlite3.SQLITE_FUNCTION, sqlite3.SQLITE_TRANSACTION,
             sqlite3.SQLITE_SAVEPOINT, sqlite3.SQLITE_RECURSIVE}


class TamperError(RuntimeError):
    """The database is not in the state its own rules guarantee: refuse to touch it."""


def now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def authorizer(role):
    inserts, deletes = INSERTS[role], DELETES.get(role, set())

    def check(action, arg1, arg2, dbname, source):
        if action in ALWAYS_OK:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_INSERT and arg1 in inserts:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_DELETE and arg1 in deletes:
            return sqlite3.SQLITE_OK
        if action == sqlite3.SQLITE_PRAGMA and ((arg1 in READ_PRAGMAS and arg2 is None) or
                                                (arg1 in TABLE_PRAGMAS and arg2 in TABLES)):
            return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

    return check


def _uri(path: Path, mode: str) -> str:
    return Path(path).resolve().as_uri() + f"?mode={mode}"


def _open(target, mode: str, role: str) -> sqlite3.Connection:
    """mode: 'ro' | 'rw' | 'rwc', or 'memory' for the expected-schema scratch database."""
    if mode == "memory":
        con = sqlite3.connect(":memory:", autocommit=True)
    else:
        con = sqlite3.connect(_uri(target, mode), uri=True, timeout=5, autocommit=True)   # explicit BEGIN/COMMIT only
    con.row_factory = sqlite3.Row
    con.create_function("cp_role", 0, lambda: role)
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA recursive_triggers = ON")       # REPLACE's implicit delete must hit the no_delete trigger
    con.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, True)                  # no writable_schema edits
    con.setconfig(sqlite3.SQLITE_DBCONFIG_TRUSTED_SCHEMA, True)             # triggers call cp_role(); the schema
    return con                                                              # itself is checked before any use


def _statements(script: str):
    """Split DDL into complete statements (triggers contain ';' inside BEGIN...END)."""
    buf = ""
    for line in script.splitlines(keepends=True):
        buf += line
        if sqlite3.complete_statement(buf):
            if buf.strip():
                yield buf.strip()
            buf = ""
    if buf.strip():
        raise ValueError(f"incomplete DDL statement: {buf[:80]}")


def _schema_of(con) -> dict[tuple[str, str], str]:
    """Every schema object, including any named sqlite_* (a hidden trigger could be planted under such a name); only
    SQLite's own automatic indexes, which have no SQL, are left out."""
    return {(r[0], r[1]): " ".join((r[2] or "").split())
            for r in con.execute("SELECT type, name, sql FROM sqlite_master")
            if not (r[0] == "index" and r[1].startswith("sqlite_autoindex_") and r[2] is None)}


@lru_cache(maxsize=1)
def _expected():
    """(sqlite_master contents, {table: ((column, type), ...)}) of exactly the expected schema, built once in a
    throwaway in-memory database. Plain data, so it is safe to share across threads."""
    con = _open(None, "memory", "build")
    try:
        for stmt in _statements(ddl()):
            con.execute(stmt)
        cols = {t: tuple((r[1], r[2].upper()) for r in con.execute(f"PRAGMA table_info({t})")) for t in TABLES}
        return _schema_of(con), cols
    finally:
        con.close()


def expected_schema() -> dict[tuple[str, str], str]:
    return _expected()[0]


def check_schema(con):
    version = con.execute("PRAGMA user_version").fetchone()[0]
    if version != SCHEMA_VERSION:
        raise TamperError(f"schema version {version}, expected {SCHEMA_VERSION}: rebuild with `cost-price build`")
    have, want = _schema_of(con), expected_schema()
    if have != want:
        changed = sorted({k[1] for k in set(have) ^ set(want)} |
                         {k[1] for k in have.keys() & want.keys() if have[k] != want[k]})
        raise TamperError(f"schema differs from the expected one ({', '.join(changed[:5])}): the database was "
                          "altered; run `cost-price verify` and rebuild from a trusted copy")


@contextmanager
def connect(path: Path, role="reader"):
    """A guarded connection. Writers get WAL, a 5 s busy timeout and must write inside transaction(con)."""
    if role not in INSERTS:
        raise ValueError(f"unknown role {role!r}")
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"{path} not found: run `cost-price build` first")
    con = _open(path, "ro" if role == "reader" else "rw", role)
    try:
        check_schema(con)
        con.set_authorizer(authorizer(role))
        yield con
    finally:
        con.close()


@contextmanager
def transaction(con):
    """BEGIN IMMEDIATE: take the write lock up front, so a concurrent writer waits instead of failing mid-way."""
    con.execute("BEGIN IMMEDIATE")
    try:
        yield con
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def create(path: Path):
    """A new, empty database with the full schema. Refuses to touch an existing file."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = _open(path, "rwc", "build")
    try:
        con.execute("PRAGMA journal_mode = WAL")
        con.execute("BEGIN IMMEDIATE")
        for stmt in _statements(ddl()):
            con.execute(stmt)
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        con.execute("COMMIT")
    finally:
        con.close()


# ---------------------------------------------------------------- hash chain
def _table_columns(table) -> tuple[tuple[str, str], ...]:
    """(name, declared type) per column, from the expected schema (never from the live file)."""
    return _expected()[1][table]


def columns(con, table) -> list[str]:
    return [c for c, _ in _table_columns(table)]


def _clean_value(table, col, kind, v):
    """Exactly what SQLite will store, so the hash covers that (an int bound to a REAL column comes back as a float;
    NaN comes back as NULL). Refuses values the database would silently change or that hide content."""
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(f"{table}.{col}: booleans are not allowed, use 0/1")
    if kind == "INTEGER":
        if isinstance(v, float) and v.is_integer():
            v = int(v)
        if not isinstance(v, int):
            raise ValueError(f"{table}.{col}: expected an integer, got {type(v).__name__}")
        return v
    if kind == "REAL":
        if not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(f"{table}.{col}: expected a finite number, got {v!r}")
        return float(v) + 0.0                             # -0.0 -> 0.0: SQLite stores both as 0.0
    if not isinstance(v, str):
        raise ValueError(f"{table}.{col}: expected text, got {type(v).__name__}")
    if "\x00" in v or any(unicodedata.category(ch) == "Cs" for ch in v):
        raise ValueError(f"{table}.{col}: NUL bytes and lone surrogates are not allowed")
    return v


def row_hash(table: str, row: dict) -> str:
    body = {k: v for k, v in row.items() if k != "row_hash"}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(f"{table}\n{data}".encode("utf-8")).hexdigest()


def append(con, table: str, values: dict, unique: tuple[str, ...] = (), finalize=None) -> int:
    """Append one history row, continuing the table's hash chain. With `unique`, an existing row with the same values
    in those columns is returned instead (so loading the same thing twice adds nothing). `finalize(row)` may add
    fields computed from the row's final id and prev_hash (approval signatures). Call inside transaction()."""
    if table not in HISTORY:
        raise ValueError(f"{table} is not a history table")
    if not con.in_transaction:
        raise RuntimeError("append() must run inside transaction(con)")
    kinds = dict(_table_columns(table))
    unknown = (set(values) | set(unique)) - set(kinds)
    if unknown:
        raise ValueError(f"{table} has no column(s) {sorted(unknown)}")
    clean = {c: _clean_value(table, c, kinds[c], values.get(c)) for c in kinds if c not in ("id", "prev_hash", "row_hash")}
    if unique:
        where = " AND ".join(f"{c} IS ?" for c in unique)
        hit = con.execute(f"SELECT id FROM {table} WHERE {where}", [clean.get(c) for c in unique]).fetchone()
        if hit:
            return hit[0]
    last = con.execute(f"SELECT id, row_hash FROM {table} ORDER BY id DESC LIMIT 1").fetchone()
    row = dict(clean, id=(last[0] + 1) if last else 1, prev_hash=last[1] if last else GENESIS)
    if finalize:
        extra = finalize(dict(row))
        row.update({k: _clean_value(table, k, kinds[k], v) for k, v in extra.items()})
    row["row_hash"] = row_hash(table, row)
    names = list(row)
    con.execute(f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' * len(names))})",
                [row[n] for n in names])
    return row["id"]


def verify_structure(con) -> list[str]:
    """Schema, SQLite integrity, foreign keys and every hash chain. [] = clean."""
    problems = []
    try:
        check_schema(con)
    except TamperError as exc:
        problems.append(str(exc))
    problems += [f"integrity: {r[0]}" for r in con.execute("PRAGMA integrity_check") if r[0] != "ok"]
    problems += [f"foreign key: {tuple(r)}" for r in con.execute("PRAGMA foreign_key_check")]
    for table in HISTORY:
        prev, expect_id = GENESIS, 1
        for r in con.execute(f"SELECT * FROM {table} ORDER BY id"):
            r = dict(r)
            if r["id"] != expect_id:
                problems.append(f"{table} id {r['id']}: ids not consecutive (rows deleted or inserted)")
            if r["prev_hash"] != prev:
                problems.append(f"{table} id {r['id']}: chain broken (prev_hash)")
            if row_hash(table, r) != r["row_hash"]:
                problems.append(f"{table} id {r['id']}: row edited (row_hash)")
            prev, expect_id = r["row_hash"], r["id"] + 1
    return problems


def open_readonly(path: Path) -> sqlite3.Connection:
    """For verify: read-only, without the up-front schema check (verify reports schema problems instead)."""
    return _open(path, "ro", "reader")


def heads(con) -> dict[str, dict]:
    """The last (id, row_hash) of each chain: what a seal records."""
    out = {}
    for t in sorted(HISTORY):
        r = con.execute(f"SELECT id, row_hash FROM {t} ORDER BY id DESC LIMIT 1").fetchone()
        out[t] = {"id": r[0], "row_hash": r[1]} if r else {"id": 0, "row_hash": GENESIS}
    return out
