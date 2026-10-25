"""The 11 read-only investigation tools over the bank (a Nessie snapshot) and the Book (books.db).

One ToolSession is one investigation. It opens both databases read-only once, copies what the tools need into memory and
never touches disk again, so a run sees frozen sources even if the files change underneath it. Nothing here reads
process-wide state: root, Nessie tag and cutoff are arguments.

  historical  (Bookkeeper) Everything is cut off at `cutoff` (default: the latest bank date) before any result is
              computed. Bank rows carry the native Nessie id as `bank_id`; synthetic ids and |ref= markers never leave
              this module. Payment status needs a dated payment record (see book_projection). Bank bills, open AP and
              deposits in transit are current-state only and are reported unavailable or omitted.
  legacy      (standalone nessie_lab/bookkeeper_tools.py) The books as they close: synthetic bank ids, Book statuses
              from open AP and autopay, deposits in transit, Nessie bills. No cutoff filtering.

Bank tools: get_bank_transaction, search_bank_transactions, get_payee_history, get_bank_bills, get_account_balances.
Books tools: search_costs, get_cost, get_agreements, get_card_batches, get_cash_deposit_slips, get_delivery_payouts.
Amounts are integer cents (unit_price_dollars excepted), dates YYYY-MM-DD. session.call(name, args) never raises.
"""
import json
import re
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from .book_projection import TABLES as COST_TABLES, project_costs

TOOLSET_VERSION = "domain-11/3"
TOOL_GROUPS = {
    "bank": ["get_bank_transaction", "search_bank_transactions", "get_payee_history", "get_bank_bills", "get_account_balances"],
    "books": ["search_costs", "get_cost", "get_agreements", "get_card_batches", "get_cash_deposit_slips", "get_delivery_payouts"],
}
GROUP_OF = {name: group for group, names in TOOL_GROUPS.items() for name in names}
MAX_ROWS = 50                       # page size ceiling for every primary list
SUPPLEMENT_ROWS = 25                # ceiling for lists attached to a result (related rows, chargebacks, memo groups)
LINE_ROWS = 50
RESPONSE_CAP = 120_000              # bytes of JSON per tool response
DIRECTION = {"deposit": "in", "withdrawal": "out", "purchase": "out"}
BANK_HOLIDAYS = {date(2025, 11, 11), date(2025, 11, 27), date(2025, 12, 25), date(2026, 1, 1), date(2026, 1, 19),   # as generate.py
                 date(2026, 2, 16), date(2026, 5, 25), date(2026, 6, 19), date(2026, 9, 7), date(2026, 10, 12)}
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
LEGACY_BANK_COLS = ["bank_id", "account", "date", "nessie_type", "direction", "amount_cents", "counterparty", "memo"]
BANK_FRAME_COLS = LEGACY_BANK_COLS + ["status"]
BANK_COLS = ["bank_id", "account", "date", "nessie_type", "amount_cents", "counterparty", "memo", "status"]   # direction follows type
COST_COLS = ["cost_id", "cost_type", "category", "payee", "location", "doc_date", "due_date", "amount_cents", "credit_cents",
             "terms_days", "installments", "payment_method", "status", "status_basis", "description"]
COST_SEARCH_COLS = ["cost_id", "cost_type", "payee", "doc_date", "due_date", "amount_cents", "credit_cents", "status"]   # detail: get_cost
REF = re.compile(r"\s*\|ref=\S+")

# The date each books table is dated by; rows after the cutoff do not exist yet. Children follow their parent document.
DATED = {
    "menu_prices": "effective_from", "daily_ops": "date", "tickets": "date",
    "refunds": "refund_date", "gift_cards": "sold_date", "pos_card_batches": "batch_date",
    "cash_deposit_slips": "deposit_date", "invoices": "invoice_date",
    "credit_memos": "issued", "inventory_counts": "count_date", "payroll_runs": "pay_date",
    "vendor_bills": "bill_date", "repair_invoices": "date", "misc_payables": "date",
    "owner_draws": "date", "internal_transfers": "date", "processor_disputes": "opened",
    "invoice_payments": "payment_date",
}
CHILDREN = {"ticket_lines": ("tickets", "ticket_id"), "invoice_lines": ("invoices", "invoice_no"),
            "ap_payment_plans": ("invoices", "invoice_no")}
SNAPSHOT_ONLY = {"open_ap", "deposits_in_transit"}       # current state with no history: legacy mode only
BOOK_TABLES = sorted(set(COST_TABLES) | {"accounts", "agreements", "suppliers", "pos_card_batches", "cash_deposit_slips",
                                         "tickets", "invoice_payments"} | SNAPSHOT_ONLY)

LIMITATIONS = {
    "unverified": "Unverified: snapshot completeness is assumed, not checked.",
    "static": "Agreements, supplier terms, leases, loans and other reference data have no version history; their validity "
              "at the cutoff is assumed.",
    "status": "Payment status is 'unknown' unless a payment record dated on or before the cutoff exists (payroll runs, owner "
              "draws, sales-tax paid dates, recorded invoice payments, chargeback debits). Autopay assumptions and current open "
              "AP are not used. 'unknown' is not evidence of non-payment.",
    "bills": "Nessie bills (pending and recurring) are current state with no dated snapshot, so bill history is unavailable.",
    "snapshot_only": "Open AP and deposits in transit are snapshot-only state and are omitted.",
    "future": "Dispute outcomes and actual payment dates after the cutoff are hidden. Contractual due dates are kept; "
              "expected settlement and payout dates are estimates from the agreements, not bank facts.",
    "balances": "Balances are opening balances plus completed bank activity; pending rows and rows before an account's "
                "opening date are excluded.",
    "weeks": "Platform sales are cut off at the cutoff before weekly aggregation; weeks with complete_week=false are partial.",
    "cash_in_transit": "Cash counted but not yet deposited at the cutoff is snapshot-only state and is omitted.",
}
NOTES = {      # the per-response reminder of each limitation; the full text is in the session's limitations
    "unverified": "snapshot completeness assumed", "static": "reference data assumed valid at the cutoff (no version history)",
    "status": "status unknown = no payment record by the cutoff, not non-payment", "bills": "bill history unavailable",
    "snapshot_only": "open AP and deposits in transit omitted", "future": "post-cutoff outcomes hidden; expected dates are estimates",
    "balances": "completed rows only, from each account's opening date", "weeks": "complete_week=false weeks are partial",
    "cash_in_transit": "cash not yet deposited at the cutoff omitted",
}
TOOL_LIMITATIONS = {
    "get_bank_transaction": ["unverified"], "search_bank_transactions": ["unverified"], "get_payee_history": ["unverified"],
    "get_bank_bills": ["bills"], "get_account_balances": ["balances", "unverified"],
    "search_costs": ["status", "snapshot_only"], "get_cost": ["status", "static"], "get_agreements": ["static"],
    "get_card_batches": ["future", "static"], "get_cash_deposit_slips": ["cash_in_transit"],
    "get_delivery_payouts": ["weeks", "future", "static"],
}


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


