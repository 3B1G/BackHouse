"""Agent Chat Protocol boundary for the ASI orchestrator and local client (same wiring as the Bookkeeper).

Who may ask: MARKET_SCOUT_ALLOWED_SENDERS, or else only the local demo client. That client's seed is private to this
machine: MARKET_SCOUT_LOCAL_CLIENT_SEED, or a random one generated on first use and kept (owner-only where the OS
allows) in data/market_scout/local_client_seed, which is gitignored. A seed in the source would let anyone sign as it.

Where it listens: uagents 0.26.1 has no host option (Agent takes only a port; uagents.asgi.ASGIServer.serve binds the
module constant uagents.asgi.HOST = "0.0.0.0"). serve sets that constant to 127.0.0.1 before starting, so the agent
is not reachable from the Wi-Fi; MARKET_SCOUT_BIND_HOST overrides it (e.g. 0.0.0.0 for a real deployment).

Who it can answer in local (non-mailbox) mode: the local demo client, plus MARKET_SCOUT_PEERS
(`agent1...=http://127.0.0.1:8005/submit`, comma-separated), e.g. the orchestrator. A peer entry only says where to
deliver a reply; it grants nothing. A peer must also be in MARKET_SCOUT_ALLOWED_SENDERS, and is not added to it."""
import json
import logging
import os
import re
import secrets
import stat
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from uagents import Agent, Context, Model, Protocol
from uagents.registration import AlmanacApiRegistrationPolicy
from uagents.resolver import RulesBasedResolver
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, TextContent, chat_protocol_spec,
)
from uagents_core.identity import Identity
from uagents_core.registration import AgentRegistrationPolicy

from .contracts import MarketRequest
from .runner import FALSE, TRUE, Busy, MarketScout
from .shared import DEFAULT_ROOT, private_dir

LOG = logging.getLogger("market_scout")
SEED_FILE = "local_client_seed"
PEER_ADDRESS = re.compile(r"^agent1[0-9a-z]{1,120}$")
LOCAL_HOSTS = ("127.0.0.1", "localhost")
RATE_LIMIT, RATE_WINDOW = 5, 600                   # requests per sender per 10 minutes


class LocalRegistration(AgentRegistrationPolicy):
    """Keep the localhost demo off the public agent registry."""

    async def register(self, agent_identifier, identity, protocols, endpoints, metadata=None):
        return None


def chat_message(text: str, end=False):
    content = [TextContent(type="text", text=text)]
    if end:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)


def failure_message(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return str(exc)[:300]                             # our own messages: unknown dish, reused request_id
    return f"Market scan failed ({type(exc).__name__}); see the scout's log."


def local_client_seed(root: Path) -> str:
    """MARKET_SCOUT_LOCAL_CLIENT_SEED, else this machine's own random seed (created on first use)."""
    seed = os.getenv("MARKET_SCOUT_LOCAL_CLIENT_SEED", "").strip()
    if seed:
        return seed
    path = private_dir(Path(root) / "data" / "market_scout") / SEED_FILE
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
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)       # Windows only honours the read-only bit; ACLs stay as is
    except OSError:
        pass
    return seed


def local_client_address(root: Path) -> str:
    return Identity.from_seed(local_client_seed(root), 0).address


def listed_senders() -> set[str]:
    return {a.strip() for a in os.getenv("MARKET_SCOUT_ALLOWED_SENDERS", "").split(",") if a.strip()}


def allowed_senders(root: Path) -> set[str]:
    """MARKET_SCOUT_ALLOWED_SENDERS (comma-separated agent addresses); unset or empty = only the local demo client."""
    return listed_senders() or {local_client_address(root)}


def env_flag(name: str) -> bool:
    value = os.getenv(name, "").strip().lower()
    if value in TRUE:
        return True
    if value in FALSE:
        return False
    raise ValueError(f"{name} must be 1 or 0 (or true/false), not {value[:20]!r}")


