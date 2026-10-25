"""CLI: run a scan, serve the uAgent, send a request as a local client, or print a saved report as a table."""
import argparse
import asyncio
import json
import logging
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

from .contracts import MarketReport, MarketRequest
from .runner import MarketScout
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
        if command == "client":
            p.add_argument("--target", required=True, help="market scout agent address")
            p.add_argument("--port", type=int, default=8004)
            p.add_argument("--endpoint", default="http://127.0.0.1:8003/submit")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8003)
    serve.add_argument("--local-only", action="store_true",
                       help="without MARKET_SCOUT_ALLOWED_SENDERS: take requests only from this machine's local client")
    show = commands.add_parser("show")
    show.add_argument("report", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    for env in (root / ".env", root.parent / ".env"):       # nessie_lab/.env, then BackHouse/.env (the Nessie key)
        load_dotenv(env, override=False)
    try:
        if args.command == "serve":
            from .agent import make_agent
            make_agent(root, args.port, local_only=args.local_only).run()
        elif args.command == "show":
            path = args.report if args.report.is_absolute() else root / args.report
            print(table(MarketReport.model_validate_json(path.read_text(encoding="utf-8"))))
        else:
            request = MarketRequest(request_id=args.request_id, items=args.items, service=args.service,
                                    as_of=args.as_of, refresh=args.refresh)
            if args.command == "client":
                from .agent import run_client
                run_client(args.target, request, args.port, args.endpoint, root)
            else:
                result = asyncio.run(MarketScout(root).run(request))
                print(json.dumps(result.model_dump(mode="json"), indent=2))       # JSON escapes control characters
                print(table(MarketReport.model_validate_json((root / result.filename).read_text(encoding="utf-8"))))
    except Exception as exc:
        parser.exit(1, f"Market scout failed ({type(exc).__name__}): {safe(exc)}\n")


if __name__ == "__main__":
    main()