def _page(df, cols, limit, offset):
    page = df.iloc[offset:offset + limit]
    more = offset + len(page) < len(df)
    return {"total_matches": len(df), "offset": offset, "returned": len(page), "truncated": more,
            "next_offset": offset + len(page) if more else None, "rows": _records(page, cols)}


def _bounded(out, key, df, cols, cap=SUPPLEMENT_ROWS):
    """Attach a capped supplementary list with its full size and a truncation flag."""
    out[key] = _records(df.head(cap), cols)
    out[f"{key}_total"] = len(df)
    out[f"{key}_truncated"] = len(df) > cap
    return out


def _between(s, lo=None, hi=None):
    m = pd.Series(True, index=s.index)
    if lo or hi:
        m &= s.notna()
        v = s.fillna("").astype(str)
        if lo:
            m &= v >= lo
        if hi:
            m &= v <= hi
    return m


def _has(s, text):
    return s.fillna("").astype(str).str.contains(text, case=False, regex=False)


def next_bday(d):
    while d.weekday() >= 5 or d in BANK_HOLIDAYS:
        d += timedelta(1)
    return d


def _iso(v):
    return None if v is None else v.isoformat() if isinstance(v, date) else str(v)[:10]


def _read_all(path, names, hint):
    """Copy the named tables (those present) from one database opened read-only."""
    if not path.is_file():
        raise sqlite3.OperationalError(f"{path.name} is missing: {hint}")
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        present = {r[0] for r in con.execute("select name from sqlite_master where type='table'")}
        out = {}
        for name in names:
            if name not in present:
                continue
            sql = f'select * from "{name}"'
            if name == "tickets":                   # the tools only use delivery-platform tickets
                sql = "select ticket_id, date, location, channel, subtotal from tickets where tender = 'platform'"
            out[name] = pd.read_sql(sql, con)
        return out


class ToolError(ValueError):
    """A problem the model can fix by changing its request."""


