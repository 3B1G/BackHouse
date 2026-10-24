"""Read-only tools for the Bookkeeper agent: the bank (Nessie) and the Book (books.db), as Claude tool definitions.

Each tool reads exactly one source, opened read-only:
  bank  data/nessie_sync_<tag>.db  Nessie, pulled by nessie_sync.py (tag from $BOH_NESSIE_TAG, default boh-1loc)
  book  data/books.db              the restaurant's documents, incl. `costs` / `cost_lines` from build_book.py
Never reads data/grader/ and never shows Nessie's |ref= tags (nessie_sync strips them; bank_id is the row id
grade.py scores flags by). Amounts are integer cents, dates YYYY-MM-DD.

Agent loop (anthropic SDK; a manual loop so every call lands in the trace that checks.py verifies against):

    import anthropic
    from bookkeeper_tools import TOOLS, ToolSession
    client, session = anthropic.Anthropic(), ToolSession(max_calls=8)
    messages = [{"role": "user", "content": starter_packet}]
    while True:
        response = client.messages.create(model="claude-opus-5-5", max_tokens=16000, system=SYSTEM,
                                          tools=TOOLS, messages=messages)
        messages.append({"role": "assistant", "content": response.content})
        calls = [b for b in response.content if b.type == "tool_use"]
        if not calls:
            break
        messages.append({"role": "user", "content": [session.tool_result(b) for b in calls]})   # one message for all
    session.trace   # [{n, tool, input, output}, ...]

Plain Python: call_tool("search_costs", {"text": "Mission"}), or the functions directly.
load_bank_frame() returns the Nessie copy shaped like bank_feed.csv, for code that reads the CSV today.
"""
import json
import os
import re
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from functools import lru_cache
from pathlib import Path

import pandas as pd

import config as C

D = Path(__file__).parent / "data"
TAG = os.environ.get("BOH_NESSIE_TAG", "boh-1loc")
BOOK_DB, BANK_DB = D / "books.db", D / f"nessie_sync_{TAG}.db"
START, END = str(C.START), str(C.END)
DIRECTION = {"deposit": "in", "withdrawal": "out", "purchase": "out"}
BANK_HOLIDAYS = {date(2025, 11, 11), date(2025, 11, 27), date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 19),   # as generate.py
                 date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19), date(2026, 9, 7), date(2026, 10, 12)}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
MAX_ROWS = 50
BANK_COLS = ["bank_id", "account", "date", "nessie_type", "direction", "amount_cents", "counterparty", "memo"]
COST_COLS = ["cost_id", "cost_type", "category", "payee", "location", "doc_date", "due_date", "amount_cents", "credit_cents",
             "terms_days", "installments", "payment_method", "status", "status_basis", "description"]


# ---------------------------------------------------------------- data access (read-only, cached)
def _read(path, sql, hint):
    if not path.exists():
        raise FileNotFoundError(f"{path.name} is missing: {hint}")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as con:
        try:
            return pd.read_sql(sql, con)
        except pd.errors.DatabaseError as e:
            raise RuntimeError(f"{path.name}: {e}; {hint}") from None


@lru_cache(None)
def _bank_raw():
    tx = _read(BANK_DB, "select * from bank_tx", f"run python nessie_sync.py --tag {TAG}")
    tx["amount"] = tx.amount.astype(int)
    tx["direction"] = tx.nessie_type.map(DIRECTION)
    return tx.sort_values(["date", "account", "nessie_type", "amount", "bank_id"]).reset_index(drop=True)


@lru_cache(None)
def _bank():
    return _bank_raw().rename(columns={"amount": "amount_cents"})


@lru_cache(None)
def _bank_table(name):
    return _read(BANK_DB, f"select * from {name}", f"run python nessie_sync.py --tag {TAG}")


@lru_cache(None)
def _book(name, sql=None):
    hint = "run python generate.py" + (" then python build_book.py" if name in ("costs", "cost_lines") else "")
    return _read(BOOK_DB, sql or f"select * from {name}", hint)


