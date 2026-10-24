"""Pull everything for a manifest back from Nessie into data/nessie_sync_<tag>.db and verify the round trip.

  python nessie_sync.py --tag sample1 [--days 5]
"""
import argparse, json, re, sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

import pandas as pd

import config as C
from nessie_client import call

D = Path(__file__).parent / "data"
REF = re.compile(r"\s*\|ref=(\w+)$")


def pull(manifest):
    acc = manifest["accounts"]
    kinds = {"deposits": "deposit", "withdrawals": "withdrawal", "purchases": "purchase"}
    jobs = [(a, k) for a in acc for k in list(kinds) + ["bills"]]
    with ThreadPoolExecutor(8) as ex:
        res = list(ex.map(lambda j: (j, call("GET", f"/accounts/{acc[j[0]]}/{j[1]}")), jobs))
    merchant_name = {v: k for k, v in manifest["merchants"].items()}
    rows, bills = [], []
    for (a, k), data in res:
        for x in data if isinstance(data, list) else []:
            if k == "bills":
                bills.append(dict(account=a, **x))
                continue
            desc = x.get("description") or ""
            m = REF.search(desc)
            rows.append(dict(nessie_id=x.get("_id") or x.get("id"), account=a, nessie_type=kinds[k],
                             date=x.get("purchase_date") or x.get("transaction_date"), amount=x.get("amount"),
                             counterparty=merchant_name.get(x.get("merchant_id")), memo=REF.sub("", desc),
                             bank_id=m.group(1) if m else None, status=x.get("status")))
    return pd.DataFrame(rows), pd.DataFrame(bills)


def verify(got, days=None):
    want = pd.read_csv(D / "bank_feed.csv")
    if days:
        want = want[want.date <= str(date.fromordinal(C.START.toordinal() + days - 1))]
    m = want.merge(got, on="bank_id", how="outer", suffixes=("", "_n"), indicator=True)
    both = m[m._merge == "both"]
    checks = {
        "rows expected": len(want), "rows in Nessie": len(got),
        "missing from Nessie": int((m._merge == "left_only").sum()), "extra in Nessie": int((m._merge == "right_only").sum()),
        "duplicate refs in Nessie": int(got.bank_id.duplicated().sum()),
        "amount mismatches": int((both.amount != both.amount_n).sum()), "date mismatches": int((both.date != both.date_n).sum()),
        "type mismatches": int((both.nessie_type != both.nessie_type_n).sum()), "memo mismatches": int((both.memo != both.memo_n).sum()),
        "account mismatches": int((both.account != both.account_n).sum()),
    }
    for k, v in checks.items():
        print(f"  {k:26} {v}")
    return all(v == 0 for k, v in checks.items() if "mismatch" in k or k.startswith(("missing", "extra", "duplicate")))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", default="dev")
    ap.add_argument("--days", type=int)
    ap.add_argument("--repair", action="store_true", help="delete duplicate |ref= rows (keeps the first)")
    a = ap.parse_args()
    manifest = json.loads((D / f"nessie_manifest_{a.tag}.json").read_text())
    tx, bills = pull(manifest)
    if a.repair and tx.bank_id.duplicated().any():
        path = {"deposit": "deposits", "withdrawal": "withdrawal", "purchase": "purchase"}
        extra = tx[tx.bank_id.notna() & tx.bank_id.duplicated()]
        for r in extra.itertuples():
            call("DELETE", f"/{path[r.nessie_type]}/{r.nessie_id}")
        print(f"repair: deleted {len(extra)} duplicate rows")
        tx, bills = pull(manifest)
    con = sqlite3.connect(D / f"nessie_sync_{a.tag}.db")
    tx.to_sql("bank_tx", con, if_exists="replace", index=False)
    bills.astype(str).to_sql("bank_bills", con, if_exists="replace", index=False)
    con.close()
    print(f"pulled {len(tx)} transactions, {len(bills)} bills")
    print("round trip OK" if verify(tx, a.days) else "ROUND TRIP FAILED")