# ---------------------------------------------------------------- the session
class ToolSession:
    """One investigation over frozen copies of the bank snapshot and the books."""

    def __init__(self, root, tag="boh-1loc", cutoff=None, mode="historical", window_start=None, sales_tax_rate=None):
        if mode not in ("historical", "legacy"):
            raise ValueError(f"unknown mode {mode!r}")
        self.mode, self.historical = mode, mode == "historical"
        self.root, self.tag = Path(root).resolve(), tag
        self.bank_path = self.root / "data" / f"nessie_sync_{tag}.db"
        self.books_path = self.root / "data" / "books.db"
        bank = _read_all(self.bank_path, ["bank_tx"] + ([] if self.historical else ["bank_bills"]),
                         f"run python nessie_sync.py --tag {tag}")
        if "bank_tx" not in bank or bank["bank_tx"].empty:
            raise ValueError("The bank snapshot has no transactions")
        tx = bank["bank_tx"]
        self.latest = str(tx.date.max())
        cutoff = _iso(cutoff)
        if self.historical:
            self.cutoff = cutoff or self.latest
            if self.cutoff > self.latest:
                raise ValueError(f"Requested date exceeds snapshot coverage ({self.latest})")
        else:
            self.cutoff = cutoff or self.latest            # legacy: the day the books close; nothing is filtered
        names = [n for n in BOOK_TABLES if self.historical is False or n not in SNAPSHOT_ONLY]
        self.books = _read_all(self.books_path, names, "run python generate.py")
        if "accounts" not in self.books:
            raise ValueError("books.db has no accounts table with opening balances")
        acc = self.books["accounts"]
        self.accounts = acc[acc.opening_as_of.astype(str) <= self.cutoff].reset_index(drop=True)
        if self.accounts.empty:
            raise ValueError("No opening balances exist at the requested cutoff")
        self.start = _iso(window_start) or str(self.accounts.opening_as_of.min())
        self.bank = self._bank_frame(tx)
        self.bills = bank.get("bank_bills")
        if self.historical:
            self._cut_books()
        self.costs, self.cost_lines = project_costs(self.books, self.cutoff, policy="book" if not self.historical else "historical",
                                                    window_start=self.start, sales_tax_rate=sales_tax_rate)
        self.costs["order_date"] = self.costs.due_date.fillna(self.costs.doc_date)
        self.costs = self.costs.sort_values(["order_date", "cost_id"]).reset_index(drop=True)
        self.limitations = [LIMITATIONS[k] for k in ("unverified", "static", "status", "bills", "snapshot_only", "future", "balances",
                                                     "weeks", "cash_in_transit")]
        if (self.bank.date < self.start).any():
            self.limitations.append("Bank rows before the opening date are available as evidence but excluded from balances.")
        self._terms = None
        self._matches = {}

    # ------------------------------------------------------------ loading
    def _bank_frame(self, tx):
        tx = tx.copy()
        if self.historical:
            tx = tx[tx.date.astype(str) <= self.cutoff]
            tx["bank_id"] = tx.nessie_id                  # the native Nessie reference; the synthetic id is dropped below
        tx["memo"] = [REF.sub("", m or "") for m in tx.memo]
        tx["amount_cents"] = tx.amount.astype(int)
        tx["direction"] = tx.nessie_type.map(DIRECTION)
        if "status" not in tx:
            tx["status"] = None
        tx = tx[BANK_FRAME_COLS + ["nessie_id"]] if not self.historical else tx[BANK_FRAME_COLS]
        return tx.sort_values(["date", "account", "nessie_type", "amount_cents", "bank_id"]).reset_index(drop=True)

    def _cut_books(self):
        """Drop every document dated after the cutoff and hide facts that were not known yet."""
        b, end = self.books, self.cutoff
        for name, field in DATED.items():
            if name in b:
                df = b[name]
                b[name] = df[df[field].notna() & (df[field].astype(str).str[:10] <= end)].reset_index(drop=True)
        for child, (parent, key) in CHILDREN.items():
            if child in b and parent in b:
                b[child] = b[child][b[child][key].isin(set(b[parent][key]))].reset_index(drop=True)
        if "processor_disputes" in b:
            d = b["processor_disputes"].copy()
            later = d.debited.notna() & (d.debited.astype(str) > end)
            d["debited"] = d.debited.astype(object).where(~later, None)
            d["outcome"] = d.outcome.astype(object).where(~later, None)
            b["processor_disputes"] = d

    def _book(self, name):
        if name not in self.books:
            raise ToolError(f"books.db has no {name} table")
        return self.books[name]

    @property
    def bank_rows(self):
        return len(self.bank)

    def close(self):
        """Nothing is held open; kept so callers can release a session uniformly."""

    def terms(self):
        """Card processor and delivery platform rates, parsed from the books' agreements (not from config)."""
        if self._terms is None:
            ag = self._book("agreements")
            card = ag[ag.terms.str.contains("per card transaction")]
            if card.empty:
                raise ToolError("the books have no card processor agreement")
            card = card.iloc[0]
            m = re.search(r"([\d.]+)% \+ (\d+)c", card.terms)
            plat = {r.counterparty: (float(re.search(r"([\d.]+)% commission", r.terms).group(1)) / 100, r.terms)
                    for r in ag.itertuples() if "commission" in r.terms}
            self._terms = dict(processor=card.counterparty, pct=float(m.group(1)) / 100, fixed=int(m.group(2)),
                               terms=card.terms, platforms=plat)
        return self._terms

    def _coverage(self):
        return f"bank data covers {self.start} to {self.cutoff}"

    # ------------------------------------------------------------ balances (shared by reports and get_account_balances)
    def account_balances(self, as_of=None):
        """Opening balances plus completed bank movements through `as_of` (default: the cutoff)."""
        as_of = _iso(as_of) or self.cutoff
        out = []
        accounts = self.accounts[self.accounts.opening_as_of.astype(str) <= as_of]
        known = {a.account: a for a in accounts.itertuples()}
        for a in accounts.itertuples():
            if not isinstance(_py(a.opening_balance_cents), int):
                raise ValueError("Opening balance must be integer cents")
        sums = {k: [0, 0] for k in known}
        for r in self.bank[(self.bank.status == "completed") & (self.bank.date <= as_of)].itertuples():
            if r.account not in known:
                if (self.accounts.account == r.account).any():
                    continue                                 # opens after as_of
                raise ValueError("Completed transaction has no opening account balance")
            if r.date < str(known[r.account].opening_as_of):
                continue
            value = _py(r.amount_cents)
            if not isinstance(value, int) or value < 0:
                raise ValueError("Bank amounts must be nonnegative integer cents")
            if r.nessie_type == "deposit":
                sums[r.account][0] += value
            elif r.nessie_type in ("purchase", "withdrawal"):
                sums[r.account][1] += value
            else:
                raise ValueError("Unsupported completed transaction type")
        for a in accounts.itertuples():
            opening, (inflow, outflow) = int(a.opening_balance_cents), sums[a.account]
            out.append(dict(account=a.account, opening_as_of=str(a.opening_as_of), opening_balance_cents=opening,
                            inflow_cents=inflow, outflow_cents=outflow, balance_cents=opening + inflow - outflow))
        return {"as_of": as_of, "accounts": out, "total_cents": sum(a["balance_cents"] for a in out)}

    # ------------------------------------------------------------ dispatch
    def call(self, name, args):
        """Validate, run one tool and wrap the result. Never raises: problems come back as {"error": ...}."""
        spec = _SPECS[self.mode].get(name)
        if spec is None:
            return self._wrap(name, {"error": f"unknown tool '{name}'; available: {', '.join(_SPECS[self.mode])}"})
        args = dict(args) if isinstance(args, dict) else args
        problem = _validate(spec, args)
        if problem:
            return self._wrap(name, {"error": problem})
        args = {k: v for k, v in args.items() if v is not None}
        try:
            out = getattr(self, name)(**args)
        except ToolError as e:
            out = {"error": str(e)}
        except Exception as e:                       # report to the agent instead of crashing its loop
            out = {"error": f"{type(e).__name__}: {e}"}
        out = self._wrap(name, out)
        if len(json.dumps(out, default=str)) > RESPONSE_CAP:
            out = self._wrap(name, {"error": f"response larger than {RESPONSE_CAP // 1000} KB; use a smaller limit or narrower filters"})
        return out

    def _wrap(self, name, out):
        if not self.historical:
            return out
        if "error" in out:
            return {"error": out["error"], "cutoff": self.cutoff}
        notes = [NOTES[k] for k in TOOL_LIMITATIONS.get(name, [])]
        return {"cutoff": self.cutoff, **out, "limitations": notes}

    # ------------------------------------------------------------ bank tools
    def get_bank_transaction(self, bank_id):
        tx = self.bank
        hit = tx[tx.bank_id == bank_id]
        if hit.empty:
            return {"error": f"no bank transaction with bank_id {bank_id}" + (f" on or before {self.cutoff}" if self.historical else "")}
        r = hit.iloc[0]
        d = date.fromisoformat(r.date)
        near = tx[(tx.account == r.account) & (tx.amount_cents == r.amount_cents) & (tx.bank_id != bank_id)
                  & _between(tx.date, str(d - timedelta(14)), str(d + timedelta(14)))]
        out = {"transaction": _records(hit, self._bank_cols())[0]}
        return _bounded(out, "same_amount_within_14_days", near, ["bank_id", "date", "memo"])

    def _bank_cols(self):
        return BANK_COLS if self.historical else LEGACY_BANK_COLS

    def search_bank_transactions(self, account=None, date_from=None, date_to=None, direction=None, nessie_type=None, text=None,
                                 amount_cents=None, amount_tolerance_cents=0, limit=20, offset=0):
        tx = self.bank
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
        out = _page(tx[m], self._bank_cols(), limit, offset)
        out["sum_amount_cents"] = int(tx[m].amount_cents.sum())
        return out

    def get_payee_history(self, text, account=None, before_date=None, limit=10):
        tx = self.bank
        m = _has(tx.memo, text) | _has(tx.counterparty, text)
        if account:
            m &= tx.account == account
        if before_date:
            m &= tx.date < before_date
        hits = tx[m]
        if hits.empty:
            return {"matches": 0, "note": f"no bank transaction mentions '{text}'" + (f" before {before_date}" if before_date else "")
                    + f" ({self._coverage()})"}
        groups = pd.DataFrame([dict(memo_pattern=p, count=len(g), first_date=g.date.min(), last_date=g.date.max(),
                                    min_cents=int(g.amount_cents.min()), median_cents=int(round(g.amount_cents.median())),
                                    max_cents=int(g.amount_cents.max()))
                               for p, g in hits.groupby(hits.memo.str.replace(r"\d", "#", regex=True))])
        groups = groups.sort_values(["count", "memo_pattern"], ascending=[False, True])
        out = {"matches": len(hits), "first_date": hits.date.min(), "last_date": hits.date.max()}
        _bounded(out, "by_memo", groups, list(groups.columns))
        out["most_recent"] = _records(hits.tail(limit).iloc[::-1], self._bank_cols())
        return out

    def get_bank_bills(self, account=None, status=None):
        if self.historical:
            return {"status": "unavailable", "reason": LIMITATIONS["bills"] + " Use search_costs for documented obligations "
                    "and search_bank_transactions for what was actually paid."}
        if self.bills is None:
            raise ToolError(f"{self.bank_path.name} has no bank_bills table")
        b = self.bills.rename(columns={"payment_amount": "amount_cents", "_id": "nessie_id"}).copy()
        b["amount_cents"] = pd.to_numeric(b.amount_cents, errors="coerce").round().astype("Int64")
        b["recurring_date"] = pd.to_numeric(b.recurring_date, errors="coerce").astype("Int64")
        m = pd.Series(True, index=b.index)
        if account:
            m &= b.account == account
        if status:
            m &= b.status == status
        b = b[m].sort_values(["payment_date", "nickname"])
        rows = _records(b, ["account", "status", "payee", "nickname", "payment_date", "recurring_date", "amount_cents"])
        return {"count": len(rows), "pending_total_cents": int(b[b.status == "pending"].amount_cents.sum()), "bills": rows}

    def get_account_balances(self, as_of=None):
        if as_of and as_of > self.cutoff:
            return {"error": f"as_of {as_of} is after the investigation cutoff {self.cutoff}"}
        if as_of and as_of < str(self.accounts.opening_as_of.min()):
            return {"error": f"as_of {as_of} is before the opening balances ({self.accounts.opening_as_of.min()})"}
        out = self.account_balances(as_of)
        nick = dict(zip(self.accounts.account, self.accounts.get("nickname", self.accounts.account)))
        for a in out["accounts"]:
            a["nickname"] = _py(nick.get(a["account"]))
        return out

    # ------------------------------------------------------------ books tools
    def search_costs(self, text=None, cost_type=None, category=None, status=None, location=None, date_from=None, date_to=None,
                     date_field="due_date", amount_cents=None, amount_tolerance_cents=0, limit=20, offset=0):
        c = self.costs
        m = _between(c[date_field or "due_date"], date_from, date_to)
        if text:
            m &= _has(c.cost_id, text) | _has(c.payee, text) | _has(c.description, text)
        if cost_type:
            m &= c.cost_type == cost_type
        if category:
            m &= _has(c.category, category)
        if location:
            m &= c.location == location
        if amount_cents is not None:
            m &= (c.amount_cents - amount_cents).abs() <= (amount_tolerance_cents or 0)
        before_status = m.copy()
        if status:
            m &= c.status == status
        out = _page(c[m], COST_SEARCH_COLS if self.historical else COST_COLS, limit, offset)
        out["sum_amount_cents"] = int(c[m].amount_cents.sum())
        if self.historical:
            unknown = before_status & (c.status == "unknown")
            out["status_counts"] = {k: int(v) for k, v in c[before_status].status.value_counts().sort_index().items()}
            if status and status != "unknown":
                out["omitted_unknown_status"] = int(unknown.sum())
                out["omitted_unknown_status_cents"] = int(c[unknown].amount_cents.sum())
        return out

    def get_cost(self, cost_id):
        c = self.costs
        hit = c[c.cost_id == cost_id]
        if hit.empty:
            hit = c[c.cost_id.str.upper() == cost_id.strip().upper()]
        if hit.empty:
            close = c[_has(c.cost_id, cost_id[:6])].cost_id.head(5).tolist()
            return {"error": f"no cost {cost_id} in the Book" + (f" on or before {self.cutoff}" if self.historical else "")
                    + (f"; similar ids: {', '.join(close)}" if close else "")}
        r = hit.iloc[0]
        lines = self.cost_lines
        out = {"cost": _records(hit, COST_COLS)[0]}
        _bounded(out, "lines", lines[lines.cost_id == r.cost_id],
                 ["item", "description", "qty", "unit", "unit_price_dollars", "line_total_cents"], LINE_ROWS)
        plans = self.books.get("ap_payment_plans")
        if plans is not None and (plans.invoice_no == r.cost_id).any():
            _bounded(out, "installments", plans[plans.invoice_no == r.cost_id].sort_values("installment").rename(
                columns={"amount": "amount_cents"}), ["installment", "due", "amount_cents"])
        cms = self.books.get("credit_memos")
        if cms is not None and (cms.invoice_no == r.cost_id).any():
            _bounded(out, "credit_memos", cms[cms.invoice_no == r.cost_id].sort_values(["issued", "credit_no"]).rename(
                columns={"amount": "amount_cents"}), ["credit_no", "issued", "amount_cents", "reason"])
        if r.cost_type == "supplier_invoice" and "suppliers" in self.books:
            s = self.books["suppliers"]
            s = s[s.supplier == r.payee]
            if len(s):
                s = s.iloc[0]
                out["supplier_terms"] = dict(category=s.category, terms_days=int(s.terms_days),
                                             delivery_days=", ".join(WEEKDAYS[d] for d in json.loads(s.delivery_days)))
        before = c[(c.payee == r.payee) & (c.cost_id != r.cost_id) & (c.order_date <= r.order_date)]
        out["same_payee_before"] = _records(before.tail(6).iloc[::-1], ["cost_id", "due_date", "amount_cents", "status"])
        out["same_payee_before_total"] = len(before)
        out["same_payee_before_truncated"] = len(before) > 6
        return out

    def get_agreements(self, counterparty=None):
        t = self.terms()
        sections = {
            "card_processor": [dict(counterparty=t["processor"], rate_pct=round(t["pct"] * 100, 4), fixed_fee_cents_per_transaction=t["fixed"],
                                    terms=t["terms"])],
            "delivery_platforms": [dict(counterparty=p, commission_pct=round(r * 100, 4), terms=txt) for p, (r, txt) in t["platforms"].items()],
            "suppliers": [dict(counterparty=s.supplier, category=s.category, terms_days=int(s.terms_days),
                               delivery_days=", ".join(WEEKDAYS[d] for d in json.loads(s.delivery_days)))
                          for s in self.books.get("suppliers", pd.DataFrame()).itertuples()],
            "leases": [dict(counterparty=L.landlord, location=L.location, monthly_rent_cents=int(L.base),
                            escalator_pct=round(L.escalator_pct * 100, 4), escalator_from=L.anniversary)
                       for L in self.books.get("leases", pd.DataFrame()).itertuples()],
            "loans": [dict(counterparty=L.lender, type=L.type, rate_pct=round(L.rate * 100, 4), monthly_payment_cents=int(L.monthly_cents),
                           pay_day=int(L.pay_day), account=L.account, balance_at_start_cents=int(L.balance_at_start_cents))
                      for L in self.books.get("loans", pd.DataFrame()).itertuples()],
        }
        if counterparty:
            sections = {k: [x for x in v if counterparty.lower() in x["counterparty"].lower()] for k, v in sections.items()}
            sections = {k: v for k, v in sections.items() if v}
            if not sections:
                return {"error": f"no agreement with a counterparty matching '{counterparty}'"}
        return sections

    # ------------------------------------------------------------ settlement matching
    def _matched(self, name, frame, memo, earliest, latest, due, expected, one_to_one=False):
        """Attach the bank deposits that settle each book row (all rows, so claims are one-to-one across calls).

        memo: the exact bank memo each row settles under; earliest/latest: the bank date window; due: when the deposit is
        expected; expected: the expected amount column. Cached per session: the sources are frozen.
        """
        if name in self._matches:
            return self._matches[name]
        tx = self.bank[self.bank.nessie_type == "deposit"]
        by_memo = {m: g for m, g in tx[tx.memo.isin(set(memo))].groupby("memo")}
        claimed, found = set(), {}
        # one-to-one memos (every cash slip settles under the same memo): same-day pairs first, so an earlier slip never
        # takes a later slip's deposit; then the window for whatever is left
        for exact in ([True, False] if one_to_one else [False]):
            for i, (m, lo, hi, want) in enumerate(zip(memo, earliest, latest, frame[expected])):
                g = by_memo.get(m)
                if i in found or g is None:
                    continue
                hits = g[((g.date == lo) if exact else (g.date >= lo) & (g.date <= hi)) & ~g.bank_id.isin(claimed)]
                if one_to_one:            # the closest amount, then the earliest date
                    hits = hits.assign(gap=(hits.amount_cents - want).abs()).sort_values(["gap", "date", "bank_id"]).head(1)
                if len(hits):
                    claimed.update(hits.bank_id)
                    found[i] = hits
        ids = [list(found[i].bank_id) if i in found else [] for i in range(len(memo))]
        dates = [found[i].date.min() if i in found else None for i in range(len(memo))]
        cents = [int(found[i].amount_cents.sum()) if i in found else None for i in range(len(memo))]
        frame = frame.copy()
        frame["bank_ids"], frame["bank_date"], frame["deposit_cents"] = ids, dates, cents
        frame["difference_cents"] = [None if c is None else c - e for c, e in zip(cents, frame[expected])]
        frame["settle_due"] = list(due)
        self._matches[name] = (frame, claimed)
        return self._matches[name]

    def _settle(self, frame, tolerance):
        """Status of each matched row under a tolerance."""
        def status(r):
            if len(r.bank_ids) > 1:
                return "multiple"
            if not r.bank_ids:
                return "not_yet_due" if r.settle_due > self.cutoff else "missing"
            return "matched" if abs(r.difference_cents) <= tolerance else "amount_mismatch"
        frame = frame.copy()
        frame["match_status"] = [status(r) for r in frame.itertuples()] if len(frame) else []
        return frame

    def _settlement_summary(self, out, frame, claimed, pattern, date_from, date_to):
        out["match_counts"] = {k: int(v) for k, v in frame.match_status.value_counts().sort_index().items()} if len(frame) else {}
        out["difference_total_cents"] = int(sum(int(d) for d in frame.difference_cents if pd.notna(d)))
        tx = self.bank[(self.bank.nessie_type == "deposit") & pattern(self.bank.memo) & _between(self.bank.date, date_from, date_to)]
        _bounded(out, "unmatched_deposits", tx[~tx.bank_id.isin(claimed)], ["bank_id", "date", "amount_cents", "memo"])
        return out

    @staticmethod
    def _exceptions(frame, exceptions_only):
        return frame[frame.match_status != "matched"] if exceptions_only else frame

    def _card_frame(self):
        t = self.terms()
        b = self._book("pos_card_batches").copy().sort_values(["batch_date", "location"]).reset_index(drop=True)
        b["expected_fee_cents"] = (b.gross_sales * t["pct"]).round().astype(int) + t["fixed"] * b.transactions
        b["expected_net_cents"] = b.gross_sales - b.refunds - b.expected_fee_cents
        b["expected_settlement_date"] = [str(next_bday(date.fromisoformat(d) + timedelta(1))) for d in b.batch_date]
        b["expected_after_cutoff"] = [d > self.cutoff for d in b.expected_settlement_date]
        b = b.rename(columns={"gross_sales": "gross_sales_cents", "refunds": "refunds_cents"})
        days = [date.fromisoformat(d) for d in b.batch_date]
        return self._matched("card", b, [f"{self._card_prefix()} {d:%m%d}" for d in days], [str(d) for d in days],
                             [str(d + timedelta(10)) for d in days], b.expected_settlement_date, "expected_net_cents")

    def _card_prefix(self):
        return f"{self.terms()['processor'].upper()} DEP"

    def get_card_batches(self, date_from, date_to, location=None, limit=31, offset=0, exceptions_only=False, tolerance_cents=10):
        t = self.terms()
        full, claimed = self._card_frame()
        b = self._settle(full[_between(full.batch_date, date_from, date_to) & ((full.location == location) if location else True)],
                         tolerance_cents)
        rows = self._exceptions(b, exceptions_only)
        out = _page(rows, ["location", "batch_date", "gross_sales_cents", "refunds_cents", "transactions", "expected_fee_cents",
                           "expected_net_cents", "expected_settlement_date", "expected_after_cutoff", "match_status", "bank_ids",
                           "bank_date", "deposit_cents", "difference_cents"], limit, offset)
        out["totals"] = {k: int(rows[k].sum()) for k in ["gross_sales_cents", "refunds_cents", "transactions", "expected_fee_cents",
                                                          "expected_net_cents"]}
        prefix = self._card_prefix() + " "
        self._settlement_summary(out, b, claimed, lambda m: m.str.startswith(prefix), date_from, date_to)
        out["fee_rule"] = f"{t['pct']:.1%} of gross + {t['fixed']}c per transaction ({t['processor']} agreement); estimated from batch " \
                          "totals while the processor rounds each transaction, so expect a few cents of difference per batch"
        out["matching"] = f"each batch is matched to the '{prefix}MMDD' deposit for its batch date, deposited within 10 days; " \
                          f"difference_cents = deposit - expected net; matched means within {tolerance_cents} cents"
        disp = self.books.get("processor_disputes")
        if disp is not None:
            disp = disp[_between(disp.debited, date_from, date_to) | _between(disp.opened, date_from, date_to)].sort_values(["opened", "case_id"])
            _bounded(out, "chargebacks", disp.rename(columns={"amount": "amount_cents"}),
                     ["case_id", "location", "opened", "debited", "amount_cents", "outcome"])
        return out

    def _cash_frame(self):
        s = self._book("cash_deposit_slips").copy().sort_values(["deposit_date", "location"]).reset_index(drop=True)
        s["over_short_cents"] = s.counted_cash - s.pos_cash
        s = s.rename(columns={"pos_cash": "pos_cash_cents", "counted_cash": "counted_cash_cents"})
        days = [date.fromisoformat(d) for d in s.deposit_date]
        latest = []
        for d in days:
            n = d
            for _ in range(3):
                n = next_bday(n + timedelta(1))
            latest.append(str(n))
        return self._matched("cash", s, ["BRANCH DEPOSIT"] * len(s), s.deposit_date, latest, s.deposit_date,
                             "counted_cash_cents", one_to_one=True)

    def get_cash_deposit_slips(self, date_from, date_to, location=None, limit=31, offset=0, exceptions_only=False, tolerance_cents=0):
        full, claimed = self._cash_frame()
        s = self._settle(full[_between(full.deposit_date, date_from, date_to) & ((full.location == location) if location else True)],
                         tolerance_cents)
        rows = self._exceptions(s, exceptions_only)
        out = _page(rows, ["location", "deposit_date", "pos_cash_cents", "counted_cash_cents", "over_short_cents", "match_status",
                           "bank_ids", "bank_date", "deposit_cents", "difference_cents"], limit, offset)
        out["totals"] = {k: int(rows[k].sum()) for k in ["pos_cash_cents", "counted_cash_cents", "over_short_cents"]}
        self._settlement_summary(out, s, claimed, lambda m: m == "BRANCH DEPOSIT", date_from, date_to)
        out["note"] = "counted_cash_cents is the cash on the slip, i.e. the amount taken to the bank"
        out["matching"] = "each slip is matched to one 'BRANCH DEPOSIT' on its date (else within 3 business days); " \
                          f"difference_cents = deposit - counted cash; matched means within {tolerance_cents} cents"
        if not self.historical and date_to >= self.cutoff and "deposits_in_transit" in self.books:
            dit = self.books["deposits_in_transit"]
            dit = dit[dit.type == "undeposited_cash"]
            out["not_yet_deposited"] = _records(dit.rename(columns={"amount": "amount_cents"}), ["location", "source_date", "amount_cents", "note"])
        return out

    def _payout_frame(self):
        t = self.terms()
        tk = self._book("tickets").copy()                 # platform tickets, already cut off at the cutoff
        tk["week_start"] = [str(d - timedelta(d.weekday())) for d in map(date.fromisoformat, tk.date.astype(str).str[:10])]
        w = tk.groupby(["location", "channel", "week_start"]).agg(orders=("subtotal", "size"), subtotal_cents=("subtotal", "sum")).reset_index()
        w = w.rename(columns={"channel": "platform"})
        w = w[w.platform.isin(t["platforms"])].sort_values(["week_start", "platform", "location"]).reset_index(drop=True)
        w["commission_pct"] = [round(t["platforms"][p][0] * 100, 4) for p in w.platform]
        w["expected_commission_cents"] = [int(round(s * t["platforms"][p][0])) for s, p in zip(w.subtotal_cents, w.platform)]
        w["expected_payout_cents"] = w.subtotal_cents - w.expected_commission_cents
        w["expected_payout_date"] = [str(next_bday(date.fromisoformat(d) + timedelta(8))) for d in w.week_start]
        w["complete_week"] = [str(date.fromisoformat(d) + timedelta(6)) <= self.cutoff for d in w.week_start]
        w["expected_after_cutoff"] = [d > self.cutoff for d in w.expected_payout_date]
        days = [date.fromisoformat(d) for d in w.week_start]
        return self._matched("payout", w, [f"{p.upper()} PAYOUT WK {d:%m%d}" for p, d in zip(w.platform, days)],
                             [str(d) for d in days], [str(d + timedelta(20)) for d in days], w.expected_payout_date,
                             "expected_payout_cents")

    def get_delivery_payouts(self, date_from, date_to, location=None, platform=None, limit=20, offset=0, exceptions_only=False,
                             tolerance_cents=10):
        full, claimed = self._payout_frame()
        m = _between(full.week_start, date_from, date_to)
        if location:
            m &= full.location == location
        if platform:
            m &= _has(full.platform, platform)
        w = self._settle(full[m], tolerance_cents)
        rows = self._exceptions(w, exceptions_only)
        out = _page(rows, ["location", "platform", "week_start", "complete_week", "orders", "subtotal_cents", "commission_pct",
                           "expected_commission_cents", "expected_payout_cents", "expected_payout_date", "expected_after_cutoff",
                           "match_status", "bank_ids", "bank_date", "deposit_cents", "difference_cents"], limit, offset)
        out["totals"] = {k: int(rows[k].sum()) for k in ["orders", "subtotal_cents", "expected_commission_cents", "expected_payout_cents"]}
        platforms = [p.upper() for p in self.terms()["platforms"] if not platform or platform.lower() in p.lower()]
        self._settlement_summary(out, w, claimed, lambda s: s.str.startswith(tuple(f"{p} PAYOUT WK " for p in platforms)) if platforms
                                 else s == "", date_from, date_to)
        out["matching"] = "each platform week is matched to its '<PLATFORM> PAYOUT WK MMDD' deposit (MMDD = week start), " \
                          f"deposited within 20 days of the week start; difference_cents = deposit - expected payout; matched means " \
                          f"within {tolerance_cents} cents"
        if not w.empty and not w.complete_week.all():
            out["incomplete_weeks"] = sorted(set(w[~w.complete_week].week_start))
        return out

    def load_bank_frame(self):
        """The bank shaped like bank_feed.csv (legacy column names)."""
        return self.bank.rename(columns={"amount_cents": "amount"})[
            ["bank_id", "account", "date", "nessie_type", "direction", "amount", "counterparty", "memo"]].copy()