def reload():
    """Forget cached tables (after re-running generate.py, build_book.py or nessie_sync.py)."""
    for f in (_bank_raw, _bank, _bank_table, _book, _terms):
        f.cache_clear()


def load_bank_frame():
    return _bank_raw()[["bank_id", "account", "date", "nessie_type", "direction", "amount", "counterparty", "memo"]].copy()


def bank_id_map():
    """{Nessie transaction id: bank_id}, for converting reports that name Nessie's own ids (contracts.from_bk_report).
    Uses the Nessie sync when present, else the committed upload manifest (ids never change after the upload)."""
    try:
        tx = _bank_raw()
        return dict(zip(tx.nessie_id, tx.bank_id))
    except FileNotFoundError:
        manifest = D / f"nessie_manifest_{TAG}.json"
        if not manifest.exists():
            raise FileNotFoundError(f"neither {BANK_DB.name} nor {manifest.name} exists: run python nessie_sync.py --tag {TAG}") from None
        return {nid: bid for bid, nid in json.loads(manifest.read_text())["tx"].items()}


@lru_cache(None)
def _terms():
    """Card processor and delivery platform rates, parsed from the books' agreements (not from config)."""
    ag = _book("agreements")
    card = ag[ag.terms.str.contains("per card transaction")].iloc[0]
    m = re.search(r"([\d.]+)% \+ (\d+)c", card.terms)
    plat = {r.counterparty: (float(re.search(r"([\d.]+)% commission", r.terms).group(1)) / 100, r.terms)
            for r in ag.itertuples() if "commission" in r.terms}
    return dict(processor=card.counterparty, pct=float(m.group(1)) / 100, fixed=int(m.group(2)), terms=card.terms, platforms=plat)


# ---------------------------------------------------------------- helpers
def _py(v):
    if hasattr(v, "item"):
        v = v.item()
    try:
        if pd.isna(v):
            return None
    except (TypeError, ValueError):
        pass
    return int(v) if isinstance(v, float) and v.is_integer() else v      # integer columns with NULLs read back as float


def _records(df, cols):
    return [{c: _py(v) for c, v in zip(cols, row)} for row in df[cols].itertuples(index=False, name=None)]


def _page(df, cols, limit):
    limit = max(1, min(int(limit or 20), MAX_ROWS))
    return {"total_matches": len(df), "returned": min(len(df), limit), "rows": _records(df.head(limit), cols)}


def _between(s, lo=None, hi=None):
    m = pd.Series(True, index=s.index)
    if lo:
        m &= s >= lo
    if hi:
        m &= s <= hi
    return m


def _has(s, text):
    return s.fillna("").str.contains(text, case=False, regex=False)


def next_bday(d):
    while d.weekday() >= 5 or d in BANK_HOLIDAYS:
        d += timedelta(1)
    return d


# ---------------------------------------------------------------- bank tools (Nessie)
def get_bank_transaction(bank_id):
    tx = _bank()
    hit = tx[tx.bank_id == bank_id]
    if hit.empty:
        return {"error": f"no bank transaction with bank_id {bank_id}"}
    r = hit.iloc[0]
    d = date.fromisoformat(r.date)
    near = tx[(tx.account == r.account) & (tx.amount_cents == r.amount_cents) & (tx.bank_id != bank_id)
              & _between(tx.date, str(d - timedelta(14)), str(d + timedelta(14)))]
    return {"transaction": _records(hit, BANK_COLS)[0], "same_amount_within_14_days": _records(near, ["bank_id", "date", "memo"])}


