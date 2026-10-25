"""Queries and appends used by the Market scout (role "scout") and CH (role "changes"). Values are always bound
parameters; table and column names only ever come from the schema.

Our prices: a CH price or availability change counts only if it is *valid*: its approval is signed by the owner's key,
says "approved", covers exactly this change (the signed suggestion names this dish and this price, or it is a revert
that restores exactly what the reverted change replaced), and the change doesn't take effect before the approval date.
Anything else in the change tables is ignored by every reader here and reported by `verify`."""
import hashlib
import ipaddress
import json
import re
from urllib.parse import urlsplit

from .db import append, now
from .owner import approval_payload, canonical, public_key, verify_signature

COMPARABLE_COURSES = ("starter", "main", "dessert", "drink")      # modifiers, bottles and event menus have no market twin
TWO_LEVEL_SUFFIXES = {"co.uk", "org.uk", "ac.uk", "com.au", "net.au", "co.nz", "co.jp", "com.br", "com.mx", "co.in"}
# hosted menu/ordering platforms: many restaurants share one domain, told apart by subdomain or first path segment
SHARED_HOSTS = {"toasttab.com", "square.site", "squareup.com", "clover.com", "popmenu.com", "getbento.com",
                "chownow.com", "menufy.com", "spoton.com", "wixsite.com", "squarespace.com", "godaddysites.com",
                "weebly.com", "owner.com", "order.online", "sites.google.com", "business.site", "linktr.ee"}


def domain_of(url: str) -> str:
    """The key one restaurant gets: its registrable domain (menu.x.com, order.x.com and www.x.com are all x.com),
    except on shared menu platforms, where it is host + first path segment (toasttab.com/a vs toasttab.com/b).
    IDNs are stored as punycode. Raises ValueError for URLs without a usable host."""
    parts = urlsplit(url)
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise ValueError(f"no host in {url!r}")
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise ValueError(f"host is not a valid domain name: {url!r}") from None
    try:
        return str(ipaddress.ip_address(host))
    except ValueError:
        pass
    labels = [label for label in host.split(".") if label]
    keep = 3 if ".".join(labels[-2:]) in TWO_LEVEL_SUFFIXES else 2
    registrable = ".".join(labels[-keep:])
    if any(host == s or host.endswith("." + s) for s in SHARED_HOSTS):
        first = re.sub(r"[^a-z0-9-]", "", (parts.path.strip("/").split("/") or [""])[0].lower())[:60]
        return host.removeprefix("www.") + (f"/{first}" if first else "")
    return registrable


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------- change validity
def planned_change(table: str, row: dict) -> dict:
    """The exact shape CH must sign as subject["planned_change"] for one menu change, and the shape readers compare
    the stored row with. Every field that decides what a reader shows is in it."""
    if table == "price_change":
        return dict(table=table, item=row["item"], channel=row["channel"], service=row["service"],
                    effective_from=row["effective_from"], price_cents=row["price_cents"])
    if table == "availability_change":
        return dict(table=table, item=row["item"], effective_from=row["effective_from"], available=row["available"])
    raise ValueError(f"not a menu change table: {table}")


def _children(con) -> dict[str, list[tuple[str, dict]]]:
    kids = {}
    for table in ("price_change", "availability_change"):
        for r in con.execute(f"SELECT * FROM {table} ORDER BY id"):
            kids.setdefault(r["change_id"], []).append((table, dict(r)))
    return kids


