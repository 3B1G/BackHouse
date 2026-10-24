"""Load the SIMULATED bank feed into Nessie. Safe to re-run: a manifest records every created id.

  python nessie_load.py dry-run                 # build + validate every payload, no network
  python nessie_load.py sample --days 5 --tag sample1
  python nessie_load.py full --tag boh1

Amounts are integer cents everywhere (account balance, transactions, bills, loan).
Account balance = opening balance as of config.START; Nessie never updates it, so agents derive balances.
"""
import argparse, json, os, re, sqlite3, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

import pandas as pd

import config as C

D = Path(__file__).parent / "data"
SIM_ADDR = {"street_number": "0", "street_name": "Demo St (SIMULATED)", "city": "San Francisco", "state": "CA", "zip": C.GEO["zip"]}


def next_due(day):
    """Next calendar date with this day-of-month after END. Nessie requires payment_date on every bill:
    a bill stored without payment_date or recurring_date makes GET /accounts/{id}/bills fail with 400
    for the whole account. Nessie computes upcoming_payment_date itself and rejects it on POST."""
    d = pd.Timestamp(C.END) + pd.Timedelta(days=1)
    while d.day != min(day, d.days_in_month):
        d += pd.Timedelta(days=1)
    return str(d.date())


def build(days=None):
    bank = pd.read_csv(D / "bank_feed.csv")
    if days:
        cutoff = str(date.fromordinal(C.START.toordinal() + days - 1))
        bank = bank[bank.date <= cutoff]
    con = sqlite3.connect(D / "books.db")
    sup = pd.read_sql("select supplier name, category from suppliers", con)
    vb = pd.read_sql("select distinct vendor name, category from vendor_bills", con)
    other = pd.DataFrame([(v["landlord"], "Occupancy") for v in C.LEASES.values()] + [(C.REPAIRS["merchant"], "Repairs & Maintenance")]
                         + [(p[3], "G&A") for p in C.PERMITS], columns=["name", "category"])
    cats = pd.concat([sup, vb, other]).drop_duplicates("name").set_index("name").category.to_dict()
    open_ap = pd.read_sql("select * from open_ap", con)
    vbills = pd.read_sql("select * from vendor_bills", con)
    con.close()

    accounts = {a: {"type": t, "nickname": f"{n} | cents | opening {C.START}", "rewards": 0, "balance": C.OPENING_BALANCE[a]}
                for a, (t, n, _) in C.ACCOUNTS.items()}
    merchants = {n: {"name": f"{n} [SIM]", "category": cats.get(n, "Other"), "address": SIM_ADDR}
                 for n in sorted(bank[bank.nessie_type == "purchase"].counterparty.dropna().unique())}
    tx = []
    for r in bank.itertuples():
        desc = f"{r.memo} |ref={r.bank_id}"
        if r.nessie_type == "purchase":
            body = {"merchant_id": None, "medium": "balance", "amount": int(r.amount), "status": "completed",
                    "purchase_date": r.date, "description": desc}
        else:
            body = {"medium": "balance", "transaction_date": r.date, "status": "completed", "amount": int(r.amount), "description": desc}
        tx.append(dict(bank_id=r.bank_id, account=r.account, type=r.nessie_type, merchant=getattr(r, "counterparty"), body=body))
    bills = [dict(ref=f"AP:{r.doc_id}", account=r.location,
                  body={"status": "pending", "payee": f"{r.payee} [SIM]", "nickname": f"{r.doc_id} due {r.due_date}", "payment_date": r.due_date, "recurring_date": int(r.due_date[8:10]),
                        "payment_amount": int(r.total)}) for r in open_ap.itertuples()]
    last = vbills.sort_values("due_date").groupby(["vendor", "location"]).tail(1)
    for r in last.itertuples():
        bills.append(dict(ref=f"REC:{r.vendor}:{r.location}", account=r.location,
                          body={"status": "recurring", "payee": f"{r.vendor.replace(' (SIM)', '')} [SIM]", "nickname": f"{r.vendor.replace(' (SIM)', '')} autopay",
                                "recurring_date": int(pd.Timestamp(r.due_date).day),
                                "payment_date": str((pd.Timestamp(r.due_date) + pd.DateOffset(months=1)).date()),   # after its latest bill
                                "payment_amount": int(r.amount)}))
    for loc, L in C.LEASES.items():
        bills.append(dict(ref=f"REC:rent:{loc}", account=loc, body={"status": "recurring", "payee": f"{L['landlord']} [SIM]",
                          "nickname": "rent incl NNN", "recurring_date": 1, "payment_date": next_due(1),
                          "payment_amount": int(L["base"] * (1 + L["escalator_pct"]) if date.fromisoformat(next_due(1)) >= L["anniversary"] else L["base"])}))
    loan = {"type": C.LOAN["type"], "status": "approved", "credit_score": C.LOAN["credit_score"], "monthly_payment": C.LOAN["monthly"],
            "amount": C.LOAN["balance_at_start"], "description": f"{C.LOAN['lender']} | balance as of {C.START} | cents"}
    return accounts, merchants, tx, bills, loan