def search_bank_transactions(account=None, date_from=None, date_to=None, direction=None, nessie_type=None, text=None,
                             amount_cents=None, amount_tolerance_cents=0, limit=20):
    tx = _bank()
    m = _between(tx.date, date_from, date_to)
    if account:
        m &= tx.account == account
    if direction:
        m &= tx.direction == direction
    if nessie_type:
        m &= tx.nessie_type == nessie_type
    if text:
        m &= _has(tx.memo, text) | _has(tx.counterparty, text)
    if amount_cents is not None:
        m &= (tx.amount_cents - amount_cents).abs() <= (amount_tolerance_cents or 0)
    out = _page(tx[m], BANK_COLS, limit)
    out["sum_amount_cents"] = int(tx[m].amount_cents.sum())
    return out


def get_payee_history(text, account=None, before_date=None, limit=10):
    tx = _bank()
    m = _has(tx.memo, text) | _has(tx.counterparty, text)
    if account:
        m &= tx.account == account
    if before_date:
        m &= tx.date < before_date
    hits = tx[m]
    if hits.empty:
        return {"matches": 0, "note": f"no bank transaction mentions '{text}'" + (f" before {before_date}" if before_date else "")
                + f" (bank data covers {START} to {END})"}
    groups = [dict(memo_pattern=p, count=len(g), first_date=g.date.min(), last_date=g.date.max(), min_cents=int(g.amount_cents.min()),
                   median_cents=int(round(g.amount_cents.median())), max_cents=int(g.amount_cents.max()))
              for p, g in hits.groupby(hits.memo.str.replace(r"\d", "#", regex=True))]
    limit = max(1, min(int(limit or 10), MAX_ROWS))
    return {"matches": len(hits), "first_date": hits.date.min(), "last_date": hits.date.max(),
            "by_memo": sorted(groups, key=lambda g: -g["count"]), "most_recent": _records(hits.tail(limit).iloc[::-1], BANK_COLS)}


def get_bank_bills(account=None, status=None):
    b = _bank_table("bank_bills").rename(columns={"payment_amount": "amount_cents"})
    m = pd.Series(True, index=b.index)
    if account:
        m &= b.account == account
    if status:
        m &= b.status == status
    b = b[m].sort_values(["payment_date", "nickname"])
    rows = _records(b, ["account", "status", "payee", "nickname", "payment_date", "recurring_date", "amount_cents"])
    return {"count": len(rows), "pending_total_cents": int(b[b.status == "pending"].amount_cents.sum()), "bills": rows}


def get_account_balances(as_of=None):
    as_of = as_of or END
    tx = _bank()
    out = []
    for a in _bank_table("bank_accounts").itertuples():
        t = tx[(tx.account == a.account) & (tx.date <= as_of)]
        inflow, outflow = int(t[t.direction == "in"].amount_cents.sum()), int(t[t.direction == "out"].amount_cents.sum())
        out.append(dict(account=a.account, nickname=a.nickname, opening_balance_cents=int(a.opening_balance_cents), opening_as_of=START,
                        money_in_cents=inflow, money_out_cents=outflow, balance_cents=int(a.opening_balance_cents) + inflow - outflow))
    return {"as_of": as_of, "accounts": out, "total_balance_cents": sum(a["balance_cents"] for a in out)}


# ---------------------------------------------------------------- book tools (books.db)
def search_costs(text=None, cost_type=None, category=None, status=None, location=None, date_from=None, date_to=None,
                 date_field="due_date", amount_cents=None, amount_tolerance_cents=0, limit=20):
    c = _book("costs")
    m = _between(c[date_field or "due_date"], date_from, date_to)
    if text:
        m &= _has(c.cost_id, text) | _has(c.payee, text) | _has(c.description, text)
    if cost_type:
        m &= c.cost_type == cost_type
    if category:
        m &= _has(c.category, category)
    if status:
        m &= c.status == status
    if location:
        m &= c.location == location
    if amount_cents is not None:
        m &= (c.amount_cents - amount_cents).abs() <= (amount_tolerance_cents or 0)
    out = _page(c[m], COST_COLS, limit)
    out["sum_amount_cents"] = int(c[m].amount_cents.sum())
    return out