def change_validity(con) -> dict[str, str | None]:
    """{change_id: None if valid, else why not} for every change_log row, plus "child:<change_id>" entries for price
    or availability rows that hang off a change that isn't a menu change (always invalid)."""
    pub = public_key(con)
    approvals = {r["id"]: dict(r) for r in con.execute("SELECT * FROM approval")}
    approval_ok = {}
    for aid, a in approvals.items():
        if not verify_signature(pub, approval_payload(a), a["signature_hex"]):
            approval_ok[aid] = "approval is not signed by the owner's key"
        elif sha256_hex(a["subject_json"]) != a["subject_sha256"]:
            approval_ok[aid] = "approval's subject doesn't match its hash"
        else:
            approval_ok[aid] = None
    started = {(r["suggestion_id"], r["bill_id"], r["approval_id"], r["to_date"])
               for r in con.execute("SELECT * FROM nessie_intent")}
    kids, out, rows, used = _children(con), {}, {}, set()
    for c in con.execute("SELECT * FROM change_log ORDER BY id"):
        c = dict(c)
        rows[c["change_id"]] = c
        a = approvals.get(c["approval_id"])
        why = approval_ok.get(c["approval_id"], "no such approval")
        if why is None and a["suggestion_id"] != c["suggestion_id"]:
            why = "approval is for another suggestion"
        if why is None and a["decision"] != "approved":
            begun = (c["suggestion_id"], c["ref"], c["approval_id"], c["new_value"]) in started
            if not (c["system"] == "nessie" and begun):          # a bank move begun before the owner revoked
                why = "approval says rejected"
        if c["system"] == "menu_prices":
            if why is None and c["approval_id"] in used:
                why = "this approval was already used by an earlier menu change"
            used.add(c["approval_id"])
            children = kids.get(c["change_id"], [])
            if why is None and len(children) != 1:
                why = "a menu change needs exactly one price or availability row"
            if why is None:
                why = _covers(json.loads(a["subject_json"]), c, children[0], rows, out, a["decided_at"][:10])
        out[c["change_id"]] = why
    for cid in kids:
        if rows.get(cid, {}).get("system") != "menu_prices":
            out[f"child:{cid}"] = "a price/availability row cites a change that isn't a menu change"
    return out


def _covers(subject: dict, c: dict, child: tuple[str, dict], rows: dict, verdicts: dict, decided: str) -> str | None:
    """Does the signed subject authorise exactly this change and exactly this stored row?"""
    table, row = child
    if subject.get("planned_change") != planned_change(table, row):
        return "the stored row doesn't match the planned change the owner signed"
    field, new = (("price_cents", str(row["price_cents"])) if table == "price_change"
                  else ("on_menu", "yes" if row["available"] else "no"))
    if (c["field"], c["ref"], c["new_value"]) != (field, row["item"], new):
        return "the change log doesn't match its price/availability row"
    if row["effective_from"] < decided:
        return "takes effect before the owner approved it"
    if "revert" in subject:
        if set(subject) != {"revert", "planned_change"}:
            return "malformed revert subject"
        target = rows.get(subject["revert"])
        if target is None or verdicts.get(subject["revert"]) is not None or target["system"] != "menu_prices":
            return "reverts a change that doesn't exist or isn't a valid menu change"
        if (c["ref"], c["field"], c["new_value"], c["reverts"]) != (target["ref"], target["field"], target["old_value"],
                                                                      target["change_id"]):
            return "revert doesn't restore exactly what the reverted change replaced"
        return None
    action, target, value = subject.get("action"), subject.get("target"), subject.get("new_price_cents")
    if table == "price_change" and action == "reprice_item" and target == row["item"] and value == row["price_cents"]:
        return None
    if table == "availability_change" and action == "remove_item" and target == row["item"] and row["available"] == 0:
        return None
    return "the signed suggestion doesn't cover this change"


def _valid_changes(con) -> set[str]:
    """change_ids of the menu changes every reader may honour."""
    validity = change_validity(con)
    menu_ids = {r[0] for r in con.execute("SELECT change_id FROM change_log WHERE system = 'menu_prices'")}
    return {cid for cid in menu_ids if validity.get(cid, "unknown") is None}


# ---------------------------------------------------------------- our side
def restaurant(con) -> dict:
    row = con.execute("SELECT * FROM restaurant ORDER BY location LIMIT 1").fetchone()
    if row is None:
        raise LookupError("no restaurant: run `cost-price build`")
    return dict(row)