# ---------------------------------------------------------------- tool specifications
def _str(desc, **kw):
    return {"type": "string", "description": desc, **kw}


def _int(desc, **kw):
    return {"type": "integer", "description": desc, **kw}


def _date(desc):
    return _str(desc + " (YYYY-MM-DD)", format="date")


def _limit(default):
    return _int(f"Rows per page (default {default}, at most {MAX_ROWS})", minimum=1, maximum=MAX_ROWS)


OFFSET = _int("Rows to skip, for paging: pass next_offset from the previous page (default 0)", minimum=0)
TOLERANCE = _int("Allowed difference from amount_cents (default 0 = exact)", minimum=0)
EXCEPTIONS = {"type": "boolean", "description": "Only rows that do not reconcile with the bank (match_status other than matched); "
              "default false"}


def _match_tolerance(default):
    return _int(f"Largest |difference_cents| still counted as matched (default {default})", minimum=0)
ACCOUNT = _str("Bank account code, e.g. CHS (Chestnut operating checking) or RES (reserve savings)")
PAGED = (" Paged: total_matches and the sums/totals cover every match, so limit=1 gives the totals for a range cheaply; "
         "when you need more rows and truncated is true, call again with offset=next_offset. Rows come in a stable order.")


def tool_specs(mode="historical"):
    """The 11 tools as {name, description, parameters} (JSON Schema)."""
    hist = mode == "historical"
    statuses = ["paid", "pending", "scheduled", "unknown"] if hist else ["paid", "pending", "scheduled"]
    status_text = (
        "`status` is what dated records prove at the cutoff: paid (a payment record on or before the cutoff: payroll run, owner "
        "draw, sales-tax paid date, recorded invoice payment, chargeback debit), pending (recorded payments do not cover the "
        "invoice), scheduled (a contract obligation falling due after the cutoff) or unknown (no payment record: not evidence "
        "of non-payment, check the bank). A status filter reports omitted_unknown_status, the matches it left out as unknown."
        if hist else
        "`status` is what the books say: paid (the books treat it as paid; autopays are assumed paid on the due date), pending "
        "(billed, unpaid when the books close), scheduled (future obligation from a lease or loan, no bill yet).")
    bills = ("Nessie bills: historical bill state has no dated snapshot, so this returns status 'unavailable'; use search_costs "
             "for obligations and search_bank_transactions for payments." if hist else
             "List the bills stored in Nessie: pending bills (supplier invoices, vendor bills and sales tax billed but unpaid when "
             "the bank data ends, nickname '<document id> due <date>') and recurring autopays (utilities, software, insurance, "
             "rent). Amounts in cents.")
    bank_id = "the native Nessie transaction id" if hist else "the bank transaction id, e.g. T1A2B3C4D5"
    specs = [
        ("get_bank_transaction",
         f"Look up one bank transaction by bank_id ({bank_id}). Returns account, date, type (deposit, withdrawal, purchase), "
         + ("" if hist else "direction (in/out), ") + "amount in cents, counterparty (purchases only), memo"
         + (" and status (deposits are money in; purchases and withdrawals are money out)" if hist else "") + ", plus "
         f"other transactions in the same account with exactly the same amount within 14 days (at most {SUPPLEMENT_ROWS}; "
         "useful for spotting duplicate charges and repeat standing orders).",
         {"bank_id": _str(f"The bank transaction id ({bank_id})")}, ["bank_id"]),
        ("search_bank_transactions",
         "Search bank transactions. Every filter is optional and they combine with AND. `text` matches the memo or counterparty, "
         "case-insensitive (e.g. 'MISSION DRY GOODS', 'TABLETAP PAYMENTS DEP', 'BRANCH DEPOSIT', 'PAYOUT WK'). Use amount_cents "
         "with amount_tolerance_cents to find amounts near a value, with a date window. Rows oldest first (date, account, type, "
         "amount, id), with the summed amount." + PAGED,
         {"account": ACCOUNT, "date_from": _date("Earliest date"), "date_to": _date("Latest date"),
          "direction": _str("Money in or out", enum=["in", "out"]),
          "nessie_type": _str("Nessie transaction type", enum=["deposit", "withdrawal", "purchase"]),
          "text": _str("Substring of the memo or counterparty"), "amount_cents": _int("Target amount in cents"),
          "amount_tolerance_cents": TOLERANCE, "limit": _limit(20), "offset": OFFSET}, []),
        ("get_payee_history",
         "Summarize a payee's bank history: every transaction whose memo or counterparty contains `text`, grouped by memo pattern "
         f"(digits shown as #, at most {SUPPLEMENT_ROWS} groups) with count, first and last date and min/median/max amount, plus "
         "the most recent rows. Use before_date to ask whether the restaurant had ever paid this payee, or this kind of charge, before.",
         {"text": _str("Payee or memo text, e.g. 'TABLETAP POS'"), "account": ACCOUNT,
          "before_date": _date("Only count transactions before this date"), "limit": _limit(10)}, ["text"]),
        ("get_bank_bills", bills,
         {"account": ACCOUNT, "status": _str("Bill status", enum=["pending", "recurring", "completed", "cancelled"])}, []),
        ("get_account_balances",
         "Cash per bank account as of a date: opening balance plus completed money in minus money out (Nessie never updates its "
         "stored balance, so this is the correct balance). Defaults to the " + ("investigation cutoff; dates after it are rejected."
                                                                               if hist else "last bank date."),
         {"as_of": _date("Balance at the end of this date")}, []),
        ("search_costs",
         "Search the Book: every cost in the restaurant's own records (supplier invoices, vendor bills, rent, payroll, loan payments, "
         "sales tax, repairs, permits, owner draws, card chargebacks). Filters are optional and combine with AND. `text` matches the "
         "cost id, payee or description, case-insensitive. " + status_text + " The bank is the proof of payment."
         + (" Rows are short (id, type, payee, dates, amount, credit, status); use get_cost for the description, line items and "
            "status basis." if hist else "") + " Rows are sorted by due date (document date when there is none), then id; also "
         "returns the summed amount" + (" and status_counts." if hist else ".") + PAGED,
         {"text": _str("Substring of the cost id, payee or description, e.g. 'Mission Dry Goods', 'salmon', 'MDG-CHS-10053'"),
          "cost_type": _str("Kind of cost", enum=["supplier_invoice", "vendor_bill", "rent", "payroll", "loan_payment", "sales_tax",
                                                  "repair", "misc_payable", "owner_draw", "chargeback"]),
          "category": _str("Substring of the category, e.g. 'Produce', 'Utilities', 'Occupancy'"),
          "status": _str("Payment status", enum=statuses),
          "location": _str("Restaurant location code, e.g. CHS"), "date_from": _date("Earliest date"), "date_to": _date("Latest date"),
          "date_field": _str("Which date the range applies to (default due_date)", enum=["due_date", "doc_date"]),
          "amount_cents": _int("Target document amount in cents"), "amount_tolerance_cents": TOLERANCE,
          "limit": _limit(20), "offset": OFFSET}, []),
        ("get_cost",
         "Full detail for one cost in the Book by cost_id: the cost, its line items (what was bought or what the charge is made of, "
         f"quantity, unit price in dollars; at most {LINE_ROWS}), installment plan (contractual due dates), credit memos, supplier "
         "terms, and the same payee's previous costs. Ids look like 'MDG-CHS-10053' (supplier invoice), 'VB-CHS-TP-2026-10' "
         "(vendor bill), 'RENT-CHS-2026-10', 'PAY-CHS-20261016', 'LOAN-2026-10', 'TAX-2026-09', 'DRAW-2026-10', "
         "'RPR-CHS-2026-06-0', 'MISC-CHS-20260115', 'CB-CHS-0121'.",
         {"cost_id": _str("The cost id")}, ["cost_id"]),
        ("get_agreements",
         "Contract terms from the books: card processor fees (percent plus fixed cents per transaction, settlement timing), delivery "
         "platform commission rates (percent), supplier payment terms and delivery days, the lease (rent, escalator) and the loan.",
         {"counterparty": _str("Only agreements whose counterparty contains this text, e.g. 'TableTap', 'Bay Produce'")}, []),
        ("get_card_batches",
         "POS card batches from the books for a date range (batch date = sale date), each matched in code to its bank deposit: "
         "gross card sales, refunds, transaction count, the deposit the processor agreement implies (expected fee and net) with the "
         "expected settlement date, and the matched deposit (bank_ids, bank_date, deposit_cents, difference_cents = deposit - "
         "expected net) with match_status: matched (within tolerance_cents), amount_mismatch, missing (no deposit though due by "
         "the cutoff), not_yet_due or multiple (more than one deposit for the batch). match_counts and difference_total_cents "
         "cover the whole range; unmatched_deposits lists processor deposits in the range that no batch accounts for. Use "
         "exceptions_only=true to see only the batches that do not reconcile. Also lists card chargebacks in the range "
         f"(at most {SUPPLEMENT_ROWS}; outcomes not known by the cutoff are null)." + PAGED,
         {"date_from": _date("First batch date"), "date_to": _date("Last batch date"), "location": _str("Location code, e.g. CHS"),
          "exceptions_only": EXCEPTIONS, "tolerance_cents": _match_tolerance(10), "limit": _limit(31), "offset": OFFSET},
         ["date_from", "date_to"]),
        ("get_cash_deposit_slips",
         "Cash deposit slips from the books for a date range, each matched in code to one 'BRANCH DEPOSIT' bank deposit: cash "
         "the POS recorded, cash counted and taken to the bank, the over/short at the till, and the matched deposit (bank_ids, "
         "bank_date, deposit_cents, difference_cents = deposit - counted cash) with match_status (matched, amount_mismatch, missing, "
         "not_yet_due). match_counts and difference_total_cents cover the whole range; unmatched_deposits lists branch deposits "
         "with no slip. Use exceptions_only=true to see only the slips that do not reconcile. "
         + ("" if hist else "When the range reaches the end of the books, also shows cash not yet deposited. ") + PAGED.strip(),
         {"date_from": _date("First deposit date"), "date_to": _date("Last deposit date"), "location": _str("Location code, e.g. CHS"),
          "exceptions_only": EXCEPTIONS, "tolerance_cents": _match_tolerance(0), "limit": _limit(31), "offset": OFFSET},
         ["date_from", "date_to"]),
        ("get_delivery_payouts",
         "Weekly delivery-platform sales from the books' POS tickets (weeks start Monday), for weeks starting in the date range, "
         "each matched in code to its '<PLATFORM> PAYOUT WK MMDD' bank deposit: order count, subtotal, the commission rate in the "
         "platform agreement, the expected commission, payout and payout date, and the matched deposit (bank_ids, bank_date, "
         "deposit_cents, difference_cents = deposit - expected payout) with match_status (matched, amount_mismatch, missing, "
         "not_yet_due, multiple). complete_week is false when the week runs past the cutoff (partial sales). match_counts and "
         "difference_total_cents cover the whole range; unmatched_deposits lists payout deposits no week accounts for. Use "
         "exceptions_only=true to see only the weeks that do not reconcile." + PAGED,
         {"date_from": _date("Earliest week start"), "date_to": _date("Latest week start"), "location": _str("Location code, e.g. CHS"),
          "platform": _str("Platform name, e.g. RapidPlate or UrbanBite"), "exceptions_only": EXCEPTIONS,
          "tolerance_cents": _match_tolerance(10), "limit": _limit(20), "offset": OFFSET}, ["date_from", "date_to"]),
    ]
    return [{"name": n, "description": d, "parameters": {"type": "object", "properties": p, "required": list(r), "additionalProperties": False}}
            for n, d, p, r in specs]