def get_cost(cost_id):
    c = _book("costs")
    hit = c[c.cost_id == cost_id]
    if hit.empty:
        hit = c[c.cost_id.str.upper() == cost_id.strip().upper()]
    if hit.empty:
        close = c[_has(c.cost_id, cost_id[:6])].cost_id.head(5).tolist()
        return {"error": f"no cost {cost_id} in the Book" + (f"; similar ids: {', '.join(close)}" if close else "")}
    r = hit.iloc[0]
    lines = _book("cost_lines")
    out = {"cost": _records(hit, COST_COLS)[0],
           "lines": _records(lines[lines.cost_id == r.cost_id], ["item", "description", "qty", "unit", "unit_price_dollars", "line_total_cents"])}
    plans = _book("ap_payment_plans")
    if (plans.invoice_no == r.cost_id).any():
        out["installments"] = _records(plans[plans.invoice_no == r.cost_id].rename(columns={"amount": "amount_cents"}),
                                       ["installment", "due", "amount_cents"])
    cms = _book("credit_memos")
    if (cms.invoice_no == r.cost_id).any():
        out["credit_memos"] = _records(cms[cms.invoice_no == r.cost_id].rename(columns={"amount": "amount_cents"}),
                                       ["credit_no", "issued", "amount_cents", "reason"])
    if r.cost_type == "supplier_invoice":
        s = _book("suppliers")
        s = s[s.supplier == r.payee].iloc[0]
        out["supplier_terms"] = dict(category=s.category, terms_days=int(s.terms_days),
                                     delivery_days=", ".join(WEEKDAYS[d] for d in json.loads(s.delivery_days)))
    before = c[(c.payee == r.payee) & (c.cost_id != r.cost_id) & (c.due_date <= r.due_date)].tail(6)
    out["same_payee_before"] = _records(before, ["cost_id", "due_date", "amount_cents", "status"])
    return out


def get_agreements(counterparty=None):
    t = _terms()
    sections = {
        "card_processor": [dict(counterparty=t["processor"], rate_pct=round(t["pct"] * 100, 4), fixed_fee_cents_per_transaction=t["fixed"],
                                terms=t["terms"])],
        "delivery_platforms": [dict(counterparty=p, commission_pct=round(r * 100, 4), terms=txt) for p, (r, txt) in t["platforms"].items()],
        "suppliers": [dict(counterparty=s.supplier, category=s.category, terms_days=int(s.terms_days),
                           delivery_days=", ".join(WEEKDAYS[d] for d in json.loads(s.delivery_days))) for s in _book("suppliers").itertuples()],
        "leases": [dict(counterparty=L.landlord, location=L.location, monthly_rent_cents=int(L.base), escalator_pct=round(L.escalator_pct * 100, 4),
                        escalator_from=L.anniversary) for L in _book("leases").itertuples()],
        "loans": [dict(counterparty=L.lender, type=L.type, rate_pct=round(L.rate * 100, 4), monthly_payment_cents=int(L.monthly_cents),
                       pay_day=int(L.pay_day), account=L.account, balance_at_start_cents=int(L.balance_at_start_cents))
                  for L in _book("loans").itertuples()],
    }
    if counterparty:
        sections = {k: [x for x in v if counterparty.lower() in x["counterparty"].lower()] for k, v in sections.items()}
        sections = {k: v for k, v in sections.items() if v}
        if not sections:
            return {"error": f"no agreement with a counterparty matching '{counterparty}'"}
    return sections


