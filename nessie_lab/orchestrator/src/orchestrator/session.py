"""One orchestrator run: which data it reads, whether it may go online, and the log of every tool call."""
import re
import secrets
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

RESTAURANT_TZ = ZoneInfo("America/Los_Angeles")            # as CH: menu changes start tomorrow in San Francisco
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


@dataclass
class Session:
    root: Path                                             # a nessie_lab directory (its data/ is what is read)
    offline: bool = True                                   # Market scout: saved menu pages only
    as_of: date | None = None                              # None = latest bank date (each source's own close)
    tag: str = "boh-1loc"                                  # Nessie tag of the sync DB
    log: list[dict] = field(default_factory=list)          # every tool call: {"name", "args", "result"}
    # per-session memo of expensive results (market scan, forecasts), so a brief runs each only once; not part of
    # the interface, and every lookup in it still goes through tools.call and lands in `log`
    cache: dict = field(default_factory=dict, repr=False, compare=False)
    _issued: set = field(default_factory=set, repr=False, compare=False)
    # the running turn's deadline (turn.Deadline: .expired()); tools.call refuses to start or write once it passes
    stop: object = field(default=None, repr=False, compare=False)
    # the Bookkeeper report file a peer agent just named (validated by tools.report_name); get_findings prefers it
    preferred_report: str | None = field(default=None, repr=False, compare=False)
    # what the owner wrote in this session's turns: an LLM's note on a finding is kept only if it is in here verbatim
    owner_texts: list = field(default_factory=list, repr=False, compare=False)

    def __post_init__(self):
        self.root = Path(self.root).resolve()
        if isinstance(self.as_of, str):
            self.as_of = date.fromisoformat(self.as_of)

    @property
    def data(self) -> Path:
        return self.root / "data"

    @property
    def cost_db(self) -> Path:
        return self.data / "cost_price.db"

    @property
    def sync_db(self) -> Path:
        return self.data / f"nessie_sync_{self.tag}.db"

    @staticmethod
    def today() -> date:
        """Today in San Francisco (CH's clock)."""
        return datetime.now(RESTAURANT_TZ).date()

    def effective_date(self) -> date:
        """The day CH starts a menu change: tomorrow in San Francisco."""
        return self.today() + timedelta(days=1)

    def new_id(self, prefix: str) -> str:
        """'orch-20261024-213512-3f9a01bc': prefix, San Francisco time to the second, 8 random hex (32 bits, so two
        sessions writing in the same second practically never collide). Only [A-Za-z0-9._-], at most 64 characters,
        never ':' (CH's revert namespace), and never the same twice in one session."""
        clean = re.sub(r"[^A-Za-z0-9._-]", "-", prefix or "id").strip("-.") or "id"
        stamp = datetime.now(RESTAURANT_TZ).strftime("%Y%m%d-%H%M%S")
        for _ in range(1000):
            out = f"{clean[:64 - 25]}-{stamp}-{secrets.token_hex(4)}"
            if out not in self._issued:
                self._issued.add(out)
                assert ID_RE.fullmatch(out), out
                return out
        raise RuntimeError("could not make a fresh id")
