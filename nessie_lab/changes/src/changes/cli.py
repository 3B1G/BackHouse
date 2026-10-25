"""CLI. `approve` is the only way to produce an Approval and it asks a person: the owner passphrase unlocks the key
that signs the decision. That passphrase is the real gate; cost_price.tty.read_passphrase also refuses piped or
redirected stdin (including `< NUL` on Windows), which only raises the bar for scripts. Agents never call it.

  changes approve s.json [--apply] [--live]           -> passphrase, shows the subject, y/N on a question line
                                                          built from the plan (the AI's title is only quoted)
  changes approve --revert C-S1-1 [--apply] [--live]   -> the same for undoing one change
  changes apply s.json                                 -> dry run: the planned changes, nothing written
  changes apply s.json --live                          -> apply an approved suggestion (no passphrase needed:
                                                          the stored signature is checked with the public key)
  changes revert C-S1-1 [--live]
  changes log | changes pending | changes menu [--on YYYY-MM-DD]
                                                       pending: bill moves with an intent but no change_log row
"""
import argparse
import json
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from cost_price import tty
from cost_price.db import connect
from cost_price.owner import unlock

from .executor import Executor, Refused, receipt_name, subject_id, usd, valid_id
from .shared import DEFAULT_ROOT, Approval, Suggestion


def read_key(db: Path):
    """The owner key, from a passphrase typed at a console (cost_price.tty refuses pipes, files and NUL)."""
    passphrase = tty.read_passphrase("Owner passphrase: ")
    with connect(db) as con:
        return unlock(con, passphrase)


def approval_file(id_: str) -> Path:
    """The receipt for `id_` in the current directory (see executor.receipt_name), never outside it."""
    name = receipt_name(id_)
    cwd = Path.cwd().resolve()
    out = (cwd / name).resolve()
    if out.parent != cwd or out.name != name:
        raise Refused(f"approval file for {id_!r} would leave the current directory")
    return out


CLAIMS = (("monthly_cash_effect_cents", "monthly cash effect"), ("one_time_cash_effect_cents", "one-time cash effect"),
          ("run_out_likely_before", "run out likely before"), ("run_out_likely_after", "run out likely after"))


def _signed_usd(cents: int) -> str:
    return ("-" if cents < 0 else "+") + usd(abs(cents))


def _md(day: str) -> str:
    return day[5:]                                                # 2026-10-24 -> 10-24


def question(shown: dict, ops: list[dict] | None, today: date) -> str:
    """The change in one line, built from the fresh plan (the numbers and dates CH will write), never from the title."""
    p = shown.get("planned_change") or {}
    if "revert" in shown:
        cid, op = shown["revert"], ops[0]
        if op["kind"] == "price":
            return f"Undo {cid}: {op['item']} back to {usd(p['price_cents'])} from {p['effective_from']}"
        if op["kind"] == "availability":
            where = "back on the menu" if p["available"] else "off the menu again"
            return f"Undo {cid}: {op['item']} {where} from {p['effective_from']}"
        if op["kind"] == "nessie":
            return f"Undo {cid}: bill {op['bill_id']} paid on {op['to']} again (now {op['expect']})"
        return f"Undo {cid}: dispute of {op['bank_id']} back to '{op['state']}'"
    action, target = shown["action"], shown["target"]
    if action == "reprice_item":
        where = f"{p['channel'].replace('_', '-')} {p['service']}"
        old = ops[0]["change"].old_value
        return f"{p['item']} ({where}) {usd(old)} -> {usd(p['price_cents'])} from {p['effective_from']}"
    if action == "remove_item":
        return f"Take {p['item']} off the menu from {p['effective_from']}"
    if action == "stretch_supplier":
        bills = shown["planned_bills"]
        days = {(date.fromisoformat(b["to_date"]) - date.fromisoformat(b["from_date"])).days for b in bills}
        later = f"{days.pop()} days later" if len(days) == 1 else "later"
        moves = ", ".join(f"{_md(b['from_date'])} -> {_md(b['to_date'])}" for b in bills)
        return f"{target}: {len(bills)} bill{'s' if len(bills) != 1 else ''} paid {later} ({moves})"
    if action == "pay_bill":
        to = shown["planned_bills"][0]["to_date"]
        return f"Pay {target} " + (f"today ({to})" if to == today.isoformat() else f"on {to}")
    if action == "dispute_charge":
        return f"Dispute {target}"
    if action == "other" and str(shown.get("suggestion_id", "")).startswith("review-"):
        n = len(shown.get("assumptions") or [])
        return f"Commit the owner's review of {target} ({n} decisions, listed above; nothing is applied)"
    return f"{action} on {target} (CH cannot apply this action)"