def _timeline(con, item, channel, service, on, valid: set[str]):
    for r in con.execute("""SELECT price_cents, effective_from, source, change_id FROM menu_price_timeline
                            WHERE item = ? AND channel = ? AND service = ? AND effective_from <= ?
                            ORDER BY effective_from DESC, rank DESC, seq DESC""", (item, channel, service, on)):
        if r["source"] == "books" or r["change_id"] in valid:
            return r
    return None


def effective_price(con, item, on: str, channel="dine_in", service="dinner", valid=None) -> int | None:
    """Price in force on a date. The latest effective_from <= on wins; on the same date a valid approved change
    beats the books (it was made later), and a later books reprice beats an earlier change. Invalid changes are
    ignored."""
    r = _timeline(con, item, channel, service, on, _valid_changes(con) if valid is None else valid)
    return r["price_cents"] if r else None


def available(con, item, on: str, valid=None) -> bool:
    valid = _valid_changes(con) if valid is None else valid
    for r in con.execute("""SELECT available, change_id FROM availability_change
                            WHERE item = ? AND effective_from <= ? ORDER BY effective_from DESC, id DESC""", (item, on)):
        if r["change_id"] in valid:
            return bool(r["available"])
    return True


def menu(con, on: str, channel="dine_in", service="dinner", courses=COMPARABLE_COURSES, items=()):
    """-> ({item: price_cents} in force on `on`, the latest effective_from among them). Removed dishes are left out;
    unknown requested items raise."""
    valid = _valid_changes(con)
    marks = ",".join("?" * len(courses))
    names = [r[0] for r in con.execute(f"""SELECT DISTINCT item FROM menu_price_base WHERE channel = ? AND service = ?
                                           AND course IN ({marks}) ORDER BY item""", (channel, service, *courses))]
    out, latest = {}, None
    for item in names:
        if not available(con, item, on, valid):
            continue
        r = _timeline(con, item, channel, service, on, valid)
        if r:
            out[item] = r["price_cents"]
            latest = max(latest or r["effective_from"], r["effective_from"])
    if items:
        unknown = sorted(set(items) - set(out))
        if unknown:
            raise ValueError(f"not on our {service} menu: {unknown}; choose from {sorted(out)}")
        out = {i: out[i] for i in items}
    return out, latest


def plate_cost(con, item, month=None) -> int | None:
    sql = "SELECT plate_cost_cents FROM plate_cost_month WHERE item = ?" + (" AND month <= ?" if month else "")
    row = con.execute(sql + " ORDER BY month DESC LIMIT 1", (item, month) if month else (item,)).fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- competitors (scout)
