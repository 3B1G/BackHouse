"""The team's shared modules, loaded from nessie_lab/: contracts.py (Suggestion, Approval, Change, ExecutionResult,
AuditEntry) and, only for live Nessie writes, nessie_client.py."""
import importlib.util
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[3]            # nessie_lab/


def load(name: str, root: Path = DEFAULT_ROOT):
    alias = f"backhouse_{name}"
    if alias in sys.modules:
        return sys.modules[alias]
    path = Path(root) / f"{name}.py"
    if not path.exists():
        raise FileNotFoundError(f"{path} not found; pull main")
    spec = importlib.util.spec_from_file_location(alias, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[alias] = module
    spec.loader.exec_module(module)
    return module


contracts = load("contracts")
Suggestion, Approval, Change = contracts.Suggestion, contracts.Approval, contracts.Change
ExecutionResult, AuditEntry = contracts.ExecutionResult, contracts.AuditEntry


class LiveNessie:
    """The real bank. Reads the key from .env through nessie_client; PUT on a bill is safe to repeat."""

    def __init__(self, root: Path = DEFAULT_ROOT):
        self.client = load("nessie_client", root)

    def get_bill(self, bill_id):
        return self.client.call("GET", f"/bills/{bill_id}")

    def set_payment_date(self, bill_id, day: str):
        return self.client.call("PUT", f"/bills/{bill_id}", {"payment_date": day})
