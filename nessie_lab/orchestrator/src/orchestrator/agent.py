"""The orchestrator as a uAgent (uagents 0.26.1, Agent Chat Protocol), for ASI:One and the local demo.

Modes
- Local (default): binds 127.0.0.1 (uagents.asgi.HOST, which 0.26.1 has no parameter for; ORCHESTRATOR_BIND_HOST
  overrides), stays off the public registry (LocalRegistration), and answers only ORCHESTRATOR_ALLOWED_SENDERS, or
  else only this machine's local client (seed in ORCHESTRATOR_LOCAL_CLIENT_SEED, or a random one kept in
  data/orchestrator/local_client_seed). Replies go to the local client endpoint and ORCHESTRATOR_PEERS
  (`agent1...=http://127.0.0.1:PORT/submit`, comma-separated; validated like the scout's MARKET_SCOUT_PEERS).
- Mailbox (ORCHESTRATOR_MAILBOX=1 or mailbox=True): reachable through Agentverse, so anyone on ASI:One can chat.
  It refuses to start unless bound to a loopback address. Every sender is rate limited (10 messages per 10 minutes)
  and messages over 4,000 characters are refused. Only ORCHESTRATOR_OWNERS may make it propose a change; everyone
  else gets facts and "ask the owner", and never an absolute path. The resolver checks ORCHESTRATOR_PEERS first and
  then the global Almanac, so ASI:One users get their replies.

Every message: an acknowledgement first (except our own address: dropped); then the TextContent parts joined;
StartSessionContent resets this sender's state; a turn already running for the sender gets "busy"; turn() runs
(tools in worker threads); the reply is TextContent + EndSessionContent. When this turn proposed a Suggestion and
cards are on (ORCHESTRATOR_CARDS, default on in mailbox mode), the reply carries an Interactive Card (card_kind
"review") with Save/Discard instead of the end-session part, so the session stays open for the click. Neither
button approves anything.

No ping-pong: a message carrying EndSessionContent, a review card, or one of our notices (tagged with metadata and
starting with NOTICE_PREFIX) never starts a turn; rate-limit, busy and refusal notices go out at most once per
sender per window, then only acknowledgements.

Agent mode (ORCHESTRATOR_USE_AGENTS=1): scan_market goes to the market scout (SCOUT_ADDR) and get_findings first
asks the Bookkeeper (BOOKKEEPER_ADDR) for a fresh report, each with ctx.send_and_receive. The scout's MarketResult
names a report file, which is loaded (data/market_reports/M-*.json only) into the in-process scan_market shape; the
Bookkeeper's filename must be data/reports/R-*.json and that report is used. On a timeout, error or bad reply the
in-process tool runs instead and the reply is marked degraded.

HTTP (GuardedServer, see there): no CORS, no proxy headers, a bearer token (data/orchestrator/rest_token) on
POST /ask, /connect and /disconnect; GET /messages is never served. REST: POST /ask (AskRequest -> AskResponse)
runs the same turn for the CLI and the demo. It is registered only when the server is bound to a loopback address."""
import asyncio
import contextlib
import hmac
import json
import logging
import os
import re
import secrets
import stat
import time
from collections import OrderedDict, deque
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

import uvicorn
from uagents import Agent, Context, Model, Protocol
from uagents.asgi import ASGIServer
from uagents.resolver import GlobalResolver, Resolver
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, MetadataContent, StartSessionContent, TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.chat.cards import (
    CtaAction, ReviewCardPayload, ReviewSummaryRow, create_card_content, extract_card_response,
)
from uagents_core.identity import Identity
from uagents_core.registration import AgentRegistrationPolicy

from .turn import MAX_INPUT, clean_text, turn

LOG = logging.getLogger("orchestrator.agent")
DEFAULT_ROOT = Path(__file__).resolve().parents[3]           # nessie_lab/
SEED_FILE = "local_client_seed"
TOKEN_FILE = "rest_token"
PEER_ADDRESS = re.compile(r"^agent1[0-9a-z]{1,120}$")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
LOOPBACK = ("127.0.0.1", "localhost", "::1")
LOOPBACK_IPS = ("127.0.0.1", "::1")
RATE_LIMIT, RATE_WINDOW = 10, 600                            # messages per sender per 10 minutes
MAX_TRACKED = 5000                                           # senders the rate limiter and notices remember (LRU)
MAX_SESSIONS = 500
PEER_TIMEOUT = 25
TRUE, FALSE = ("1", "true", "yes", "on"), ("0", "false", "no", "off", "")
REQUEST_ID = re.compile(r"[^A-Za-z0-9._-]")
NOTICE_PREFIX = "BackHouse notice:"
NOTICE_TAG = "backhouse_notice"