def competitors(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM competitor ORDER BY id")]


def observations(con, competitor_id=None, reader=None) -> list[dict]:
    sql, args = "SELECT * FROM observation_status WHERE 1=1", []
    if competitor_id is not None:
        sql, args = sql + " AND competitor_id = ?", args + [competitor_id]
    if reader is not None:
        sql, args = sql + " AND reader = ?", args + [reader]
    return [dict(r) for r in con.execute(sql + " ORDER BY id", args)]


def add_competitor(con, *, name, menu_url, found_via, address=None, neighborhood=None, cuisine=None, price_level=None,
                   distance_miles=None, notes=None, created_at=None) -> int:
    """Keyed by domain_of(menu_url): one restaurant can't vote twice under two names or two subdomains."""
    return append(con, "competitor", dict(domain=domain_of(menu_url), name=name, address=address,
                                          neighborhood=neighborhood, cuisine=cuisine, price_level=price_level,
                                          distance_miles=distance_miles, menu_url=menu_url, found_via=found_via,
                                          notes=notes, created_at=created_at or now()), unique=("domain",))


OBS_KEY = ("competitor_id", "our_item", "price_cents", "quote", "source_url", "reader")


def add_observation(con, *, competitor_id, our_item, their_item, price_cents, service, source_url, source_type, quote,
                    match_confidence, reader, retrieved_at, report_id=None, created_at=None) -> int:
    return append(con, "competitor_observation", dict(
        competitor_id=competitor_id, our_item=our_item, their_item=their_item, price_cents=price_cents,
        service=service, source_url=source_url, source_type=source_type, quote=quote,
        match_confidence=match_confidence, reader=reader, retrieved_at=retrieved_at, report_id=report_id,
        created_at=created_at or now()), unique=OBS_KEY)


def add_check(con, observation_id, status, reason=None, snapshot_sha256=None, report_id=None) -> int:
    return append(con, "observation_check", dict(observation_id=observation_id, status=status, reason=reason,
                                                 snapshot_sha256=snapshot_sha256, report_id=report_id, checked_at=now()))


def add_market_summary(con, *, report_id, our_item, service, menu_as_of, our_price_cents, competitor_median_cents,
                       price_gap_cents, n_prices, position) -> int:
    """menu_as_of: the date whose menu our_price_cents was read from (the scan's as_of)."""
    return append(con, "market_summary", dict(report_id=report_id, our_item=our_item, service=service,
                                              menu_as_of=str(menu_as_of), our_price_cents=our_price_cents,
                                              competitor_median_cents=competitor_median_cents,
                                              price_gap_cents=price_gap_cents, n_prices=n_prices, position=position,
                                              created_at=now()), unique=("report_id", "our_item", "service"))


def latest_market(con, service="dinner", on: str | None = None) -> list[dict]:
    """Per dish, the newest scan that compared the menu in force on `on` (default: today on this machine's clock, the
    same default the scout uses; not UTC, which in San Francisco is already tomorrow from 5 pm). A scan of an older or
    a future menu (a request with a different as_of) never stands in for the current comparison."""
    from datetime import date
    on = on or date.today().isoformat()
    return [dict(r) for r in con.execute("""SELECT * FROM market_summary m WHERE service = ? AND menu_as_of = ? AND id =
                                            (SELECT max(id) FROM market_summary x WHERE x.our_item = m.our_item
                                             AND x.service = m.service AND x.menu_as_of = m.menu_as_of)
                                            ORDER BY our_item""", (service, on))]


# ---------------------------------------------------------------- approvals and changes (CH)
def record_approval(con, key, *, suggestion_id, subject: dict, decision, decided_by, decided_at, note=None) -> int:
    """Sign and store the owner's decision on `subject` (the suggestion exactly as shown, as a JSON-able dict)."""
    owner = public_key(con)
    if owner is None or key.public_hex != owner:
        raise PermissionError("this key is not the owner key of this database")
    subject_json = canonical(subject)
    values = dict(suggestion_id=suggestion_id, subject_json=subject_json, subject_sha256=sha256_hex(subject_json),
                  decision=decision, decided_by=decided_by, decided_at=decided_at, note=note, created_at=now())
    return append(con, "approval", values, finalize=lambda row: {"signature_hex": key.sign(approval_payload(row))})


def current_approval(con, suggestion_id, subject: dict) -> tuple[int | None, str | None]:
    """(approval id, None) if the owner's latest decision on this suggestion is a genuine, signed approval of exactly
    `subject`; else (None, why not). Needs no passphrase: signatures are checked with the stored public key."""
    row = con.execute("SELECT * FROM approval WHERE suggestion_id = ? ORDER BY id DESC LIMIT 1",
                      (suggestion_id,)).fetchone()
    if row is None:
        return None, "no recorded owner approval; run `changes approve` first"
    row = dict(row)
    if not verify_signature(public_key(con), approval_payload(row), row["signature_hex"]):
        return None, "the latest approval is not signed by the owner's key (forged or tampered)"
    if row["decision"] != "approved":
        return None, f"owner's latest decision was '{row['decision']}'"
    if row["subject_json"] != canonical(subject) or row["subject_sha256"] != sha256_hex(canonical(subject)):
        return None, "suggestion changed after the owner approved it"
    return row["id"], None


def latest_approval(con, suggestion_id) -> dict | None:
    row = con.execute("SELECT * FROM approval WHERE suggestion_id = ? ORDER BY id DESC LIMIT 1",
                      (suggestion_id,)).fetchone()
    return dict(row) if row else None


def add_intent(con, *, suggestion_id, approval_id, bill_id, from_date, to_date) -> int:
    """Record, before calling the bank, exactly which date a bill is being moved to. A retry uses this absolute
    target instead of recomputing it from a (possibly already moved) resynced date."""
    return append(con, "nessie_intent", dict(suggestion_id=suggestion_id, approval_id=approval_id, bill_id=bill_id,
                                             from_date=from_date, to_date=to_date, at=now()),
                  unique=("suggestion_id", "bill_id"))


def intents(con, suggestion_id) -> dict[str, dict]:
    return {r["bill_id"]: dict(r) for r in con.execute("SELECT * FROM nessie_intent WHERE suggestion_id = ?",
                                                        (suggestion_id,))}


def log_change(con, *, change_id, suggestion_id, approval_id, system, ref, field, old_value, new_value, reverts=None,
               at=None) -> int:
    return append(con, "change_log", dict(change_id=change_id, suggestion_id=suggestion_id, approval_id=approval_id,
                                          at=at or now(), system=system, ref=ref, field=field,
                                          old_value=None if old_value is None else str(old_value),
                                          new_value=None if new_value is None else str(new_value), reverts=reverts),
                  unique=("change_id",))


def add_price_change(con, *, change_id, item, effective_from, price_cents, channel="dine_in", service="dinner") -> int:
    return append(con, "price_change", dict(change_id=change_id, item=item, channel=channel, service=service,
                                            effective_from=effective_from, price_cents=price_cents),
                  unique=("change_id",))


def add_availability_change(con, *, change_id, item, effective_from, available) -> int:
    return append(con, "availability_change", dict(change_id=change_id, item=item, effective_from=effective_from,
                                                   available=1 if available else 0), unique=("change_id",))


def add_dispute(con, *, change_id, bank_id, state, note=None, opened_at=None) -> int:
    return append(con, "dispute", dict(change_id=change_id, bank_id=bank_id, opened_at=opened_at or now(), state=state,
                                       note=note), unique=("change_id",))


def mark_applied(con, *, suggestion_id, action, target, decided_by, result_json) -> int:
    return append(con, "applied", dict(suggestion_id=suggestion_id, applied_at=now(), action=action, target=target,
                                       decided_by=decided_by, result_json=result_json), unique=("suggestion_id",))


def applied_result(con, suggestion_id) -> str | None:
    row = con.execute("SELECT result_json FROM applied WHERE suggestion_id = ?", (suggestion_id,)).fetchone()
    return row[0] if row else None


def change(con, change_id) -> dict | None:
    row = con.execute("SELECT * FROM change_log WHERE change_id = ?", (change_id,)).fetchone()
    return dict(row) if row else None


def change_log(con) -> list[dict]:
    return [dict(r) for r in con.execute("SELECT * FROM change_log ORDER BY id")]


def logged_refs(con, suggestion_id, system) -> set[str]:
    return {r[0] for r in con.execute("SELECT ref FROM change_log WHERE suggestion_id = ? AND system = ?",
                                      (suggestion_id, system))}


def upcoming_changes(con, on: str) -> list[dict]:
    """Valid approved menu changes that take effect after `on`: what the menu will be, so a scan run today can say
    'approved: $19.50 from 2026-10-25' instead of silently comparing the old price."""
    valid = _valid_changes(con)
    out = [dict(kind="price", item=r["item"], channel=r["channel"], service=r["service"], effective_from=r["effective_from"],
                price_cents=r["price_cents"], change_id=r["change_id"])
           for r in con.execute("SELECT * FROM price_change WHERE effective_from > ? ORDER BY effective_from, id", (on,))
           if r["change_id"] in valid]
    out += [dict(kind="availability", item=r["item"], effective_from=r["effective_from"], available=bool(r["available"]),
                 change_id=r["change_id"])
            for r in con.execute("SELECT * FROM availability_change WHERE effective_from > ? ORDER BY effective_from, id",
                                 (on,))
            if r["change_id"] in valid]
    return sorted(out, key=lambda x: (x["effective_from"], x["item"]))
