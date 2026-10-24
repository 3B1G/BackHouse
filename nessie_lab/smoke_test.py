"""Nessie smoke test: answers the open questions before we design the loader.

1. What does a create response look like?
2. Do purchases / deposits / withdrawals change account balance (and when: pending vs completed)?
3. Can we PUT a bill's payment_date (the Controller's "stretch payment" action)?
4. How do transfers work (payee field)?
5. How fast is the API (to size the seed job)?
"""
import json, time
from pathlib import Path
import requests

BASE = "https://prod-api.nessieisreal.com"
ENV = Path(__file__).resolve().parent.parent / ".env"
KEY = next(l.split("=", 1)[1].strip().strip("\"'") for l in ENV.read_text().splitlines()
           if l.startswith("NESSIE_API_KEY="))
LOG, TIMES = [], []


def call(method, path, body=None):
    t = time.perf_counter()
    r = requests.request(method, BASE + path, params={"key": KEY}, json=body, timeout=30)
    TIMES.append(time.perf_counter() - t)
    try:
        data = r.json()
    except ValueError:
        data = r.text
    LOG.append({"req": f"{method} {path}", "body": body, "status": r.status_code, "resp": data})
    print(f"{method:6} {path:45} {r.status_code}  {json.dumps(data)[:150]}")
    return data


def balance(acct):
    return call("GET", f"/accounts/{acct}")["balance"]


cust = call("POST", "/customers", {"first_name": "SMOKE", "last_name": "TEST",
            "address": {"street_number": "1", "street_name": "Test St", "city": "San Francisco",
                        "state": "CA", "zip": "94123"}})
cid = cust["objectCreated"]["_id"]
a1 = call("POST", f"/customers/{cid}/accounts", {"type": "Checking", "nickname": "smoke A",
          "rewards": 0, "balance": 100000})["objectCreated"]["_id"]
a2 = call("POST", f"/customers/{cid}/accounts", {"type": "Savings", "nickname": "smoke B",
          "rewards": 0, "balance": 0})["objectCreated"]["_id"]
mid = call("POST", "/merchants", {"name": "SMOKE Produce", "category": "Food Supply",
           "address": {"street_number": "1", "street_name": "Test St", "city": "San Francisco",
                       "state": "CA", "zip": "94123"},
           "geocode": {"lat": 37.80, "lng": -122.44}})["objectCreated"]["_id"]

results = {"start": balance(a1)}
p1 = call("POST", f"/accounts/{a1}/purchases", {"merchant_id": mid, "medium": "balance", "amount": 1234,
          "status": "completed", "purchase_date": "2026-10-01", "description": "completed purchase"})
results["after_completed_purchase"] = balance(a1)
p2 = call("POST", f"/accounts/{a1}/purchases", {"merchant_id": mid, "medium": "balance", "amount": 1000,
          "status": "pending", "purchase_date": "2026-10-02", "description": "pending purchase"})
results["after_pending_purchase"] = balance(a1)
d1 = call("POST", f"/accounts/{a1}/deposits", {"medium": "balance", "transaction_date": "2026-10-03",
          "status": "completed", "amount": 5000, "description": "completed deposit"})
results["after_completed_deposit"] = balance(a1)
d2 = call("POST", f"/accounts/{a1}/deposits", {"medium": "balance", "transaction_date": "2026-10-04",
          "status": "pending", "amount": 300, "description": "pending deposit"})
results["after_pending_deposit"] = balance(a1)
w1 = call("POST", f"/accounts/{a1}/withdrawals", {"medium": "balance", "amount": 700,
          "transaction_date": "2026-10-05", "status": "completed", "description": "payroll"})
results["after_withdrawal"] = balance(a1)

# Does updating a pending purchase to completed move the balance?
pid2 = p2.get("objectCreated", {}).get("_id")
if pid2:
    call("PUT", f"/purchase/{pid2}", {"status": "completed"})
    results["after_pending_to_completed"] = balance(a1)

# Bills: create, then the Controller's "stretch 15 days" action
b = call("POST", f"/accounts/{a1}/bills", {"status": "recurring", "payee": "SMOKE Landlord",
         "nickname": "rent", "payment_date": "2026-11-01", "recurring_date": 1, "payment_amount": 950.50})
bid = b.get("objectCreated", {}).get("_id")
if bid:
    call("PUT", f"/bills/{bid}", {"payment_date": "2026-11-16"})
    call("GET", f"/bills/{bid}")
results["after_bill"] = balance(a1)

# Transfers: required-only, then with payee variants
for extra in ({}, {"payee_id": a2}, {"payee": a2}, {"medium": "balance"}):
    call("POST", f"/accounts/{a1}/transfers", {"transaction_date": "2026-10-06", "status": "completed",
         "amount": 100, "description": "transfer test", **extra})
results["a1_after_transfers"] = balance(a1)
results["a2_after_transfers"] = balance(a2)

# Read-back views the agents will use
for path in (f"/accounts/{a1}/purchases", f"/merchants/{mid}/purchases", f"/accounts/{a1}/deposits",
             f"/accounts/{a1}/withdrawals", f"/accounts/{a1}/bills", f"/accounts/{a1}/transfers",
             f"/customers/{cid}/bills"):
    call("GET", path)

# Cleanup (customers and merchants have no DELETE)
for acct in (a1, a2):
    call("DELETE", f"/accounts/{acct}")

results["timing_s"] = {"calls": len(TIMES), "mean": round(sum(TIMES) / len(TIMES), 3),
                       "max": round(max(TIMES), 3)}
print("\nBALANCES / TIMING:", json.dumps(results, indent=2))
Path(__file__).with_name("smoke_results.json").write_text(json.dumps({"results": results, "log": LOG}, indent=2))