def ask_owner(sid: str, shown: dict, line: str, title: str | None = None) -> Approval:
    """The question line comes from the plan. The AI's title and its forecast claims are shown, labelled, but the
    claims are signed as part of the subject because the owner saw them."""
    print(json.dumps(shown, indent=2))
    if title is not None:
        print(f"AI's title: {json.dumps(title)}")
    claims = [f"{label} {_signed_usd(shown[k]) if k.endswith('_cents') else shown[k]}"
              for k, label in CLAIMS if shown.get(k) is not None]
    if claims:
        print("AI/forecast claims, not checked by CH: " + "; ".join(claims))
    answer = input(f"Approve: {line}? [y/N] ").strip().lower()
    note = input("Note (optional): ").strip() or None
    return Approval(suggestion_id=sid, decision="approved" if answer == "y" else "rejected",
                    decided_at=datetime.now(timezone.utc), note=note)


def load_suggestion(path: Path) -> Suggestion:
    s = Suggestion.model_validate_json(path.read_text(encoding="utf-8"))
    valid_id(s.suggestion_id, "suggestion_id")
    return s


def main(argv=None):
    parser = argparse.ArgumentParser(prog="changes")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="nessie_lab directory")
    parser.add_argument("--tag", default="boh-1loc", help="Nessie tag whose sync DB lists the bills")
    commands = parser.add_subparsers(dest="command", required=True)
    p = commands.add_parser("approve")
    p.add_argument("suggestion", type=Path, nargs="?")
    p.add_argument("--revert", metavar="CHANGE_ID")
    p.add_argument("--apply", action="store_true", help="apply right after approving")
    p.add_argument("--live", action="store_true", help="with --apply: actually write (default: dry run)")
    p = commands.add_parser("apply")
    p.add_argument("suggestion", type=Path)
    p.add_argument("--live", action="store_true", help="actually write (default: dry run)")
    p = commands.add_parser("revert")
    p.add_argument("change_id")
    p.add_argument("--live", action="store_true")
    commands.add_parser("log")
    commands.add_parser("pending", help="bill moves CH started (intent) but never logged")
    p = commands.add_parser("menu")
    p.add_argument("--on", default=date.today().isoformat())
    args = parser.parse_args(argv)
    root = args.root.resolve()
    db = root / "data" / "cost_price.db"
    for env in (root / ".env", root.parent / ".env"):       # nessie_lab/.env, then BackHouse/.env (the Nessie key)
        load_dotenv(env, override=False)
    try:
        if args.command == "approve":
            if bool(args.revert) == bool(args.suggestion):
                raise Refused("give either a suggestion file or --revert CHANGE_ID")
            if args.revert:
                valid_id(args.revert, "change_id")
                subject = {"revert": args.revert}
            else:
                subject = load_suggestion(args.suggestion)
            sid = subject_id(subject)
            out = approval_file(sid)
            key = read_key(db)                                       # before anything is shown or asked
            ex = Executor(root, key, tag=args.tag)
            shown, ops = ex.plan(subject)                            # the subject and the fresh plan behind it
            line = question(shown, ops, ex.today)                    # from the plan, never from the AI's title
            if args.revert:
                change = [r for r in ex.log() if r["change_id"] == args.revert]
                if not change:
                    raise Refused(f"no change {args.revert}")
                print(json.dumps(change[0], indent=2))
                a = ask_owner(sid, shown, line)
            else:
                a = ask_owner(sid, shown, line, subject.title)
            ex.record_approval(subject, a, shown=shown)              # signed: exactly what was shown
            out.write_text(json.dumps(dict(a.model_dump(mode="json"), subject=shown), indent=2), encoding="utf-8")
            print(f"{a.decision}; recorded in {db.name}, receipt {out.name}")
            if args.apply and a.decision == "approved":
                r = ex.revert(args.revert, live=args.live) if args.revert else ex.apply(subject, live=args.live)
                print(r.model_dump_json(indent=2))
        elif args.command in ("apply", "revert"):
            ex = Executor(root, tag=args.tag)                        # no key: the approval is checked, not signed
            if args.command == "apply":
                r = ex.apply(load_suggestion(args.suggestion), live=args.live)
            else:
                r = ex.revert(valid_id(args.change_id, "change_id"), live=args.live)
            print(r.model_dump_json(indent=2))
        elif args.command == "log":
            for row in Executor(root, tag=args.tag).log():
                print(json.dumps(row))
        elif args.command == "pending":
            rows = Executor(root, tag=args.tag).pending()
            for row in rows:
                print(json.dumps(row))
            if not rows:
                print("no unlogged bill moves")
        else:
            for item, cents in sorted(Executor(root, tag=args.tag).menu(args.on).items()):
                print(f"{item:28} ${cents / 100:,.2f}")
    except (PermissionError, Refused, FileNotFoundError, ValueError, sqlite3.Error) as exc:
        parser.exit(1, f"changes: {exc}\n")


if __name__ == "__main__":
    main()
