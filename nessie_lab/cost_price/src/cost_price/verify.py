"""`cost-price verify` and `cost-price seal`.

verify  schema identical to the expected one; SQLite integrity and foreign keys; every hash chain intact with
        consecutive ids; every approval signed by the owner's key; every CH change covered by its approval.
seal    the owner signs, into data/cost_price.seal.json: the owner's public key, the head (last id + row_hash) of
        every history chain, and a digest of the reference layer and of the seed rows (neither is hash-chained:
        the reference layer is rebuilt, and seed rows are what a cp_role() spoof could plant). verify then also
        proves that no sealed row was removed or rewritten - even by someone who recomputed every hash - that the
        books' prices and the seed list are unchanged, and that the database wasn't swapped for one with another owner.
        Once an owner exists, a missing seal is a problem, not a note. Rows added after the seal are covered by the
        chain only: review them and re-seal (also after a legitimate `build`).

Limits: rolling back the database *and* its seal together to an older pair can't be caught from inside the computer.
verify prints when it was sealed and how many rows each chain had; compare with a copy kept elsewhere. And a seal is
only as good as knowing it is *your* key: compare the fingerprint verify prints with the one owner-init printed."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from .api import change_validity
from .db import heads, open_readonly, verify_structure
from .owner import approval_payload, canonical, fingerprint, public_key, verify_signature
from .schema import HISTORY, REFERENCE

SEAL_LABEL = "cost_price seal v2\n"
SEED_ROWS = {"competitor": "found_via = 'seed'", "competitor_observation": "reader = 'seed'"}


def seal_path(db: Path) -> Path:
    return Path(db).with_name(Path(db).stem + ".seal.json")


def _seal_payload(body: dict) -> bytes:
    return (SEAL_LABEL + canonical(body)).encode("utf-8")


def _digest(con, sql: str) -> str:
    h = hashlib.sha256()
    for r in con.execute(sql):
        h.update(canonical(list(r)).encode("utf-8") + b"\n")
    return h.hexdigest()


def digests(con) -> dict[str, str]:
    """Order-stable digests of everything the hash chains don't cover."""
    out = {f"reference:{t}": _digest(con, f"SELECT * FROM {t} ORDER BY {', '.join(_pk(con, t))}") for t in sorted(REFERENCE)}
    for t, where in SEED_ROWS.items():
        out[f"seed:{t}"] = _digest(con, f"SELECT * FROM {t} WHERE {where} ORDER BY id")
    return out


def _pk(con, table) -> list[str]:
    cols = [(r[5], r[1]) for r in con.execute(f"PRAGMA table_info({table})")]
    pk = [name for order, name in sorted(cols) if order > 0]
    return pk or [name for _, name in cols]


def seal(db: Path, key) -> dict:
    """Write a signed seal of the current state. Refuses to seal a database that doesn't verify."""
    problems = verify(db, require_seal=False)["problems"]
    if problems:
        raise RuntimeError("refusing to seal a database that doesn't verify:\n" + "\n".join(problems[:10]))
    con = open_readonly(db)
    try:
        if key.public_hex != public_key(con):
            raise PermissionError("this key is not the owner key of this database")
        body = dict(owner_public_key=key.public_hex, sealed_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    heads=heads(con), digests=digests(con))
    finally:
        con.close()
    doc = dict(body, signature=key.sign(_seal_payload(body)))
    path = seal_path(db)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
    tmp.replace(path)
    return doc


def _check_seal(con, path: Path) -> tuple[list[str], list[str]]:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        body = {k: doc[k] for k in ("owner_public_key", "sealed_at", "heads", "digests")}
        sig = doc["signature"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return [f"seal unreadable ({type(exc).__name__}): the seal file was edited or damaged"], []
    if not (isinstance(body["heads"], dict) and isinstance(body["digests"], dict) and isinstance(sig, str)
            and isinstance(body["owner_public_key"], str) and isinstance(body["sealed_at"], str)):
        return ["seal fields have the wrong types (the seal file was edited)"], []
    problems = []
    if not verify_signature(body["owner_public_key"], _seal_payload(body), sig):
        problems.append("seal signature is invalid (the seal file was edited)")
    if public_key(con) != body["owner_public_key"]:
        problems.append("database owner key differs from the sealed one (the database was replaced)")
    if set(body["heads"]) != set(HISTORY):
        return problems + ["seal lists different tables than the schema (the seal file was edited)"], []
    for table in sorted(HISTORY):                        # names from the schema, never from the (editable) file
        head = body["heads"][table]
        if not (isinstance(head, dict) and isinstance(head.get("id"), int) and not isinstance(head.get("id"), bool)
                and isinstance(head.get("row_hash"), str)):
            problems.append(f"{table}: seal entry malformed")
            continue
        if head["id"] == 0:
            continue
        row = con.execute(f"SELECT row_hash FROM {table} WHERE id = ?", (head["id"],)).fetchone()
        if row is None:
            problems.append(f"{table}: sealed row {head['id']} is missing (rows were removed)")
        elif row[0] != head["row_hash"]:
            problems.append(f"{table}: sealed row {head['id']} was rewritten")
    now = digests(con)
    for name in sorted(now):
        if body["digests"].get(name) != now[name]:
            what = "the books' reference data" if name.startswith("reference:") else "the seed competitors/prices"
            problems.append(f"{name} differs from the seal: {what} changed since sealing "
                            "(after a legitimate build, review and re-seal)")
    counts = ", ".join(f"{t} {body['heads'][t]['id']}" for t in sorted(HISTORY) if isinstance(body["heads"][t], dict))
    notes = [f"sealed at {body['sealed_at']} with: {counts}. If that is older than your own record, the database "
             "and seal were rolled back together"]
    return problems, notes


def verify(db: Path, require_seal=True) -> dict:
    """{"problems": [...], "notes": [...], "fingerprint": str|None}. No problems = the database checks out."""
    db = Path(db)
    if not db.exists():
        return dict(problems=[f"{db} not found"], notes=[], fingerprint=None)
    con = open_readonly(db)
    try:
        problems = verify_structure(con)
        notes = []
        pub = public_key(con)
        for a in con.execute("SELECT * FROM approval ORDER BY id"):
            if not verify_signature(pub, approval_payload(dict(a)), a["signature_hex"]):
                problems.append(f"approval id {a['id']}: not signed by the owner's key")
        for change_id, why in change_validity(con).items():
            if why:
                problems.append(f"change {change_id}: {why} (ignored by every reader)")
        path = seal_path(db)
        if path.exists():
            p, n = _check_seal(con, path)
            problems += p
            notes += n
        elif require_seal and pub is not None:
            problems.append("no seal, but an owner exists: the seal was deleted, or run `cost-price seal`")
        elif require_seal:
            notes.append("not sealed yet: after `cost-price owner-init`, run `cost-price seal`")
        if pub is None:
            notes.append("no owner set up: run `cost-price owner-init`")
        return dict(problems=problems, notes=notes, fingerprint=fingerprint(pub) if pub else None)
    finally:
        con.close()