def get_card_batches(date_from, date_to, location=None, limit=31):
    t = _terms()
    b = _book("pos_card_batches")
    b = b[_between(b.batch_date, date_from, date_to) & ((b.location == location) if location else True)].copy()
    b["expected_fee_cents"] = (b.gross_sales * t["pct"]).round().astype(int) + t["fixed"] * b.transactions
    b["expected_net_cents"] = b.gross_sales - b.refunds - b.expected_fee_cents
    b["expected_settlement_date"] = [str(next_bday(date.fromisoformat(d) + timedelta(1))) for d in b.batch_date]
    b = b.rename(columns={"gross_sales": "gross_sales_cents", "refunds": "refunds_cents"})
    out = _page(b, ["location", "batch_date", "gross_sales_cents", "refunds_cents", "transactions", "expected_fee_cents",
                    "expected_net_cents", "expected_settlement_date"], limit)
    out["totals"] = {k: int(b[k].sum()) for k in ["gross_sales_cents", "refunds_cents", "transactions", "expected_fee_cents", "expected_net_cents"]}
    out["fee_rule"] = f"{t['pct']:.1%} of gross + {t['fixed']}c per transaction ({t['processor']} agreement); estimated from batch " \
                      "totals while the processor rounds each transaction, so expect a few cents of difference per batch"
    disp = _book("processor_disputes")
    disp = disp[_between(disp.debited, date_from, date_to) | _between(disp.opened, date_from, date_to)]
    out["chargebacks"] = _records(disp.rename(columns={"amount": "amount_cents"}), ["case_id", "location", "opened", "debited", "amount_cents", "outcome"])
    return out


def get_cash_deposit_slips(date_from, date_to, location=None, limit=31):
    s = _book("cash_deposit_slips")
    s = s[_between(s.deposit_date, date_from, date_to) & ((s.location == location) if location else True)].copy()
    s["over_short_cents"] = s.counted_cash - s.pos_cash
    s = s.rename(columns={"pos_cash": "pos_cash_cents", "counted_cash": "counted_cash_cents"})
    out = _page(s, ["location", "deposit_date", "pos_cash_cents", "counted_cash_cents", "over_short_cents"], limit)
    out["totals"] = {k: int(s[k].sum()) for k in ["pos_cash_cents", "counted_cash_cents", "over_short_cents"]}
    out["note"] = "counted_cash_cents is the cash on the slip, i.e. the amount taken to the bank"
    if date_to >= END:
        dit = _book("deposits_in_transit")
        dit = dit[dit.type == "undeposited_cash"]
        out["not_yet_deposited"] = _records(dit.rename(columns={"amount": "amount_cents"}), ["location", "source_date", "amount_cents", "note"])
    return out


def get_delivery_payouts(date_from, date_to, location=None, platform=None, limit=20):
    t = _terms()
    tk = _book("tickets", "select date, location, channel, subtotal from tickets where tender = 'platform'").copy()
    tk["week_start"] = [str(d - timedelta(d.weekday())) for d in map(date.fromisoformat, tk.date)]
    if location:
        tk = tk[tk.location == location]
    if platform:
        tk = tk[_has(tk.channel, platform)]
    w = tk.groupby(["location", "channel", "week_start"]).agg(orders=("subtotal", "size"), subtotal_cents=("subtotal", "sum")).reset_index()
    w = w[_between(w.week_start, date_from, date_to)].rename(columns={"channel": "platform"})
    w["commission_pct"] = [round(t["platforms"][p][0] * 100, 4) for p in w.platform]
    w["expected_commission_cents"] = [int(round(s * t["platforms"][p][0])) for s, p in zip(w.subtotal_cents, w.platform)]
    w["expected_payout_cents"] = w.subtotal_cents - w.expected_commission_cents
    w["expected_payout_date"] = [str(next_bday(date.fromisoformat(d) + timedelta(8))) for d in w.week_start]
    w = w.sort_values(["week_start", "platform"])
    out = _page(w, ["location", "platform", "week_start", "orders", "subtotal_cents", "commission_pct", "expected_commission_cents",
                    "expected_payout_cents", "expected_payout_date"], limit)
    out["totals"] = {k: int(w[k].sum()) for k in ["orders", "subtotal_cents", "expected_commission_cents", "expected_payout_cents"]}
    return out


# ---------------------------------------------------------------- tool definitions (Claude tool use)
def _str(desc, **kw):
    return {"type": "string", "description": desc, **kw}


