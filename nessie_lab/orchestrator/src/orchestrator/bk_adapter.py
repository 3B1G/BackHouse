"""The ONE place that knows the Bookkeeper's report and request shapes (bk branch, report schema 1.1).

Today's Bookkeeper writes a FULL-window snapshot on every run (window {start, end}, end = as_of = the latest bank
date), renumbers finding ids F1..Fn each time, and its ReportRequest forbids extra fields. Incremental support
(a "delta" report covering only the newest days) is not shipped yet, so every field name for it below is a GUESS.

UPDATE THE CONSTANTS BELOW WHEN THE BOOKKEEPER OWNER CONFIRMS THE REAL NAMES; nothing else in the orchestrator
reads these fields directly (bk_state, bk_runner, tools and cli all go through this module)."""
import hashlib
import os
from datetime import date

# ---------------------------------------------------------------- field names (guesses: update when confirmed)
MODE_FIELD = "mode"                       # report["mode"]: "full" | "delta"
DELTA_MODE = "delta"
SINCE_FIELD = "since"                     # request["since"] / report["window"]["since"]: first day a delta covers
PRIOR_FIELD = "prior_report_id"           # request["prior_report_id"]: the report a delta continues
COVERAGE_FIELD = "coverage"               # report["coverage"]["window"] = {start, end}: the days a delta re-examined
COVERAGE_WINDOW_FIELD = "window"
FINDING_KEY_FIELD = "finding_key"         # finding["finding_key"]: a stable id across runs
STATUS_FIELD = "status"                   # finding["status"]: "open" | "resolved" | "superseded"
CLOSED_STATUSES = ("resolved", "superseded")
STATUSES = ("open",) + CLOSED_STATUSES

# CLI flags of `bookkeeper run` for the incremental request (guesses as well)
SINCE_FLAG = "--since"
PRIOR_FLAG = "--prior-report-id"

INCREMENTAL_ENV = "ORCHESTRATOR_BK_INCREMENTAL"   # 1 = send since / prior_report_id (the Bookkeeper accepts them)
AUTO_ENV = "ORCHESTRATOR_BK_AUTO"                 # 1 = a turn may run a quick DELTA update first (never a full one)

SCHEMA_VERSION = "1.1"
TREATMENT_KINDS = ("exclude", "correct_to", "ongoing", "owed")


def _flag(name: str, environ=None) -> bool:
    return str((os.environ if environ is None else environ).get(name, "")).strip().lower() in ("1", "true", "yes", "on")


def incremental_enabled(environ=None) -> bool:
    return _flag(INCREMENTAL_ENV, environ)


def auto_enabled(environ=None) -> bool:
    return _flag(AUTO_ENV, environ)


# ---------------------------------------------------------------- reports
def _window(value) -> tuple[date, date] | None:
    if not isinstance(value, dict):
        return None
    try:
        return date.fromisoformat(str(value["start"])), date.fromisoformat(str(value["end"]))
    except (KeyError, TypeError, ValueError):
        return None


def coverage_window(report: dict) -> tuple[date, date] | None:
    cov = report.get(COVERAGE_FIELD)
    return _window(cov.get(COVERAGE_WINDOW_FIELD)) if isinstance(cov, dict) else None


def capabilities(report: dict, request: dict | None = None) -> dict:
    """What this report (and the request that made it) shows the Bookkeeper can do."""
    window = report.get("window") if isinstance(report.get("window"), dict) else {}
    delta = (report.get(MODE_FIELD) == DELTA_MODE or SINCE_FIELD in window
             or SINCE_FIELD in (request or {}) or coverage_window(report) is not None)
    findings = [f for f in report.get("findings") or [] if isinstance(f, dict)]
    return {"delta": bool(delta),
            "status": any(STATUS_FIELD in f for f in findings),
            "finding_key": any(FINDING_KEY_FIELD in f for f in findings)}


def is_delta(report: dict) -> bool:
    return capabilities(report)["delta"]


def mode(report: dict) -> str:
    return "delta" if is_delta(report) else "full"


def report_as_of(report: dict) -> date:
    return date.fromisoformat(str(report["as_of"]))


def report_window(report: dict) -> dict:
    """{start, end} the report speaks for: a delta's coverage window if it has one, else its window, else as_of."""
    w = coverage_window(report) or _window(report.get("window"))
    if w is None:
        d = report_as_of(report)
        w = (d, d)
    out = {"start": w[0].isoformat(), "end": w[1].isoformat()}
    since = (report.get("window") or {}).get(SINCE_FIELD) if isinstance(report.get("window"), dict) else None
    if since:
        out["since"] = str(since)
    return out


def covered_end(report: dict) -> date:
    """The last day this report covers: the window end, else as_of."""
    return date.fromisoformat(report_window(report)["end"])


def validate_report(report) -> list[str]:
    """Structural problems with a schema-1.1 report (empty list = usable). Never quotes report text back."""
    if not isinstance(report, dict):
        return ["not a JSON object"]
    problems = []
    if str(report.get("schema_version", SCHEMA_VERSION)) != SCHEMA_VERSION:
        problems.append("schema_version is not 1.1")
    if not isinstance(report.get("report_id"), str) or not report.get("report_id"):
        problems.append("report_id missing")
    try:
        report_as_of(report)
    except (KeyError, TypeError, ValueError):
        problems.append("as_of missing or not a date")
    if "window" in report and _window(report["window"]) is None:
        problems.append("window is not {start, end}")
    findings = report.get("findings")
    if not isinstance(findings, list):
        return problems + ["findings is not a list"]
    for i, f in enumerate(findings):
        if not isinstance(f, dict):
            problems.append(f"findings[{i}] is not an object")
            continue
        t = f.get("treatment")
        if not isinstance(f.get("root_cause"), str):
            problems.append(f"findings[{i}].root_cause missing")
        if not isinstance(t, dict) or t.get("kind") not in TREATMENT_KINDS:
            problems.append(f"findings[{i}].treatment.kind missing or unknown")
        if STATUS_FIELD in f and f[STATUS_FIELD] not in STATUSES:
            problems.append(f"findings[{i}].{STATUS_FIELD} is not one of {', '.join(STATUSES)}")
    return problems


