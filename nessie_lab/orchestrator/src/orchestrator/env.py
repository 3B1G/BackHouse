"""Where things are. Puts nessie_lab/ on sys.path (forecaster, contracts, config, features, bookkeeper_tools), loads
.env from nessie_lab/.env and then BackHouse/.env (never overriding what is already set), and points the nessie_lab
modules that hard-code `Path(__file__).parent / "data"` at a session's root.

The nessie_lab modules keep their data paths in module globals (forecaster.D, bookkeeper_tools.BANK_DB, ...). `bind`
re-points them at `<root>/data` so a session on a scratch copy of the data reads and writes only that copy. It holds
a lock: one root at a time per process."""
import sys
import threading
from pathlib import Path

from dotenv import load_dotenv

NESSIE_LAB = Path(__file__).resolve().parents[3]            # src/orchestrator/env.py -> nessie_lab/
DEFAULT_ROOT = NESSIE_LAB

if str(NESSIE_LAB) not in sys.path:
    sys.path.insert(0, str(NESSIE_LAB))

for _env in (NESSIE_LAB / ".env", NESSIE_LAB.parent / ".env"):
    load_dotenv(_env, override=False)

LOCK = threading.RLock()
_bound: tuple[str, str] | None = None


def bind(root: Path, tag: str = "boh-1loc"):
    """Point forecaster, bookkeeper_tools and features at root/data (and the Nessie tag). Idempotent; clears the
    caches of bookkeeper_tools and the forecaster when the root changes."""
    global _bound
    import bookkeeper_tools as bt
    import features
    import forecaster as fc

    key = (str(Path(root).resolve()), tag)
    with LOCK:
        if _bound == key:
            return
        d = Path(key[0]) / "data"
        fc.D, fc.REPORTS, fc.FORECASTS = d, d / "reports", d / "forecasts"
        fc.BOOKS, fc.FEATURES, fc.TAG = d / "books.db", d / "features.db", tag
        fc._CACHE.clear()
        bt.D, bt.TAG = d, tag
        bt.BOOK_DB, bt.BANK_DB = d / "books.db", d / f"nessie_sync_{tag}.db"
        bt.reload()
        features.D = d
        _bound = key
