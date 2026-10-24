"""Thin Nessie client: key from $NESSIE_API_KEY or a .env (BackHouse/.env first), retries reads only, never logs the key."""
import os, time
from pathlib import Path

import requests

BASE = "https://prod-api.nessieisreal.com"
_HERE = Path(__file__).resolve().parent
_ENV_FILES = [_HERE.parent / "BackHouse" / ".env", _HERE.parent / ".env", _HERE / ".env"]


def _key():
    """NESSIE_API_KEY from the environment, else from the first .env that has it."""
    if os.environ.get("NESSIE_API_KEY"):
        return os.environ["NESSIE_API_KEY"].strip()
    for f in _ENV_FILES:
        if f.exists():
            for line in f.read_text().splitlines():
                if line.startswith("NESSIE_API_KEY="):
                    return line.split("=", 1)[1].strip().strip("\"'")
    raise RuntimeError("NESSIE_API_KEY not set and not found in: " + ", ".join(str(f) for f in _ENV_FILES))


KEY = _key()
_s = requests.Session()


class NessieError(Exception):
    pass


def call(method, path, body=None, retries=None):
    # POST is not idempotent: a timed-out POST may already be stored, so never resend it automatically.
    # The loader recovers on the next run by adopting what Nessie already holds.
    retries = retries or (1 if method == "POST" else 4)
    for attempt in range(retries):
        try:
            r = _s.request(method, BASE + path, params={"key": KEY}, json=body, timeout=30)
        except requests.RequestException as e:
            err = type(e).__name__                   # message would contain the URL (and key)
        else:
            if r.status_code < 500:
                try:
                    data = r.json()
                except ValueError:
                    data = r.text
                if r.status_code >= 400:
                    raise NessieError(f"{method} {path} -> {r.status_code}: {str(data)[:300]}")
                return data
            err = f"HTTP {r.status_code}"
        if attempt < retries - 1:
            time.sleep(1.5 * 2 ** attempt)
    raise NessieError(f"{method} {path} failed after {retries} attempts ({err})")


def created_id(resp):
    return resp["objectCreated"]["_id"]