# ---------------------------------------------------------------- findings
def _target(f: dict) -> str:
    t = f.get("treatment") or {}
    kind = t.get("kind")
    if kind == "owed":
        return str((f.get("books_refs") or [""])[0])
    if kind == "correct_to":
        return str(t.get("bank_id", ""))
    if kind == "exclude":
        return ",".join(sorted(str(b) for b in t.get("bank_ids") or []))
    if kind == "ongoing":
        return str(t.get("since", ""))
    return ""


def finding_key(f: dict) -> str:
    """A key that survives the Bookkeeper renumbering its ids: its own finding_key when it sends one, else
    sha256(root_cause | lower(strip(counterparty)) | treatment.kind | target)."""
    own = f.get(FINDING_KEY_FIELD)
    if isinstance(own, str) and own.strip():
        return own.strip()
    parts = [str(f.get("root_cause", "")), str(f.get("counterparty") or "").strip().lower(),
             str((f.get("treatment") or {}).get("kind", "")), _target(f)]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def bank_refs(f: dict) -> set[str]:
    """Every bank id a finding names (its bank_ids and its treatment's): the secondary match across runs."""
    t = f.get("treatment") or {}
    ids = set(map(str, f.get("bank_ids") or []))
    ids.update(map(str, t.get("bank_ids") or []))
    if t.get("bank_id"):
        ids.add(str(t["bank_id"]))
    return ids


def same_finding(a: dict, b: dict) -> bool:
    """Secondary match when keys differ: same root cause and overlapping bank ids."""
    return a.get("root_cause") == b.get("root_cause") and bool(bank_refs(a) & bank_refs(b))


def status(f: dict) -> str:
    s = f.get(STATUS_FIELD)
    return s if s in CLOSED_STATUSES else "open"


def clean_finding(f: dict) -> dict:
    """The finding as a schema-1.1 Finding (the incremental-only fields dropped)."""
    return {k: v for k, v in f.items() if k not in (FINDING_KEY_FIELD, STATUS_FIELD)}


def merged_report(findings: list[dict], *, report_id: str, as_of: str, window: dict, sources: dict | None = None,
                  limitations: list[str] | None = None) -> dict:
    """A schema-1.1-shaped report holding `findings` (ids already set), with summary totals recomputed."""
    def impact(f):
        return int(((f.get("amounts") or {}).get("impact_cents")) or 0)

    summary = {"errors": sum(f.get("verdict") == "error" for f in findings),
               "needs_owner": sum(f.get("verdict") == "needs_owner" for f in findings),
               "total_impact_cents": sum(abs(impact(f)) for f in findings),
               "recoverable_cents": sum(abs(impact(f)) for f in findings if f.get("recoverable") is True),
               "ongoing_monthly_cents": sum(int((f.get("treatment") or {}).get("monthly_cents") or 0)
                                            for f in findings if (f.get("treatment") or {}).get("kind") == "ongoing")}
    return {"schema_version": SCHEMA_VERSION, "report_id": report_id, "request_id": report_id, "as_of": as_of,
            "window": {"start": window["start"], "end": window["end"]}, "sources": sources or {},
            "summary": summary, "findings": findings, "cleared": [], "limitations": list(limitations or []),
            "verification": {"passed": False, "checked_at": None, "findings_checked": 0, "findings_rejected": 0,
                             "notes": ["Merged by the orchestrator from ingested Bookkeeper reports; unverified."]}}


# ---------------------------------------------------------------- requests
def build_request(as_of, since=None, prior_report_id=None, *, request_id: str, tag: str = "boh-1loc",
                  environ=None) -> dict:
    """The ReportRequest payload (agent mode sends it as JSON). `since` / `prior_report_id` are included ONLY with
    ORCHESTRATOR_BK_INCREMENTAL=1: today's ReportRequest forbids extra fields."""
    payload = {"request_id": request_id, "nessie_tag": tag,
               "as_of": as_of.isoformat() if isinstance(as_of, date) else as_of}
    if incremental_enabled(environ):
        if since is not None:
            payload[SINCE_FIELD] = since.isoformat() if isinstance(since, date) else str(since)
        if prior_report_id:
            payload[PRIOR_FIELD] = str(prior_report_id)
    return payload


def cli_args(payload: dict) -> list[str]:
    """`bookkeeper run` arguments for a payload from build_request."""
    args = ["run", "--request-id", payload["request_id"]]
    if payload.get("as_of"):
        args += ["--as-of", str(payload["as_of"])]
    args += ["--tag", payload.get("nessie_tag", "boh-1loc")]
    if SINCE_FIELD in payload:
        args += [SINCE_FLAG, str(payload[SINCE_FIELD])]
    if PRIOR_FIELD in payload:
        args += [PRIOR_FLAG, str(payload[PRIOR_FIELD])]
    return args
