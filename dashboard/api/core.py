"""Where the dashboard finds the pipeline (nessie_lab), its data, and secrets from the repo's .env."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]          # BackHouse/
LAB = ROOT / "nessie_lab"
DATA = LAB / "data"
UPLOADS = Path(__file__).resolve().parent / "uploads"
TAG = os.environ.get("BOH_NESSIE_TAG", "boh-1loc")

if str(LAB) not in sys.path:
    sys.path.insert(0, str(LAB))


def readiness():
    """Is the data the chat and the forecast read in place? A check (Get started, or the chat's "Use the sample data")
    puts it there: the bank pulled from Nessie, and the records with the Book and the features built."""
    import sqlite3
    from contextlib import closing
    missing = []
    if not (DATA / f"nessie_sync_{TAG}.db").exists():
        missing.append("bank")
    books, feats = DATA / "books.db", DATA / "features.db"
    try:
        with closing(sqlite3.connect(books.as_uri() + "?mode=ro", uri=True)) as con:
            has_book = con.execute("select 1 from sqlite_master where type = 'table' and name = 'costs'").fetchone()
    except sqlite3.Error:
        has_book = None
    if not (books.exists() and has_book and feats.exists()):
        missing.append("records")
    return {"ready": not missing, "missing": missing}


def env_value(name):
    """A setting from the environment, else from BackHouse/.env (never logged)."""
    if os.environ.get(name):
        return os.environ[name].strip()
    env = ROOT / ".env"
    if env.exists():
        for line in env.read_text().splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip().strip("\"'")
    return None
