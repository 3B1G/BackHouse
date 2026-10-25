"""CLI for report generation and verification, serving, local orchestration, and offline flag export."""
import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from dotenv import load_dotenv

from .contracts import Report, ReportRequest
from .runner import Bookkeeper, immutable_json
from .tools import read_database
from .verifier import Verifier, verify_in_process


def export_flags(root: Path, report_path: Path, output: Path):
    """Translate report IDs for grade.py only; never read its answer key."""
    report = Report.model_validate_json(report_path.read_text())
    snapshot = (root / report.sources.bank_snapshot).resolve()
    if not snapshot.is_relative_to((root / "data").resolve()) or "grader" in snapshot.parts:
        raise ValueError("Report snapshot must be under data/, outside grader/")
    with read_database(snapshot) as con:
        mapping = {r[0]: r[1] for r in con.execute("SELECT nessie_id, bank_id FROM bank_tx")}
    flags, seen = [], set()
    for finding in report.findings:
        treatment = finding.treatment
        ids = (treatment.bank_ids if treatment.kind == "exclude" else
               [treatment.bank_id] if treatment.kind == "correct_to" else finding.bank_ids)
        refs = [("bank_id", mapping[i]) for i in ids if mapping.get(i)]
        if not ids:
            refs += [("books_ref", ref) for ref in finding.books_refs]
        for key, value in refs:
            if (key, value) not in seen:
                flags.append({key: value})
                seen.add((key, value))
    immutable_json(output, flags)


async def verified(root: Path, request: ReportRequest | None = None, trace_id: str | None = None):
    """Run or resume the Bookkeeper, then verify its draft in this process; no agents are needed."""
    bookkeeper = Bookkeeper(root, model=os.getenv("ASI_MODEL", "asi1"))
    outcome = await (bookkeeper.run(request) if request else bookkeeper.resume(trace_id))
    return await verify_in_process(bookkeeper, Verifier(root), outcome)


def main():
    """Run the selected command with repository-relative data and shared report paths."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[3])
    commands = parser.add_subparsers(dest="command", required=True)
    for command in ["run", "client"]:
        p = commands.add_parser(command)
        p.add_argument("--request-id", required=True)
        p.add_argument("--as-of")
        p.add_argument("--tag", default="boh-1loc")
        if command == "client":
            p.add_argument("--target", required=True, help="Bookkeeper agent address")
            p.add_argument("--port", type=int, default=8002)
            p.add_argument("--endpoint", default="http://127.0.0.1:8001/submit")
    serve = commands.add_parser("serve")
    serve.add_argument("--port", type=int, default=8001)
    serve_verifier = commands.add_parser("serve-verifier")
    serve_verifier.add_argument("--port", type=int, default=8003)
    verify = commands.add_parser("verify")
    verify.add_argument("--report-id", required=True, help="R-... of a saved draft under data/drafts")
    resume = commands.add_parser("resume")
    resume.add_argument("--trace-id", required=True)
    export = commands.add_parser("export-flags")
    export.add_argument("report", type=Path)
    export.add_argument("output", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    load_dotenv(root / ".env", override=False)
    try:
        if args.command == "serve":
            from .agent import make_agent
            make_agent(root, args.port).run()
        elif args.command == "serve-verifier":
            from .agent import make_verifier_agent
            make_verifier_agent(root, args.port).run()
        elif args.command == "resume":
            result = asyncio.run(verified(root, trace_id=args.trace_id))
            print(result.model_dump_json(indent=2))
        elif args.command == "verify":
            bookkeeper = Bookkeeper(root, model=os.getenv("ASI_MODEL", "asi1"))
            drafted = bookkeeper.latest_draft(args.report_id)
            if drafted is None:
                raise ValueError(f"No saved draft for {args.report_id}")
            result = asyncio.run(verify_in_process(bookkeeper, Verifier(root), drafted))
            print(result.model_dump_json(indent=2))
        elif args.command == "export-flags":
            export_flags(root, args.report, args.output)
        else:
            request = ReportRequest(request_id=args.request_id, as_of=args.as_of, nessie_tag=args.tag)
            if args.command == "client":
                from .agent import run_client
                run_client(args.target, request, args.port, args.endpoint)
            else:
                result = asyncio.run(verified(root, request))
                print(result.model_dump_json(indent=2))
    except Exception as exc:
        parser.exit(1, f"Bookkeeper failed ({type(exc).__name__}): {exc}\n")


if __name__ == "__main__":
    main()