class LocalRegistration(AgentRegistrationPolicy):
    """Keep the localhost demo off the public agent registry."""

    async def register(self, agent_identifier, identity, protocols, endpoints, metadata=None):
        return None


class LocalFirstResolver(Resolver):
    """Local rules first (the local client, ORCHESTRATOR_PEERS), then the fallback (the global Almanac resolver in
    mailbox mode, so ASI:One users can be answered; none in local mode, so nothing leaves this machine)."""

    def __init__(self, rules: dict[str, str], fallback: Resolver | None = None):
        self.rules = dict(rules)
        self.fallback = fallback

    async def resolve(self, destination: str) -> tuple[str | None, list[str]]:
        address = destination.rsplit("/", 1)[-1] if "://" in destination else destination
        if address in self.rules:
            return address, [self.rules[address]]
        if self.fallback is None:
            return None, []
        return await self.fallback.resolve(destination)


class RateLimit:
    """At most `limit` messages per sender in any `window` seconds (sliding window, in memory). Remembers at most
    `max_senders` senders (least recently seen dropped first) and forgets a sender once its window is empty, so a
    flood of fresh addresses can't grow memory without bound."""

    def __init__(self, limit=RATE_LIMIT, window=RATE_WINDOW, clock=time.monotonic, max_senders=MAX_TRACKED):
        self.limit, self.window, self.clock, self.max_senders = limit, window, clock, max_senders
        self.seen: OrderedDict[str, deque] = OrderedDict()

    def _prune(self, now):
        while self.seen:                                  # oldest-touched first: drop senders with nothing left
            first, q = next(iter(self.seen.items()))
            if q and now - q[-1] < self.window:
                break
            del self.seen[first]
        while len(self.seen) > self.max_senders:
            self.seen.popitem(last=False)

    def allow(self, sender: str) -> bool:
        now = self.clock()
        q = self.seen.pop(sender, None) or deque()
        while q and now - q[0] >= self.window:
            q.popleft()
        allowed = len(q) < self.limit
        if allowed:
            q.append(now)
        if q:
            self.seen[sender] = q                         # most recently touched last
        self._prune(now)
        return allowed


class NoticeOnce:
    """At most one notice (rate limited, busy, refused) per sender per window; after that, only acknowledgements."""

    def __init__(self, window=RATE_WINDOW, clock=time.monotonic, max_senders=MAX_TRACKED):
        self.window, self.clock, self.max_senders = window, clock, max_senders
        self.sent: OrderedDict[str, float] = OrderedDict()

    def allow(self, sender: str) -> bool:
        now = self.clock()
        last = self.sent.pop(sender, None)
        ok = last is None or now - last >= self.window
        self.sent[sender] = now if ok else last
        while len(self.sent) > self.max_senders:
            self.sent.popitem(last=False)
        return ok


# ---------------------------------------------------------------- environment
def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or not value.strip():
        return default
    value = value.strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise ValueError(f"{name} must be 1 or 0 (or true/false), not {value[:20]!r}")


def parse_peers(text: str | None, allow_remote=False, var="ORCHESTRATOR_PEERS") -> dict[str, str]:
    """`agent1...=http(s)://host:port/path`, comma-separated -> {address: endpoint}. The host must be 127.0.0.1 or
    localhost unless allow_remote. Any malformed entry raises ValueError (serve won't start)."""
    peers = {}
    for i, entry in enumerate((text or "").split(",")):
        entry = entry.strip()
        if not entry:
            continue
        where = f"{var} entry {i + 1} ({entry[:80]!r})"
        address, eq, endpoint = entry.partition("=")
        address, endpoint = address.strip(), endpoint.strip()
        if not eq or not address or not endpoint:
            raise ValueError(f"{where}: expected agent1...=http://127.0.0.1:<port>/submit")
        if not PEER_ADDRESS.match(address):
            raise ValueError(f"{where}: the address must be an agent address starting with agent1")
        if address in peers:
            raise ValueError(f"{where}: {address[:20]}... is listed twice")
        try:
            parts = urlsplit(endpoint)
            parts.port                                        # raises on a bad port
        except ValueError:
            raise ValueError(f"{where}: the endpoint is not a valid URL") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{where}: the endpoint must be an http(s) URL with a host")
        if "@" in parts.netloc:
            raise ValueError(f"{where}: the endpoint must not carry a user name or password")
        if parts.hostname.lower() not in LOCAL_HOSTS and not allow_remote:
            raise ValueError(f"{where}: the endpoint must be on 127.0.0.1 or localhost "
                             f"(set {var}_ALLOW_REMOTE=1 to allow other hosts)")
        peers[address] = endpoint
    return peers