def validate(accounts, merchants, tx, bills, loan):
    errs = []
    for t in tx:
        b = t["body"]
        if not isinstance(b["amount"], int) or b["amount"] <= 0:
            errs.append(f"{t['bank_id']}: amount {b['amount']!r}")
        if t["type"] == "purchase" and t["merchant"] not in merchants:
            errs.append(f"{t['bank_id']}: unknown merchant {t['merchant']!r}")
        if t["account"] not in accounts:
            errs.append(f"{t['bank_id']}: unknown account {t['account']}")
    for b in bills:
        if not b["body"].get("payment_date") or not b["body"].get("recurring_date"):
            errs.append(f"{b['ref']}: bills need payment_date AND recurring_date, or GET bills returns 400")
        if b["body"]["status"] not in ("pending", "cancelled", "completed", "recurring"):
            errs.append(f"{b['ref']}: bad status")
    return errs


class Manifest:
    def __init__(self, tag):
        self.path = D / f"nessie_manifest_{tag}.json"
        self.m = json.loads(self.path.read_text()) if self.path.exists() else {"tag": tag, "accounts": {}, "merchants": {}, "tx": {}, "bills": {}}
        self.lock = threading.Lock()

    def put(self, section, k, v):
        with self.lock:
            (self.m.setdefault(section, {}) if isinstance(self.m.get(section, {}), dict) else self.m)[k] = v

    def save(self):
        with self.lock:                               # write-then-rename: a crash never leaves a half-written manifest
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.m, indent=1))
            for attempt in range(10):                 # Windows: antivirus/indexer can lock the file for a moment
                try:
                    os.replace(tmp, self.path)
                    return
                except PermissionError:
                    time.sleep(0.1 * (attempt + 1))
            print(f"  warning: could not save {self.path.name} this time; the next save or run will catch up")


def adopt_existing(M, merchants, bills):
    """Rebuild 'already loaded' from what Nessie holds, so a POST that succeeded but wasn't recorded is never resent.
    Transactions match by their |ref= tag, bills by nickname, merchants by name, the loan by description."""
    from nessie_client import call
    names = {b["name"]: n for n, b in merchants.items()}
    for x in call("GET", "/merchants") or []:
        if x.get("name") in names and names[x["name"]] not in M.m["merchants"]:
            M.put("merchants", names[x["name"]], x["_id"])
    nick = {b["body"]["nickname"]: b["ref"] for b in bills}
    for loc, aid in M.m["accounts"].items():
        for kind in ("deposits", "withdrawals", "purchases"):
            for x in call("GET", f"/accounts/{aid}/{kind}") or []:
                m = re.search(r"\|ref=(\w+)$", x.get("description") or "")
                if m and m.group(1) not in M.m["tx"]:
                    M.put("tx", m.group(1), x.get("_id") or x.get("id"))
        for x in call("GET", f"/accounts/{aid}/bills") or []:
            if x.get("nickname") in nick and nick[x["nickname"]] not in M.m["bills"]:
                M.put("bills", nick[x["nickname"]], x["_id"])
        if loc == C.LOAN["account"] and "loan" not in M.m:
            for x in call("GET", f"/accounts/{aid}/loans") or []:
                if (x.get("description") or "").startswith(C.LOAN["lender"]):
                    M.m["loan"] = x["_id"]
    M.save()


