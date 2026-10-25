"""CLI: run a scan, serve the uAgent, send a request as a local client, print a saved report as a table, or list
which competitor menu pages have a saved copy (`pages`, to check the cache before an offline demo)."""
import argparse
import asyncio
import json
import logging
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

from .contracts import MarketReport, MarketRequest
from .runner import MarketScout, env_offline
from .shared import DEFAULT_ROOT
from .text import safe


def table(report: MarketReport) -> str:
    """The report as text for a terminal. Every string from the report is passed through safe(): a report file (or a
    restaurant name in it) can carry terminal escape sequences."""
    rows = [f"{safe(report.our_restaurant)} vs {sum(c.relevant for c in report.competitors)} competitors "
            f"({safe(report.service)}, menu of {report.menu_as_of}, at par = within {report.par_band:.0%} of the median)",
            f"{'dish':28} {'ours':>7} {'median':>7} {'gap':>7} {'n':>3}  position"]
    for p in report.positions:
        fmt = lambda c: f"${c / 100:,.2f}" if c is not None else "-"
        rows.append(f"{safe(p.our_item):28} {fmt(p.our_price_cents):>7} {fmt(p.competitor_median_cents):>7} "
                    f"{(f'{p.gap_pct:+.0%}' if p.gap_pct is not None else '-'):>7} {p.n_prices:>3}  "
                    f"{safe(p.position)}" + (f" ({p.n_unreviewed} unreviewed)" if p.n_unreviewed else ""))
        if p.upcoming_price_cents is not None:
            rows.append(f"{'':28} approved: {fmt(p.upcoming_price_cents)} from {p.upcoming_from} -> "
                        f"{safe(p.upcoming_position)}")
        if p.leaving_from is not None:
            rows.append(f"{'':28} approved: leaving the menu on {p.leaving_from}")
    return "\n".join(rows + [f"note: {safe(x)}" for x in report.limitations])


def pages_table(status, now=None) -> tuple[str, int]:
    """-> (text, number of pages with no saved copy) for `market-scout pages`."""
    now = now or datetime.now(timezone.utc)
    rows, missing = [], 0
    for name, url, meta in status:
        if meta is None:
            missing += 1
            state = "MISSING"
        else:
            at = meta["retrieved_at"]
            at = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            hours = (now - at).total_seconds() / 3600
            state = f"saved {at.astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC ({hours:,.0f} h old, {meta['kind']})"
        rows.append(f"{safe(name)[:28]:28} {state:44} {safe(url)[:120]}")
    rows.append(f"{len(status) - missing} of {len(status)} menu pages have a saved copy"
                + (f"; {missing} missing (an offline scan lists them as not read)" if missing else ""))
    return "\n".join(rows), missing


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(prog="market-scout")
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT, help="nessie_lab directory (holds data/cost_price.db)")
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ["run", "client"]:
        p = commands.add_parser(command)
        p.add_argument("--request-id", required=True)
        p.add_argument("--items", nargs="*", default=[], help="our dish names; default: every comparable dish")
        p.add_argument("--service", choices=["dinner", "lunch"], default="dinner")
        p.add_argument("--as-of", type=date.fromisoformat, default=None, help="our menu on this date (default today)")
        p.add_argument("--refresh", action="store_true",
                       help="re-fetch menu pages whose saved snapshot is more than 6 hours old")
        p.add_argument("--offline", action="store_true",
                       help="saved menu pages only: no fetch, no Apify, no ASI (also MARKET_SCOUT_OFFLINE=1)")
        if command == "client":
            p.add_argument("--target", required=True, help="market scout agent address")
            p.add_argument("--port", type=int, default=8004)
            p.add_argument("--endpoint", default="http://127.0.0.1:8003/submit")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8003)
    serve.add_argument("--local-only", action="store_true",
                       help="without MARKET_SCOUT_ALLOWED_SENDERS: take requests only from this machine's local client")
    serve.add_argument("--offline", action="store_true",
                       help="every scan uses saved menu pages only; requests can't turn it off (also MARKET_SCOUT_OFFLINE=1)")
    commands.add_parser("pages", help="list the competitor menu pages a scan reads and whether each has a saved copy")
    show = commands.add_parser("show")
    show.add_argument("report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    for env in (root / ".env", root.parent / ".env"):       # nessie_lab/.env, then BackHouse/.env (the Nessie key)
        load_dotenv(env, override=False)
    try:
        if args.command == "serve":
            from .agent import make_agent
            make_agent(root, args.port, local_only=args.local_only, offline=args.offline or env_offline()).run()
        elif args.command == "pages":
            text, missing = pages_table(MarketScout(root, offline=True).page_status())
            print(text)
            if missing:
                sys.exit(1)
        elif args.command == "show":
            path = args.report if args.report.is_absolute() else root / args.report
            print(table(MarketReport.model_validate_json(path.read_text(encoding="utf-8"))))
        else:
            offline = args.offline or env_offline()
            request = MarketRequest(request_id=args.request_id, items=args.items, service=args.service,
                                    as_of=args.as_of, refresh=args.refresh, offline=offline)
            if args.command == "client":
                from .agent import run_client
                run_client(args.target, request, args.port, args.endpoint, root)
            else:
                result = asyncio.run(MarketScout(root, offline=offline).run(request))
                print(json.dumps(result.model_dump(mode="json"), indent=2))       # JSON escapes control characters
                print(table(MarketReport.model_validate_json((root / result.filename).read_text(encoding="utf-8"))))
    except Exception as exc:
        parser.exit(1, f"Market scout failed ({type(exc).__name__}): {safe(exc)}\n")


if __name__ == "__main__":
    main()