def env_peers() -> dict[str, str]:
    return parse_peers(os.getenv("ORCHESTRATOR_PEERS"), env_flag("ORCHESTRATOR_PEERS_ALLOW_REMOTE"))


def env_address(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    if not value:
        return None
    if not PEER_ADDRESS.match(value):
        raise ValueError(f"{name} must be an agent address starting with agent1")
    return value


def env_owners() -> set[str]:
    """ORCHESTRATOR_OWNERS: the agent addresses (ASI:One users) allowed to make it propose changes in mailbox mode."""
    owners = {a.strip() for a in os.getenv("ORCHESTRATOR_OWNERS", "").split(",") if a.strip()}
    bad = [a for a in owners if not PEER_ADDRESS.match(a)]
    if bad:
        raise ValueError("ORCHESTRATOR_OWNERS must list agent addresses starting with agent1")
    return owners


def private_dir(path: Path) -> Path:
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    ignore = path / ".gitignore"
    if not ignore.exists():
        ignore.write_text("# orchestrator private state: never commit\n*\n", encoding="utf-8")
    return path


def _owner_only(path: Path):
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def local_client_seed(root: Path) -> str:
    """ORCHESTRATOR_LOCAL_CLIENT_SEED, else this machine's own random seed (created on first use)."""
    seed = os.getenv("ORCHESTRATOR_LOCAL_CLIENT_SEED", "").strip()
    if seed:
        return seed
    path = private_dir(Path(root) / "data" / "orchestrator") / SEED_FILE
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        seed = path.read_text(encoding="utf-8").strip()
        if len(seed) < 32:
            raise ValueError(f"{path} does not hold a usable seed; delete it to generate a new one") from None
        return seed
    seed = secrets.token_hex(32)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(seed + "\n")
    _owner_only(path)
    return seed


def new_rest_token(root: Path) -> str:
    """A fresh random bearer token for this run, written to data/orchestrator/rest_token (owner-only)."""
    path = private_dir(Path(root) / "data" / "orchestrator") / TOKEN_FILE
    token = secrets.token_urlsafe(32)
    with contextlib.suppress(FileNotFoundError):
        path.unlink()
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    _owner_only(path)
    return token


def read_rest_token(root: Path) -> str | None:
    """The running agent's token, for the CLI and register.py (same machine, same user)."""
    try:
        token = (Path(root) / "data" / "orchestrator" / TOKEN_FILE).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return token or None


def local_client_address(root: Path) -> str:
    return Identity.from_seed(local_client_seed(root), 0).address


def listed_senders() -> set[str]:
    return {a.strip() for a in os.getenv("ORCHESTRATOR_ALLOWED_SENDERS", "").split(",") if a.strip()}


def allowed_senders(root: Path) -> set[str]:
    return listed_senders() or {local_client_address(root)}


def bind_localhost() -> str:
    """uagents 0.26.1 binds every agent server to uagents.asgi.HOST (0.0.0.0) and has no parameter for it."""
    import uagents.asgi

    uagents.asgi.HOST = os.getenv("ORCHESTRATOR_BIND_HOST", "127.0.0.1")
    return uagents.asgi.HOST


# ---------------------------------------------------------------- HTTP guard
def _loopback_origin(origin: str) -> bool:
    try:
        host = (urlsplit(origin).hostname or "").lower()
    except ValueError:
        return False
    return host in LOOPBACK


class GuardedServer(ASGIServer):
    """uagents 0.26.1's ASGIServer, narrowly hardened for this agent only (make_agent swaps the class of the one
    instance Agent.__init__ built, so no other agent in the process is touched).

    Why: uagents/asgi.py's serve() runs uvicorn with `forwarded_allow_ips="*"` (any client can claim to be
    127.0.0.1 with X-Forwarded-For, and the reserved inspector routes trust exactly that address) and with
    `Access-Control-Allow-Origin: *` on every response (any web page can drive the REST routes and read the
    replies, including GET /messages, the agent's message history). Here:
    - serve() runs uvicorn with proxy_headers=False (forwarded_allow_ips=None: no proxy is trusted) and no CORS
      headers at all;
    - a request with an Origin header is refused unless the origin is loopback AND it carries the bearer token;
    - GET /messages is never served; POST /connect and /disconnect need the token, a loopback client, and an open
      inspector (closed by ORCHESTRATOR_INSPECTOR=0 or once a /connect succeeded);
    - POST /ask needs the token (`Authorization: Bearer <data/orchestrator/rest_token>`).
    Everything else (/submit envelopes from agents, HEAD readiness probes) goes to uagents unchanged."""

    token: str = ""
    inspector_open: bool = False

    def _authed(self, headers: dict[bytes, bytes]) -> bool:
        value = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, given = value.partition(" ")
        return bool(self.token) and scheme.lower() == "bearer" and hmac.compare_digest(given.strip(), self.token)

    async def _deny(self, send, status: int, error: str):
        await send({"type": "http.response.start", "status": status,
                    "headers": [[b"content-type", b"application/json"]]})
        await send({"type": "http.response.body", "body": json.dumps({"error": error}).encode()})

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            return await super().__call__(scope, receive, send)
        headers = {}
        for k, v in scope.get("headers") or []:
            headers.setdefault(bytes(k).lower(), bytes(v))
        path, method = scope.get("path", ""), scope.get("method", "")
        client = (scope.get("client") or ("",))[0]
        authed = self._authed(headers)
        origin = headers.get(b"origin")
        if origin is not None and not (_loopback_origin(origin.decode("latin-1")) and authed):
            return await self._deny(send, 403, "forbidden")
        if path == "/messages":
            return await self._deny(send, 404, "not found")
        if path in ("/connect", "/disconnect"):
            if not self.inspector_open:
                return await self._deny(send, 404, "not found")
            if not authed or client not in LOOPBACK_IPS:
                return await self._deny(send, 403, "forbidden")
            if path == "/connect":
                return await super().__call__(scope, receive, self._watch_connect(send))
        if path == "/ask" and not authed:
            return await self._deny(send, 401, "missing or wrong bearer token")
        return await super().__call__(scope, receive, send)

    def _watch_connect(self, send):
        """Close the inspector routes once Agentverse accepted the mailbox."""
        state = {"status": None}

        async def watched(message):
            if message.get("type") == "http.response.start":
                state["status"] = message.get("status")
            elif message.get("type") == "http.response.body" and state["status"] == 200:
                try:
                    if json.loads(message.get("body") or b"{}").get("success") is True:
                        self.inspector_open = False
                except (ValueError, AttributeError):
                    pass
            await send(message)
        return watched

    def uvicorn_config(self) -> uvicorn.Config:
        import uagents.asgi

        return uvicorn.Config(self, host=uagents.asgi.HOST, port=self._port, log_level="warning",
                              proxy_headers=False, forwarded_allow_ips=None, headers=[])

    async def serve(self):
        self._server = uvicorn.Server(self.uvicorn_config())
        self._logger.info(f"Starting server on http://{self._server.config.host}:{self._port} (Press CTRL+C to quit)")
        with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
            await self._server.serve()


def guard_server(agent, token: str, inspector_open: bool) -> GuardedServer:
    server = agent._server
    if not isinstance(server, ASGIServer):
        raise TypeError("uagents changed: Agent._server is no longer an ASGIServer")
    server.__class__ = GuardedServer
    server.token, server.inspector_open = token, inspector_open
    return server


# ---------------------------------------------------------------- messages and cards
def chat_message(parts: list) -> ChatMessage:
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=parts)


