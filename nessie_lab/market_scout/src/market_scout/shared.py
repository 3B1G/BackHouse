"""Load the team's shared nessie_lab/contracts.py so the scout emits exactly the agreed CompetitorPrice/MarketSummary."""
import importlib.util
import sys
from pathlib import Path

DEFAULT_ROOT = Path(__file__).resolve().parents[3]            # nessie_lab/


def private_dir(path: Path) -> Path:
    """data/market_scout/: the scout's own state (local client seed, Apify cache), never committed. The directory
    carries its own .gitignore, so nothing outside market_scout/ has to change."""
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# market scout private state: never commit\n*\n", encoding="utf-8")
    return path


def load_contracts(root: Path = DEFAULT_ROOT):
    """Import root/contracts.py once under a private name; it only needs pydantic, not the parent's pandas stack."""
    name = "backhouse_contracts"
    if name in sys.modules:
        return sys.modules[name]
    path = Path(root) / "contracts.py"
    if not path.exists():
        raise FileNotFoundError(f"shared contracts not found at {path}; pull main (PR #4 added it)")
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


contracts = load_contracts()
CompetitorPrice = contracts.CompetitorPrice
MarketSummary = contracts.MarketSummary
