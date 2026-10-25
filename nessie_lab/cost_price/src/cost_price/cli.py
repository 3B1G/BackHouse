"""cost-price build | verify | seal | heads | owner-init | menu | market | competitors"""
import argparse
import json
import sys
from datetime import date
from pathlib import Path

from . import api
from .build import build, compare_with_features
from .db import TamperError, connect, heads
from .owner import OwnerError, init_owner, unlock
from .tty import NotATerminal, read_passphrase
from .verify import seal, verify

DEFAULT_ROOT = Path(__file__).resolve().parents[3]              # nessie_lab/


def main():
    parser = argparse.ArgumentParser(prog="cost-price")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="nessie_lab directory (holds data/books.db)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build", help="create or refresh data/cost_price.db from books.db and the seed list")
    sub.add_parser("verify", help="schema, chains, signatures, change coverage, seal")
    sub.add_parser("seal", help="owner signs the current state, so later rewrites are detectable")
    sub.add_parser("heads", help="print the last id and hash of every chain")
    sub.add_parser("owner-init", help="create the owner signing key (once), protected by a passphrase")
    p = sub.add_parser("menu")
    p.add_argument("--on", default=date.today().isoformat())
    p.add_argument("--service", choices=["dinner", "lunch"], default="dinner")
    p = sub.add_parser("market")
    p.add_argument("--service", choices=["dinner", "lunch"], default="dinner")
    p.add_argument("--on", default=date.today().isoformat(), help="the menu date the scan compared (default today)")
    sub.add_parser("competitors")
    args = parser.parse_args()
    root = args.root.resolve()
    db = root / "data" / "cost_price.db"
    try:
        if args.command == "build":
            print(json.dumps(build(root), indent=2))
            diff = compare_with_features(root)
            print("plate costs match features.db" if not diff else f"plate cost differences ({len(diff)}):\n" +
                  "\n".join(diff[:10]))
        elif args.command == "verify":
            out = verify(db)
            for line in out["problems"]:
                print(f"PROBLEM: {line}")
            for line in out["notes"]:
                print(f"note: {line}")
            if out["fingerprint"]:
                print(f"owner key fingerprint: {out['fingerprint']}  (must match what owner-init printed)")
            print("ok" if not out["problems"] else f"{len(out['problems'])} problem(s)")
            sys.exit(1 if out["problems"] else 0)
        elif args.command == "seal":
            with connect(db) as con:
                key = unlock(con, read_passphrase())
            doc = seal(db, key)
            print(f"sealed at {doc['sealed_at']}: " + ", ".join(f"{t} {h['id']}" for t, h in doc["heads"].items()))
        elif args.command == "heads":
            with connect(db) as con:
                print(json.dumps(heads(con), indent=2))
        elif args.command == "owner-init":
            first = read_passphrase("New owner passphrase (10+ characters): ")
            if read_passphrase("Repeat it: ") != first:
                raise OwnerError("passphrases differ")
            with connect(db, "owner") as con:
                fp = init_owner(con, first)
            print(f"owner key created. Fingerprint: {fp}\nWrite it down: verify prints it, and it must match. "
                  "The passphrase is not stored anywhere, so don't lose it.")
        elif args.command == "menu":
            with connect(db) as con:
                prices, _ = api.menu(con, args.on, service=args.service)
            for item, cents in prices.items():
                print(f"{item:28} ${cents / 100:,.2f}")
        elif args.command == "market":
            with connect(db) as con:
                for r in api.latest_market(con, args.service, args.on):
                    med = f"${r['competitor_median_cents'] / 100:,.2f}" if r["competitor_median_cents"] else "-"
                    print(f"{r['our_item']:28} ${r['our_price_cents'] / 100:>7,.2f} {med:>8} {r['n_prices']:>3}  "
                          f"{r['position']}")
        else:
            with connect(db) as con:
                for c in api.competitors(con):
                    print(f"{c['id']:>3} {c['domain']:32} {c['price_level'] or '?':5} {c['distance_miles'] or '?':>5} mi  "
                          f"{c['name']}")
    except (OwnerError, NotATerminal, TamperError, FileNotFoundError, ValueError, LookupError, RuntimeError,
            PermissionError) as exc:
        parser.exit(1, f"cost-price: {exc}\n")


if __name__ == "__main__":
    main()