def text_reply(text: str, end=True, extra: list | None = None) -> ChatMessage:
    parts = [TextContent(type="text", text=text), *(extra or [])]
    if end:
        parts.append(EndSessionContent(type="end-session"))
    return chat_message(parts)


def notice(text: str) -> ChatMessage:
    """One of our notices (rate limited, busy, refused, greeting, error): tagged twice, so another agent built like
    this one never answers it, and it ends the session."""
    return chat_message([TextContent(type="text", text=f"{NOTICE_PREFIX} {text}"),
                         MetadataContent(type="metadata", metadata={NOTICE_TAG: "1"}),
                         EndSessionContent(type="end-session")])


def is_our_output(message: ChatMessage) -> bool:
    """A notice (tag or prefix) or a message carrying a review card: what an orchestrator sends, never a question."""
    for part in message.content:
        if isinstance(part, MetadataContent):
            meta = part.metadata or {}
            if NOTICE_TAG in meta or ("card_kind" in meta and extract_card_response(part) is None):
                return True
        if isinstance(part, TextContent) and part.text.lstrip().startswith(NOTICE_PREFIX):
            return True
    return False


def _dollars(cents) -> str:
    try:
        return f"${int(cents) / 100:,.2f}"
    except (TypeError, ValueError):
        return str(cents)


