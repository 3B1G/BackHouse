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