def parse_peers(text: str | None, allow_remote=False) -> dict[str, str]:
    """MARKET_SCOUT_PEERS -> {agent address: endpoint}. Each entry is `agent1...=http(s)://host:port/path`; the host
    must be 127.0.0.1 or localhost unless allow_remote. Any malformed entry raises ValueError (serve won't start)."""
    peers = {}
    for i, entry in enumerate((text or "").split(",")):
        entry = entry.strip()
        if not entry:
            continue
        where = f"MARKET_SCOUT_PEERS entry {i + 1} ({entry[:80]!r})"
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
            parts.port                                    # raises on a bad port
        except ValueError:
            raise ValueError(f"{where}: the endpoint is not a valid URL") from None
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"{where}: the endpoint must be an http(s) URL with a host")
        if "@" in parts.netloc:
            raise ValueError(f"{where}: the endpoint must not carry a user name or password")
        if parts.hostname.lower() not in LOCAL_HOSTS and not allow_remote:
            raise ValueError(f"{where}: the endpoint must be on 127.0.0.1 or localhost "
                             "(set MARKET_SCOUT_PEERS_ALLOW_REMOTE=1 to allow other hosts)")
        peers[address] = endpoint
    return peers


def env_peers() -> dict[str, str]:
    return parse_peers(os.getenv("MARKET_SCOUT_PEERS"), env_flag("MARKET_SCOUT_PEERS_ALLOW_REMOTE"))


def resolver_rules(root: Path) -> dict[str, str]:
    """Local mode: where replies go. The local demo client, plus MARKET_SCOUT_PEERS (which may not redefine it)."""
    client = local_client_address(root)
    rules = {client: os.getenv("MARKET_SCOUT_LOCAL_CLIENT_ENDPOINT", "http://127.0.0.1:8004/submit")}
    peers = env_peers()
    if client in peers:
        raise ValueError("MARKET_SCOUT_PEERS lists the local demo client; set MARKET_SCOUT_LOCAL_CLIENT_ENDPOINT instead")
    return {**rules, **peers}


class RateLimit:
    """At most `limit` requests per sender in any `window` seconds (sliding window, in memory)."""

    def __init__(self, limit=RATE_LIMIT, window=RATE_WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.seen: dict[str, deque] = defaultdict(deque)

    def allow(self, sender: str) -> bool:
        now, q = self.clock(), self.seen[sender]
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.limit:
            return False
        q.append(now)
        return True


def build_protocol(service: MarketScout, allowed: set[str] | None = None, limiter: RateLimit | None = None):
    protocol = Protocol(spec=chat_protocol_spec)
    allowed = set(allowed) if allowed is not None else allowed_senders(service.root)
    limiter = limiter or RateLimit()

    async def reply(ctx, sender, body):
        await ctx.send(sender, chat_message(json.dumps(body), end=True))

    @protocol.on_message(ChatMessage)
    async def handle_message(ctx: Context, sender: str, message: ChatMessage):
        if sender not in allowed:                         # not an agent we work for: no scan, no network, no files
            ctx.logger.warning(f"Market scout refused a request from {sender[:80]}")
            await reply(ctx, sender, {"status": "error", "error": "sender not allowed"})
            return
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=message.msg_id))
        if not limiter.allow(sender):
            await reply(ctx, sender, {"status": "rate_limited",
                                      "error": f"at most {limiter.limit} requests per {limiter.window // 60:.0f} minutes"})
            return
        text = "\n".join(part.text for part in message.content if isinstance(part, TextContent))
        if not text.strip():                              # reply anyway, so the sender isn't left waiting
            await reply(ctx, sender, {"status": "error", "error": "empty request; send MarketRequest JSON"})
            return
        request = None
        try:
            request = MarketRequest.model_validate_json(text[:20_000])
            response = (await service.run(request, sender, wait=False)).model_dump(mode="json")
        except Busy as exc:
            response = {"request_id": request.request_id if request else None, "status": "busy", "error": str(exc)}
        except Exception as exc:
            ctx.logger.error(f"Market scout request failed: {failure_message(exc)}")
            response = {"request_id": request.request_id if request else None, "status": "error",
                        "error": failure_message(exc), "error_type": type(exc).__name__}
        await reply(ctx, sender, response)

    @protocol.on_message(ChatAcknowledgement)
    async def handle_ack(ctx: Context, sender: str, message: ChatAcknowledgement):
        ctx.logger.debug(f"Market report acknowledged by {sender}")

    return protocol


class Health(Model):
    status: str = "ok"