def review_card(proposal: dict, scrub=lambda t: t) -> MetadataContent:
    """ASI:One Interactive Card, card_kind "review", card_protocol_version "1": every value is a string."""
    s = proposal.get("suggestion") if isinstance(proposal.get("suggestion"), dict) else proposal
    path = str(proposal["path"])
    rows = [("What", s.get("title")), ("Action", s.get("action")), ("Target", s.get("target"))]
    if s.get("new_price_cents") is not None:
        rows.append(("New price", _dollars(s["new_price_cents"])))
    if s.get("extra_days") is not None:
        rows.append(("Extra days", s["extra_days"]))
    if s.get("monthly_cash_effect_cents") is not None:
        rows.append(("Monthly cash effect (forecast)", _dollars(s["monthly_cash_effect_cents"])))
    if s.get("one_time_cash_effect_cents") is not None:
        rows.append(("One-time cash effect (forecast)", _dollars(s["one_time_cash_effect_cents"])))
    if s.get("run_out_likely_before") or s.get("run_out_likely_after"):
        rows.append(("Likely run-out", f"{s.get('run_out_likely_before') or '-'} -> {s.get('run_out_likely_after') or '-'}"))
    rows.append(("Status", "proposed, not applied"))
    rows.append(("Suggestion file", path))
    if proposal.get("approve_command"):
        rows.append(("To approve (owner, at the terminal)", proposal["approve_command"]))
    summary = [ReviewSummaryRow(label=clean_text(k, 80), value=clean_text(scrub(str(v)), 500) or "-")
               for k, v in rows if v is not None and str(v).strip()]
    payload = ReviewCardPayload(
        title=clean_text(f"Proposed change: {s.get('title') or s.get('action') or 'suggestion'}", 120),
        summary_rows=summary,
        approve_cta=CtaAction(label="Save", selection={"action": "save", "suggestion": path}, primary=True),
        reject_cta=CtaAction(label="Discard", selection={"action": "discard", "suggestion": path}),
    )
    return create_card_content(payload, card_id=uuid4())


def message_text(message: ChatMessage) -> str:
    """The TextContent parts joined; a card response (MetadataContent selection) becomes its selection JSON."""
    texts = [part.text for part in message.content if isinstance(part, TextContent)]
    for part in message.content:
        if isinstance(part, MetadataContent):
            response = extract_card_response(part)
            if response and response.selection:
                texts.append(json.dumps({k: str(v) for k, v in response.selection.items()}))
    return "\n".join(texts)


# ---------------------------------------------------------------- agent mode
def _core(name: str):
    import importlib
    return importlib.import_module(f"orchestrator.{name}")


