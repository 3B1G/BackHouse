"""Agent Chat Protocol boundary for the ASI orchestrator and local client (same wiring as the Bookkeeper).

Who may ask: MARKET_SCOUT_ALLOWED_SENDERS, or else only the local demo client. That client's seed is private to this
machine: MARKET_SCOUT_LOCAL_CLIENT_SEED, or a random one generated on first use and kept (owner-only where the OS
allows) in data/market_scout/local_client_seed, which is gitignored. A seed in the source would let anyone sign as it.

Where it listens: uagents 0.26.1 has no host option (Agent takes only a port; uagents.asgi.ASGIServer.serve binds the
module constant uagents.asgi.HOST = "0.0.0.0"). serve sets that constant to 127.0.0.1 before starting, so the agent
is not reachable from the Wi-Fi; MARKET_SCOUT_BIND_HOST overrides it (e.g. 0.0.0.0 for a real deployment)."""
import json
import os
import secrets
import stat
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents.resolver import RulesBasedResolver
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, TextContent, chat_protocol_spec,
)
from uagents_core.identity import Identity
from uagents_core.registration import AgentRegistrationPolicy

from .contracts import MarketRequest
from .runner import Busy, MarketScout
from .shared import DEFAULT_ROOT, private_dir

SEED_FILE = "local_client_seed"
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


def bind_localhost():
    """uagents 0.26.1 binds every agent server to uagents.asgi.HOST (0.0.0.0) and has no parameter for it."""
    import uagents.asgi

    uagents.asgi.HOST = os.getenv("MARKET_SCOUT_BIND_HOST", "127.0.0.1")
    return uagents.asgi.HOST


def make_agent(root: Path, port=8003, local_only=False):
    seed = os.getenv("MARKET_SCOUT_SEED")
    if not seed:
        raise ValueError("Set MARKET_SCOUT_SEED to a private, stable seed before serving")
    if not listed_senders() and not local_only:
        raise ValueError("Set MARKET_SCOUT_ALLOWED_SENDERS to the agent addresses the scout works for, or pass "
                         "--local-only to take requests only from this machine's local client")
    bind_localhost()
    mailbox = os.getenv("MARKET_SCOUT_MAILBOX", "false").lower() == "true"
    local_options = {} if mailbox else {
        "registration_policy": LocalRegistration(),
        "resolve": RulesBasedResolver({local_client_address(root):
            os.getenv("MARKET_SCOUT_LOCAL_CLIENT_ENDPOINT", "http://127.0.0.1:8004/submit")}),
    }
    agent = Agent(name="market_scout", seed=seed, port=port, mailbox=mailbox,
                  endpoint=[f"http://127.0.0.1:{port}/submit"], enable_agent_inspector=False,
                  publish_agent_details=mailbox, report_events=False, **local_options)
    agent.include(build_protocol(MarketScout(root)), publish_manifest=mailbox)
    return agent


def run_client(target: str, request: MarketRequest, port=8004, endpoint="http://127.0.0.1:8003/submit",
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