def bind_localhost():
    """uagents 0.26.1 binds every agent server to uagents.asgi.HOST (0.0.0.0) and has no parameter for it."""
    import uagents.asgi

    uagents.asgi.HOST = os.getenv("MARKET_SCOUT_BIND_HOST", "127.0.0.1")
    return uagents.asgi.HOST


def make_agent(root: Path, port=8006, local_only=False, offline=None):
    seed = os.getenv("MARKET_SCOUT_SEED")
    if not seed:
        raise ValueError("Set MARKET_SCOUT_SEED to a private, stable seed before serving")
    if not listed_senders() and not local_only:
        raise ValueError("Set MARKET_SCOUT_ALLOWED_SENDERS to the agent addresses the scout works for, or pass "
                         "--local-only to take requests only from this machine's local client")
    peers = env_peers()                                   # validated in both modes: a typo refuses to start
    mailbox = os.getenv("MARKET_SCOUT_MAILBOX", "false").lower() == "true"
    if mailbox and peers:
        LOG.warning("MARKET_SCOUT_PEERS is ignored with MARKET_SCOUT_MAILBOX=true (replies go through the Almanac)")
    for address in sorted(set(peers) - listed_senders()):
        LOG.warning("peer %s... is not in MARKET_SCOUT_ALLOWED_SENDERS; its requests will be refused", address[:20])
    bind_localhost()
    local_options = {} if mailbox else {
        "registration_policy": LocalRegistration(),
        "resolve": RulesBasedResolver(resolver_rules(root)),
    }
    # uagents 0.26.1: an explicit endpoint overrides the mailbox, so only pass one in local mode. In mailbox mode the
    # inspector routes, which any web page can call (CORS *), are served only with MARKET_SCOUT_INSPECTOR on, for the
    # one-time POST /connect that creates the Agentverse mailbox; GET /health keeps the server, and so the agent,
    # running without them (with no REST route, 0.26.1 stops a mailbox agent at once). Registration uses the Almanac
    # API alone, waiting 10 s (0.26.1 waits 1 s; the API takes about 2 s): no on-chain registration, which wants FET.
    where = {} if mailbox else {"endpoint": [f"http://127.0.0.1:{port}/submit"]}
    inspector = mailbox and os.getenv("MARKET_SCOUT_INSPECTOR", "false").strip().lower() in TRUE
    if mailbox:
        local_options = {"registration_policy": AlmanacApiRegistrationPolicy(timeout=10)}
    agent = Agent(name="market_scout", seed=seed, port=port, mailbox=mailbox, enable_agent_inspector=inspector,
                  publish_agent_details=mailbox, report_events=False, **where, **local_options)
    if mailbox:
        @agent.on_rest_get("/health", Health)
        async def health(ctx: Context) -> Health:
            return Health()
    agent.include(build_protocol(MarketScout(root, offline=offline)), publish_manifest=mailbox)
    return agent


def run_client(target: str, request: MarketRequest, port=8004, endpoint="http://127.0.0.1:8006/submit",
               root: Path = DEFAULT_ROOT):
    """Send one request, print the reply, then stop."""
    bind_localhost()
    client = Agent(name="market-scout-local-client", seed=local_client_seed(root), port=port,
                   endpoint=[f"http://127.0.0.1:{port}/submit"], enable_agent_inspector=False,
                   resolve=RulesBasedResolver({target: endpoint}), registration_policy=LocalRegistration(),
                   publish_agent_details=False, report_events=False)
    protocol = Protocol(spec=chat_protocol_spec)

    @client.on_event("startup")
    async def send(ctx: Context):
        await ctx.send(target, chat_message(request.model_dump_json()))

    @protocol.on_message(ChatMessage)
    async def receive(ctx: Context, sender: str, message: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=message.msg_id))
        for part in message.content:
            if isinstance(part, TextContent):
                print(part.text, flush=True)
        if any(isinstance(part, EndSessionContent) for part in message.content):
            os._exit(0)                                   # uagents has no clean stop from a handler

    @protocol.on_message(ChatAcknowledgement)
    async def ack(ctx: Context, sender: str, message: ChatAcknowledgement):
        ctx.logger.info("Market scout acknowledged the request")

    client.include(protocol)
    client.run()