_SPECS = {mode: {s["name"]: s for s in tool_specs(mode)} for mode in ("historical", "legacy")}
assert all(set(specs) == set(GROUP_OF) for specs in _SPECS.values())


def asi_tool_definitions(mode="historical"):
    """OpenAI-style function tools, as the ASI chat completions API takes them."""
    return [{"type": "function", "function": s} for s in tool_specs(mode)]


def claude_tool_definitions(mode="legacy"):
    """Anthropic Messages API tool definitions (the standalone format)."""
    return [{"name": s["name"], "description": s["description"], "input_schema": s["parameters"]} for s in tool_specs(mode)]


def _validate(spec, args):
    if not isinstance(args, dict):
        return "tool input must be a JSON object"
    schema = spec["parameters"]
    props = schema["properties"]
    for k in schema["required"]:
        if args.get(k) in (None, ""):
            return f"missing required field '{k}'"
    for k, v in list(args.items()):
        if k not in props:
            return f"unknown field '{k}'; allowed: {', '.join(props)}"
        p = props[k]
        if v is None:
            continue
        if p["type"] == "boolean":
            if not isinstance(v, bool):
                return f"'{k}' must be true or false"
        elif p["type"] == "integer":
            if isinstance(v, float) and v.is_integer():
                args[k] = v = int(v)
            if not isinstance(v, int) or isinstance(v, bool):
                return f"'{k}' must be an integer"
            if "minimum" in p and v < p["minimum"]:
                return f"'{k}' must be at least {p['minimum']}"
            if "maximum" in p and v > p["maximum"]:
                return f"'{k}' must be at most {p['maximum']}"
        elif not isinstance(v, str):
            return f"'{k}' must be a string"
        elif "enum" in p and v not in p["enum"]:
            return f"'{k}' must be one of: {', '.join(p['enum'])}"
        elif p.get("format") == "date":
            try:
                date.fromisoformat(v)
            except ValueError:
                return f"'{k}' must be a date like 2026-08-01"
            if len(v) != 10:
                return f"'{k}' must be a date like 2026-08-01"
    if args.get("date_from") and args.get("date_to") and args["date_from"] > args["date_to"]:
        return "date_from is after date_to"
    return None


def succeeded(result):
    """True when a tool result is usable evidence: not an error and not an unavailable-history answer."""
    return isinstance(result, dict) and "error" not in result and result.get("status") != "unavailable"
