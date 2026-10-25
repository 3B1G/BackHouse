# Bookkeeper report backups

Verified Bookkeeper reports kept in git. `data/reports/` is gitignored (every run writes there locally), so a report
someone ran once lives here too and can be restored on any machine without running the Bookkeeper again.

| File | As of | Findings | Verified |
|---|---|---|---|
| `R-fb8f4cfb4767cce5aa48da780bb5a2034a16520311177d0d69ba70cf970d0407.json` | 2026-10-23 | 9, each an error the Verifier agreed with | yes, 3 rounds |

It was first shared as `20261024_233812.json`.

To restore it, from `nessie_lab/`:

    mkdir -p data/reports
    cp data/backup_reports/R-fb8f4cfb4767cce5aa48da780bb5a2034a16520311177d0d69ba70cf970d0407.json data/reports/
    uv run --project orchestrator orchestrator bookkeeper-ingest data/reports/R-fb8f4cfb4767cce5aa48da780bb5a2034a16520311177d0d69ba70cf970d0407.json

The dashboard then shows its findings for the owner's review (see `dashboard/README.md`).