def _int(desc):
    return {"type": "integer", "description": desc}


def _date(desc):
    return _str(desc + " (YYYY-MM-DD)", format="date")


def _tool(name, description, properties, required=()):
    return {"name": name, "description": description,
            "input_schema": {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}}


ACCOUNT = _str("Bank account code, e.g. CHS (Chestnut operating checking) or RES (reserve savings)")
LIMIT = _int(f"Max rows to return (default 20, at most {MAX_ROWS})")
BANK_SPAN = f"Bank data covers {START} to {END}."
TOOLS = [
    _tool("get_bank_transaction",
          "Look up one bank transaction in Nessie by its bank_id. Returns account, date, type (deposit, withdrawal, purchase), "
          "direction (in/out), amount in cents, counterparty (purchases only) and memo, plus other transactions in the same account "
          "with exactly the same amount within 14 days (useful for spotting duplicate charges and repeat standing orders).",
          {"bank_id": _str("The bank transaction id, e.g. T1A2B3C4D5")}, ["bank_id"]),
    _tool("search_bank_transactions",
          "Search bank transactions in Nessie. Every filter is optional and they combine with AND. `text` matches the memo or "
          "counterparty, case-insensitive (e.g. 'MISSION DRY GOODS', 'TABLETAP PAYMENTS DEP', 'BRANCH DEPOSIT'). Use amount_cents "
          "with amount_tolerance_cents to find amounts near a value. Returns rows oldest first, the total match count and the summed "
          "amount. " + BANK_SPAN,
          {"account": ACCOUNT, "date_from": _date("Earliest date"), "date_to": _date("Latest date"),
           "direction": _str("Money in or out", enum=["in", "out"]),
           "nessie_type": _str("Nessie transaction type", enum=["deposit", "withdrawal", "purchase"]),
           "text": _str("Substring of the memo or counterparty"), "amount_cents": _int("Target amount in cents"),
           "amount_tolerance_cents": _int("Allowed difference from amount_cents (default 0 = exact)"), "limit": LIMIT}),
    _tool("get_payee_history",
          "Summarize a payee's history in the bank (Nessie): every transaction whose memo or counterparty contains `text`, grouped "
          "by memo pattern (digits shown as #) with count, first and last date and min/median/max amount, plus the most recent rows. "
          "Use before_date to ask whether the restaurant had ever paid this payee, or this kind of charge, before. " + BANK_SPAN,
          {"text": _str("Payee or memo text, e.g. 'TABLETAP POS'"), "account": ACCOUNT,
           "before_date": _date("Only count transactions before this date"), "limit": _int("Recent rows to return (default 10)")}, ["text"]),
    _tool("get_bank_bills",
          "List the bills stored in Nessie: pending bills (supplier invoices, vendor bills and sales tax billed but unpaid when the "
          "bank data ends, nickname '<document id> due <date>') and recurring autopays (utilities, software, insurance, rent). "
          "Amounts in cents.",
          {"account": ACCOUNT, "status": _str("Bill status", enum=["pending", "recurring", "completed", "cancelled"])}),
    _tool("get_account_balances",
          "Cash per bank account in Nessie as of a date: opening balance plus money in minus money out. Nessie never updates its "
          "stored balance, so this is the correct way to get a balance. Defaults to the last bank date.",
          {"as_of": _date("Balance at the end of this date")}),
    _tool("search_costs",
          "Search the Book: every cost in the restaurant's own records (supplier invoices, vendor bills, rent, payroll, loan payments, "
          "sales tax, repairs, permits, owner draws, card chargebacks). Filters are optional and combine with AND. `text` matches the "
          "cost id, payee or description, case-insensitive. `status` is what the books say: paid (the books treat it as paid; autopays "
          "are assumed paid on the due date), pending (billed, unpaid when the books close), scheduled (future obligation from a lease "
          "or loan, no bill yet). The bank is the proof of payment: a 'paid' cost with no matching bank debit is a finding. Rows are "
          "sorted by due date; also returns the summed amount.",
          {"text": _str("Substring of the cost id, payee or description, e.g. 'Mission Dry Goods', 'salmon', 'MDG-CHS-10053'"),
           "cost_type": _str("Kind of cost", enum=["supplier_invoice", "vendor_bill", "rent", "payroll", "loan_payment", "sales_tax",
                                                   "repair", "misc_payable", "owner_draw", "chargeback"]),
           "category": _str("Substring of the category, e.g. 'Produce', 'Utilities', 'Occupancy'"),
           "status": _str("Status in the books", enum=["paid", "pending", "scheduled"]),
           "location": _str("Restaurant location code, e.g. CHS"), "date_from": _date("Earliest date"), "date_to": _date("Latest date"),
           "date_field": _str("Which date the range applies to (default due_date)", enum=["due_date", "doc_date"]),
           "amount_cents": _int("Target document amount in cents"),
           "amount_tolerance_cents": _int("Allowed difference from amount_cents (default 0 = exact)"), "limit": LIMIT}),
    _tool("get_cost",
          "Full detail for one cost in the Book by cost_id: the cost, its line items (what was bought or what the charge is made of, "
          "quantity, unit price), installment plan, credit memos, supplier terms, and the same payee's previous costs. Ids look like "
          "'MDG-CHS-10053' (supplier invoice), 'VB-CHS-TP-2026-10' (vendor bill), 'RENT-CHS-2026-10', 'PAY-CHS-20261016', "
          "'LOAN-2026-10', 'TAX-2026-09', 'DRAW-2026-10', 'RPR-CHS-2026-06-0', 'MISC-CHS-20260115', 'CB-CHS-0121'.",
          {"cost_id": _str("The cost id")}, ["cost_id"]),
    _tool("get_agreements",
          "Contract terms from the books: card processor fees (percent plus fixed cents per transaction, settlement timing), delivery "
          "platform commission rates, supplier payment terms and delivery days, the lease (rent, escalator) and the loan.",
          {"counterparty": _str("Only agreements whose counterparty contains this text, e.g. 'TableTap', 'Bay Produce'")}),
    _tool("get_card_batches",
          "POS card batches from the books for a date range (batch date = sale date): gross card sales, refunds, transaction count, "
          "and the deposit the processor agreement implies (expected fee and expected net) with the expected settlement date (next "
          "business day). Also lists card chargebacks in the range, and totals. Compare with the 'TABLETAP PAYMENTS DEP' deposits "
          "from search_bank_transactions.",
          {"date_from": _date("First batch date"), "date_to": _date("Last batch date"), "location": _str("Location code, e.g. CHS"),
           "limit": _int(f"Max rows (default 31, at most {MAX_ROWS})")}, ["date_from", "date_to"]),
    _tool("get_cash_deposit_slips",
          "Cash deposit slips from the books for a date range: cash the POS recorded, cash counted and taken to the bank, and the "
          "over/short at the till. When the range reaches the end of the books, also shows cash not yet deposited. Compare with "
          "'BRANCH DEPOSIT' rows from search_bank_transactions.",
          {"date_from": _date("First deposit date"), "date_to": _date("Last deposit date"), "location": _str("Location code, e.g. CHS"),
           "limit": _int(f"Max rows (default 31, at most {MAX_ROWS})")}, ["date_from", "date_to"]),
    _tool("get_delivery_payouts",
          "Weekly delivery-platform sales from the books' POS tickets (weeks start Monday), for weeks starting in the date range: "
          "order count, subtotal, the commission rate in the platform agreement, and the expected commission, payout and payout date "
          "(the Tuesday after the week, next business day). Compare with '<PLATFORM> PAYOUT WK' deposits from search_bank_transactions.",
          {"date_from": _date("Earliest week start"), "date_to": _date("Latest week start"), "location": _str("Location code, e.g. CHS"),
           "platform": _str("Platform name, e.g. RapidPlate or UrbanBite"), "limit": LIMIT}, ["date_from", "date_to"]),
]
FUNCS = {f.__name__: f for f in [get_bank_transaction, search_bank_transactions, get_payee_history, get_bank_bills, get_account_balances,
                                 search_costs, get_cost, get_agreements, get_card_batches, get_cash_deposit_slips, get_delivery_payouts]}