class PeerDispatch:
    """dispatch() for run_tool_loop in agent mode. Remote results are appended to session.log (in the in-process
    tools' shape) so the guard, the controller and propose_fix see them; any failure falls back to the in-process
    tool and marks the turn degraded."""

    def __init__(self, ctx, scout: str | None = None, bookkeeper: str | None = None, timeout: int = PEER_TIMEOUT):
        self.ctx, self.scout, self.bookkeeper, self.timeout = ctx, scout, bookkeeper, timeout
        self.notes: list[str] = []
        self.degraded = False

    async def _local(self, session, name, args):
        from .llm import _default_dispatch
        return await _default_dispatch(session, name, args)

    async def _call(self, address: str, payload: dict) -> tuple[dict | None, str]:
        """Send one ChatMessage of JSON and wait for the peer's ChatMessage reply. (reply, why-not)."""
        msg = chat_message([TextContent(type="text", text=json.dumps(payload, default=str))])
        try:
            reply, status = await asyncio.wait_for(
                self.ctx.send_and_receive(address, msg, response_type=ChatMessage, timeout=self.timeout),
                self.timeout + 5)
        except asyncio.TimeoutError:
            return None, "timed out"
        except Exception as exc:
            return None, f"send failed ({type(exc).__name__})"
        if reply is None:
            return None, clean_text(getattr(status, "detail", "") or "no reply", 120)
        try:
            body = json.loads("\n".join(p.text for p in reply.content if isinstance(p, TextContent)))
        except (ValueError, AttributeError):
            return None, "reply was not JSON"
        return (body, "") if isinstance(body, dict) else (None, "reply was not a JSON object")

    def _fallback(self, who: str, why: str):
        self.degraded = True
        self.notes.append(f"degraded: {who} agent {why}; used the in-process tool")

    def _request_id(self, session, prefix):
        try:
            rid = str(session.new_id(prefix))
        except Exception:
            rid = f"{prefix}-{uuid4().hex[:12]}"
        return REQUEST_ID.sub("-", rid)[:64] or f"{prefix}-{uuid4().hex[:12]}"

    @staticmethod
    def _as_of(session):
        value = getattr(session, "as_of", None)
        return value.isoformat() if isinstance(value, date) else value

    async def _adopt(self, session, reply: dict, request_id: str, args: dict) -> dict:
        """The scout's MarketResult -> the in-process scan_market result, from the report file it names."""
        adopt = getattr(_core("tools"), "adopt_market_report", None)
        if adopt is None:
            return {"error": "no report reader"}
        try:
            return await asyncio.to_thread(adopt, session, reply.get("filename"), request_id, args)
        except Exception as exc:
            return {"error": f"report not readable ({type(exc).__name__})"}

    async def __call__(self, session, name, args):
        if name == "scan_market" and self.scout:
            items = args.get("items") if isinstance(args.get("items"), list) else []
            payload = {"request_id": self._request_id(session, "orch-scan"), "items": [str(i)[:80] for i in items][:50],
                       "service": args.get("service") if args.get("service") in ("lunch", "dinner") else "dinner",
                       "as_of": self._as_of(session), "offline": bool(getattr(session, "offline", True))}
            reply, why = await self._call(self.scout, payload)
            if reply is not None and reply.get("status") == "ok":
                result = await self._adopt(session, reply, payload["request_id"], args)
                if isinstance(result, dict) and "error" not in result:
                    return result
                why = f"sent a report I could not use ({clean_text((result or {}).get('error'), 120)})"
            elif reply is not None:
                why = f"replied {clean_text(reply.get('status'), 30) or 'without a status'}"
            self._fallback("market scout", why)
            result = await self._local(session, name, args)
            return {**result, "degraded": True} if isinstance(result, dict) else result
        if name == "get_findings" and self.bookkeeper:
            payload = {"request_id": self._request_id(session, "orch-bk"),
                       "nessie_tag": getattr(session, "tag", "boh-1loc"), "as_of": self._as_of(session)}
            reply, why = await self._call(self.bookkeeper, payload)
            check = getattr(_core("tools"), "report_name", None)
            filename = reply.get("filename") if reply is not None else None
            named = check(filename) if check is not None and filename else None
            if named:
                try:
                    session.preferred_report = named           # get_findings reads THAT report first
                except AttributeError:
                    pass
                self.notes.append(f"bookkeeper report: {named}")
            else:
                self._fallback("bookkeeper", why or ("named a file outside data/reports" if filename
                                                     else "returned no filename"))
        return await self._local(session, name, args)


# ---------------------------------------------------------------- the service
class Orchestrator:
    def __init__(self, root: Path, allowed: set[str] | None, limiter: RateLimit | None = None, cards=False,
                 use_agents=False, scout=None, bookkeeper=None, llm_factory=None, offline=True, tag="boh-1loc",
                 owners: set[str] | None = None, mailbox: bool | None = None, address: str | None = None):
        self.root = Path(root)
        self.allowed = None if allowed is None else set(allowed)   # None = anyone (mailbox mode)
        self.mailbox = (allowed is None) if mailbox is None else mailbox
        self.owners = set(owners or ())                   # mailbox mode: who may make it propose changes
        self.address = address
        self.limiter = limiter or RateLimit()
        self.notices = NoticeOnce(self.limiter.window)
        self.cards, self.use_agents, self.scout, self.bookkeeper = cards, use_agents, scout, bookkeeper
        self.llm_factory = llm_factory or (lambda: None)
        self.offline, self.tag = offline, tag
        self.sessions: OrderedDict[str, object] = OrderedDict()
        self.running: set[str] = set()
        self.brief_cache: dict = {}                       # non-owner briefs, per (root, day)

    def new_session(self):
        from .session import Session
        return Session(self.root, offline=self.offline, as_of=None, tag=self.tag, log=[])

    def session(self, key: str, reset=False):
        if reset or key not in self.sessions:
            self.sessions[key] = self.new_session()
        self.sessions.move_to_end(key)
        while len(self.sessions) > MAX_SESSIONS:
            self.sessions.popitem(last=False)
        return self.sessions[key]

    def dispatch(self, ctx):
        if self.use_agents and (self.scout or self.bookkeeper):
            return PeerDispatch(ctx, self.scout, self.bookkeeper)
        return None

    def is_owner(self, sender: str) -> bool:
        return not self.mailbox or sender in self.owners

    def scrub(self, text: str) -> str:
        """Mailbox replies never carry this machine's absolute paths."""
        if not self.mailbox or not text:
            return text
        root = self.root.resolve()
        for form in sorted({str(root), root.as_posix(), str(root).replace("\\", "\\\\")}, key=len, reverse=True):
            text = text.replace(form, "<nessie_lab>")
        return text

    async def run(self, ctx, key: str, text: str, reset=False, owner: bool = True) -> tuple[dict, list[dict]]:
        """(turn result, proposals new in this turn). Raises Busy."""
        if key in self.running:
            raise Busy()
        self.running.add(key)
        try:
            from .turn import proposals
            session = self.session(key, reset)
            before = {p["path"] for p in proposals(session)}
            result = await turn(session, text, llm=self.llm_factory(), dispatch=self.dispatch(ctx), owner=owner,
                                brief_cache=self.brief_cache)
            new = [p for p in proposals(session) if p["path"] not in before]
            return result, new
        finally:
            self.running.discard(key)