def load(tag, days=None, workers=8):
    from nessie_client import call, created_id
    accounts, merchants, tx, bills, loan = build(days)
    errs = validate(accounts, merchants, tx, bills, loan)
    if errs:
        raise SystemExit("validation failed:\n" + "\n".join(errs[:20]))
    M = Manifest(tag)
    if "customer" not in M.m:
        mine = [c for c in call("GET", "/customers") or [] if c.get("last_name") == f"Group [SIM {tag}]"]
        if mine:                                      # created on a run that crashed before saving the manifest
            M.m["customer"] = mine[0]["_id"]
            for a in call("GET", f"/customers/{mine[0]['_id']}/accounts") or []:
                for k, (t, n, _) in C.ACCOUNTS.items():
                    if a.get("nickname", "").startswith(n):
                        M.put("accounts", k, a["_id"])
            M.save()
    if "customer" not in M.m:
        M.m["customer"] = created_id(call("POST", "/customers", {"first_name": "Demo Bistro", "last_name": f"Group [SIM {tag}]", "address": SIM_ADDR}))
        M.save()
    cid = M.m["customer"]
    for a, body in accounts.items():
        if a not in M.m["accounts"]:
            M.put("accounts", a, created_id(call("POST", f"/customers/{cid}/accounts", body))); M.save()
    adopt_existing(M, merchants, bills)
    for n, body in merchants.items():
        if n not in M.m["merchants"]:
            M.put("merchants", n, created_id(call("POST", "/merchants", body))); M.save()
    adopt_existing(M, merchants, bills)
    acc, mer = M.m["accounts"], M.m["merchants"]
    if "loan" not in M.m:
        M.m["loan"] = created_id(call("POST", f"/accounts/{acc[C.LOAN['account']]}/loans", loan)); M.save()

    path = {"deposit": "deposits", "withdrawal": "withdrawals", "purchase": "purchases"}
    todo = [t for t in tx if t["bank_id"] not in M.m["tx"]]
    jobs = [(f"/accounts/{acc[t['account']]}/{path[t['type']]}", dict(t["body"], **({"merchant_id": mer[t["merchant"]]} if t["type"] == "purchase" else {})),
             "tx", t["bank_id"]) for t in todo]
    jobs += [(f"/accounts/{acc[b['account']]}/bills", b["body"], "bills", b["ref"]) for b in bills if b["ref"] not in M.m["bills"]]
    t0, done, failed = time.time(), 0, []

    def run(job):
        p, body, section, ref = job
        return section, ref, created_id(call("POST", p, body))

    with ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(run, j): j for j in jobs}
        for f in as_completed(futs):
            try:
                section, ref, nid = f.result()
                M.put(section, ref, nid)
                M.save()                                 # after every create: a crash never loses track of a write
                done += 1
                if done % 200 == 0:
                    print(f"  {done}/{len(jobs)} ({time.time() - t0:.0f}s)")
            except Exception as e:                      # keep going; failures are retried on the next run
                failed.append((futs[f][3], str(e)[:200]))
    M.save()
    print(f"loaded {done}/{len(jobs)} in {time.time() - t0:.0f}s; failed {len(failed)}; manifest {M.path.name}")
    for ref, e in failed[:10]:
        print("  FAIL", ref, e)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["dry-run", "sample", "full"])
    ap.add_argument("--days", type=int, default=5)
    ap.add_argument("--tag", default="dev")
    a = ap.parse_args()
    if a.mode == "dry-run":
        accounts, merchants, tx, bills, loan = build()
        errs = validate(accounts, merchants, tx, bills, loan)
        n = 1 + len(accounts) + len(merchants) + len(tx) + len(bills) + 1
        print(f"payloads: customer 1, accounts {len(accounts)}, merchants {len(merchants)}, transactions {len(tx)} "
              f"{pd.Series([t['type'] for t in tx]).value_counts().to_dict()}, bills {len(bills)}, loan 1")
        print(f"total calls {n}; est. {n * 0.85 / 60:.0f} min sequential, {n * 0.85 / 8 / 60:.0f} min at 8 workers; validation errors: {len(errs)}")
        print("example purchase:", json.dumps(next(t for t in tx if t["type"] == "purchase")["body"]))
        print("example bill:", json.dumps(bills[0]["body"]))
        for e in errs[:10]:
            print("  ERR", e)
    else:
        load(a.tag, days=a.days if a.mode == "sample" else None)