SPECS = {t["name"]: t for t in TOOLS}
assert SPECS.keys() == FUNCS.keys()


def _validate(spec, args):
    if not isinstance(args, dict):
        return "tool input must be a JSON object"
    props, schema = spec["input_schema"]["properties"], spec["input_schema"]
    for k in schema["required"]:
        if args.get(k) in (None, ""):
            return f"missing required field '{k}'"
    for k, v in args.items():
        if k not in props:
            return f"unknown field '{k}'; allowed: {', '.join(props)}"
        p = props[k]
        if v is None:
            continue
        if p["type"] == "integer":
            if isinstance(v, float) and v.is_integer():
                args[k] = v = int(v)
            if not isinstance(v, int) or isinstance(v, bool):
                return f"'{k}' must be an integer"
        elif not isinstance(v, str):
            return f"'{k}' must be a string"
        if "enum" in p and v not in p["enum"]:
            return f"'{k}' must be one of: {', '.join(p['enum'])}"
        if p.get("format") == "date":
            try:
                date.fromisoformat(v)
            except ValueError:
                return f"'{k}' must be a date like 2026-08-01"
    return None


def call_tool(name, args):
    """Run one tool by name. Never raises: problems come back as {"error": ...} so the agent can correct itself."""
    spec = SPECS.get(name)
    if spec is None:
        return {"error": f"unknown tool '{name}'"}
    args = dict(args or {})
    problem = _validate(spec, args)
    if problem:
        return {"error": problem}
    try:
        return FUNCS[name](**args)
    except Exception as e:                       # report to the agent instead of crashing its loop
        return {"error": f"{type(e).__name__}: {e}"}