class Busy(Exception):
    pass


def reply_text(result: dict) -> str:
    text = result.get("text") or ""
    if result.get("degraded"):
        notes = "; ".join(n for n in result.get("notes", []) if n)
        text += f"\n\n(degraded answer{': ' + notes if notes else ''})"
    return text


def build_protocol(service: Orchestrator, peers_to_ignore: set[str] | None = None):
    protocol = Protocol(spec=chat_protocol_spec)
    ignore = set(peers_to_ignore or ())

    async def send_notice(ctx, sender, text):
        if service.notices.allow(sender):                 # once per sender per window, then only acks
            await ctx.send(sender, notice(text))

    @protocol.on_message(ChatMessage)
    async def handle_message(ctx: Context, sender: str, message: ChatMessage):
        me = getattr(getattr(ctx, "agent", None), "address", None) or service.address
        if me and sender == me:                           # our own message come back: never even acknowledged
            return
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc),
                                                   acknowledged_msg_id=message.msg_id))
        if sender in ignore:                              # a late peer reply: never answer it (no ping-pong)
            return
        if service.allowed is not None and sender not in service.allowed:
            ctx.logger.warning(f"Orchestrator refused a message from {sender[:80]}")
            return                                        # no reply: two refusing agents must not loop
        if any(isinstance(p, EndSessionContent) for p in message.content) or is_our_output(message):
            return                                        # a reply or notice, not a question: ack only
        if not service.limiter.allow(sender):
            await send_notice(ctx, sender, f"Rate limited: at most {service.limiter.limit} messages per "
                                           f"{service.limiter.window // 60:.0f} minutes. Try again later.")
            return
        reset = any(isinstance(p, StartSessionContent) for p in message.content)
        raw = message_text(message)
        if len(raw) > MAX_INPUT:
            await send_notice(ctx, sender, f"That message is too long (over {MAX_INPUT} characters).")
            return
        text = clean_text(raw)
        if not text:
            if reset:
                service.session(sender, reset=True)
            await ctx.send(sender, notice("Hi, I'm BackHouse's controller. Ask how we're doing, about cash, bills, "
                                          "the menu or the forecast."))
            return
        try:
            result, new = await service.run(ctx, sender, text, reset=reset, owner=service.is_owner(sender))
        except Busy:
            await send_notice(ctx, sender, "busy: I'm still working on your last message. Ask again in a moment.")
            return
        except Exception as exc:
            ctx.logger.error(f"Orchestrator turn failed ({type(exc).__name__})")
            await send_notice(ctx, sender, "Sorry, something went wrong on my side. Nothing was changed.")
            return
        body = service.scrub(reply_text(result))
        if service.cards and new:
            cards = []
            for p in new[:3]:
                try:
                    cards.append(review_card(p, service.scrub))
                except Exception as exc:                  # a bad card must not lose the text answer
                    ctx.logger.warning(f"card not built ({type(exc).__name__})")
            await ctx.send(sender, text_reply(body, end=not cards, extra=cards))
        else:
            await ctx.send(sender, text_reply(body))

    @protocol.on_message(ChatAcknowledgement)
    async def handle_ack(ctx: Context, sender: str, message: ChatAcknowledgement):
        ctx.logger.debug(f"acknowledged by {sender[:80]}")

    return protocol


class AskRequest(Model):
    text: str
    session_id: str = "local"
    reset: bool = False


class AskResponse(Model):
    text: str
    suggestions: list[str] = []
    degraded: bool = False
    notes: list[str] = []
    status: str = "ok"


