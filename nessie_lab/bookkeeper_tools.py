"""Read-only tools for the Bookkeeper agent: the bank (Nessie) and the Book (books.db), as Claude tool definitions.

Standalone compatibility wrapper. The one maintained implementation is bookkeeper/src/bookkeeper/bookkeeper_tools.py,
packaged with Bookkeeper; this module runs it in its legacy mode, the books as they close:
  bank  data/nessie_sync_<tag>.db  Nessie, pulled by nessie_sync.py (tag from $BOH_NESSIE_TAG, default boh-1loc)
  book  data/books.db              the restaurant's documents; the Book (costs, line items, status) is projected in memory
Bank rows carry the synthetic bank_id that grade.py scores flags by (Bookkeeper itself only ever sees native Nessie ids).
Statuses are the Book's (open AP is pending, autopays are paid). Never reads data/grader/ and never shows Nessie's |ref=
tags. Amounts are integer cents, dates YYYY-MM-DD.

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
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "bookkeeper" / "src"))

import config as C  # noqa: E402
from bookkeeper import bookkeeper_tools as _impl  # noqa: E402
from bookkeeper.bookkeeper_tools import BANK_HOLIDAYS, MAX_ROWS, WEEKDAYS, next_bday  # noqa: E402,F401

D = Path(__file__).parent / "data"
TAG = os.environ.get("BOH_NESSIE_TAG", "boh-1loc")
BOOK_DB, BANK_DB = D / "books.db", D / f"nessie_sync_{TAG}.db"
START, END = str(C.START), str(C.END)
TOOLS = _impl.claude_tool_definitions("legacy")
SPECS = {t["name"]: t for t in TOOLS}
_session = None


def session():
    """The shared legacy session, loaded on first use (reload() to pick up regenerated data)."""
    global _session
    if _session is None:
        _session = _impl.ToolSession(D.parent, TAG, cutoff=C.END, mode="legacy", window_start=C.START,
                                     sales_tax_rate=C.SALES_TAX)
    return _session


def reload():
    """Forget the loaded tables (after re-running generate.py, build_book.py or nessie_sync.py)."""
    global _session
    _session = None


def load_bank_frame():
    return session().load_bank_frame()


def bank_id_map():
    """{Nessie transaction id: bank_id}, for converting reports that name Nessie's own ids (contracts.from_bk_report).
    The union of the committed upload manifest and the local Nessie sync, so a report converts whichever Nessie load
    it came from, as long as that load's ids are here; the sync wins on a clash."""
    manifest = D / f"nessie_manifest_{TAG}.json"
    if not BANK_DB.exists() and not manifest.exists():
        raise FileNotFoundError(f"neither {BANK_DB.name} nor {manifest.name} exists: run python nessie_sync.py --tag {TAG}")
    ids = {nid: bid for bid, nid in json.loads(manifest.read_text())["tx"].items()} if manifest.exists() else {}
    if BANK_DB.exists():
        bank = session().bank                     # legacy mode keeps both ids
        ids.update(zip(bank.nessie_id, bank.bank_id))
    return ids


def _tool_function(name):
    def tool(*args, **kwargs):
        return getattr(session(), name)(*args, **kwargs)
    tool.__name__ = tool.__qualname__ = name
    tool.__doc__ = SPECS[name]["description"]
    return tool


FUNCS = {name: _tool_function(name) for name in SPECS}
globals().update(FUNCS)


def call_tool(name, args):
    """Run one tool by name. Never raises: problems come back as {"error": ...} so the agent can correct itself."""
    return session().call(name, args)


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
            ("get_bank_bills", {"status": "pending"}),
            ("search_costs", {"cost_type": "nope"})]
    for name, args in demo:
        print(f"> {name}({json.dumps(args)})\n{json.dumps(call_tool(name, args), default=str)[:700]}\n")