class ToolSession:
    """One investigation: enforces the call budget and keeps the trace checks.py compares the verdict against."""

    def __init__(self, max_calls=8):
        self.max_calls, self.trace = max_calls, []

    def call(self, name, args):
        if sum(1 for t in self.trace if not t["over_budget"]) >= self.max_calls:
            out, over = {"error": f"the budget of {self.max_calls} tool calls is used up; give your verdict from the evidence you have"}, True
        else:
            out, over = call_tool(name, args), False
        self.trace.append(dict(n=len(self.trace) + 1, tool=name, input=args, output=out, over_budget=over))
        return out

    def tool_result(self, block):
        """A tool_use block from the API response in, the matching tool_result block out."""
        out = self.call(block.name, block.input)
        result = {"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(out, default=str)}
        if "error" in out:
            result["is_error"] = True
        return result


if __name__ == "__main__":
    print(f"{len(TOOLS)} tools: {', '.join(SPECS)}\nbank {BANK_DB.name} | book {BOOK_DB.name}\n")
    demo = [("get_account_balances", {}),
            ("search_costs", {"status": "pending", "limit": 3}),
            ("get_cost", {"cost_id": "RENT-CHS-2026-10"}),
            ("get_payee_history", {"text": "CITY GAS", "limit": 2}),
            ("search_bank_transactions", {"text": "ACH SONOMA DAIRY", "date_from": "2026-10-01", "limit": 2}),
            ("get_card_batches", {"date_from": "2026-10-01", "date_to": "2026-10-03"}),
            ("get_agreements", {"counterparty": "Bay Produce"}),
            ("search_costs", {"cost_type": "nope"})]
    for name, args in demo:
        print(f"> {name}({json.dumps(args)})\n{json.dumps(call_tool(name, args), default=str)[:700]}\n")