def build_ask_handler(service: Orchestrator):
    """POST /ask: the owner's own CLI on this machine (GuardedServer requires the bearer token), so owner=True."""
    async def ask(ctx: Context, request: AskRequest) -> AskResponse:
        if len(request.text) > MAX_INPUT:
            return AskResponse(text=f"That message is too long (over {MAX_INPUT} characters).", status="error")
        key = "rest:" + REQUEST_ID.sub("-", request.session_id)[:64]
        try:
            result, _ = await service.run(ctx, key, request.text, reset=request.reset, owner=True)
        except Busy:
            return AskResponse(text="busy: a turn is already running for this session.", status="busy")
        except Exception as exc:
            ctx.logger.error(f"/ask failed ({type(exc).__name__})")
            return AskResponse(text="Something went wrong. Nothing was changed.", status="error")
        return AskResponse(text=result["text"], suggestions=result["suggestions"], degraded=result["degraded"],
                           notes=result["notes"])
    return ask


def make_agent(root: Path = DEFAULT_ROOT, port=8005, mailbox=False, llm_factory=None):
    seed = os.getenv("ORCHESTRATOR_SEED", "").strip()
    if not seed:
        raise ValueError("Set ORCHESTRATOR_SEED to a private, stable seed before serving")
    mailbox = mailbox or env_flag("ORCHESTRATOR_MAILBOX")
    peers = env_peers()                                   # validated in both modes: a typo refuses to start
    cards = env_flag("ORCHESTRATOR_CARDS", default=mailbox)
    use_agents = env_flag("ORCHESTRATOR_USE_AGENTS")
    scout, bookkeeper = env_address("SCOUT_ADDR"), env_address("BOOKKEEPER_ADDR")
    owners = env_owners()
    inspector = mailbox and env_flag("ORCHESTRATOR_INSPECTOR", default=True)
    if use_agents and not (scout or bookkeeper):
        LOG.warning("ORCHESTRATOR_USE_AGENTS=1 but neither SCOUT_ADDR nor BOOKKEEPER_ADDR is set; tools run in-process")
    host = bind_localhost()
    if mailbox and host not in LOOPBACK:
        raise ValueError(f"mailbox mode must bind a loopback address (127.0.0.1), not {host[:40]}: Agentverse reaches "
                         "it through the mailbox, so nothing needs to listen on the network")
    if mailbox:
        allowed = None                                    # anyone on ASI:One; rate limited
        resolver = LocalFirstResolver(peers, GlobalResolver())
        options = {"mailbox": True, "enable_agent_inspector": inspector, "publish_agent_details": True}
        if not owners:
            LOG.warning("ORCHESTRATOR_OWNERS is empty: no ASI:One user can make the orchestrator propose a change")
    else:
        client = local_client_address(root)
        if client in peers:
            raise ValueError("ORCHESTRATOR_PEERS lists the local client; set ORCHESTRATOR_LOCAL_CLIENT_ENDPOINT instead")
        rules = {client: os.getenv("ORCHESTRATOR_LOCAL_CLIENT_ENDPOINT", "http://127.0.0.1:8006/submit"), **peers}
        allowed = allowed_senders(root)
        resolver = LocalFirstResolver(rules)
        options = {"endpoint": [f"http://127.0.0.1:{port}/submit"], "registration_policy": LocalRegistration(),
                   "enable_agent_inspector": False, "publish_agent_details": False}
    agent = Agent(name="backhouse_orchestrator", seed=seed, port=port, resolve=resolver,
                  handle_messages_concurrently=True, report_events=False, **options)
    token = new_rest_token(root)
    guard_server(agent, token, inspector_open=inspector)
    service = Orchestrator(root, allowed, cards=cards, use_agents=use_agents, scout=scout, bookkeeper=bookkeeper,
                           llm_factory=llm_factory, offline=env_flag("ORCHESTRATOR_OFFLINE", default=True),
                           tag=os.getenv("ORCHESTRATOR_TAG", "boh-1loc").strip() or "boh-1loc",
                           owners=owners, mailbox=mailbox, address=agent.address)
    agent.include(build_protocol(service, {a for a in (scout, bookkeeper) if a}), publish_manifest=mailbox)
    if host in LOOPBACK:
        agent.on_rest_post("/ask", AskRequest, AskResponse)(build_ask_handler(service))
    else:
        LOG.warning("POST /ask is disabled: the server is bound to %s, not a loopback address", host)
    print(f"orchestrator: REST token for POST /ask (also in data/orchestrator/rest_token): {token}")
    agent.orchestrator = service                          # for tests and the CLI
    return agent
