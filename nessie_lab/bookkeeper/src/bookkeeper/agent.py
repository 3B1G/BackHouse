"""Agent Chat Protocol boundary for the ASI orchestrator, the Bookkeeper and Verifier agents, and the local client."""
import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import uagents.asgi
from pydantic import ValidationError
from uagents import Agent, Context, Model, Protocol
from uagents.mailbox import MailboxClient
from uagents.registration import AlmanacApiRegistrationPolicy
from uagents.resolver import RulesBasedResolver
from uagents_core.config import AgentverseConfig
from uagents_core.identity import Identity
from uagents_core.registration import AgentRegistrationPolicy
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, TextContent, chat_protocol_spec,
)

from .contracts import Drafted, ReportRequest, RevisionRequest, VerifyRequest
from .runner import Bookkeeper, failure_message
from .verifier import Verifier

LOG = logging.getLogger("bookkeeper")
LOCAL_CLIENT_SEED = "bookkeeper-local-demo-client-not-for-production"
ALMANAC_API_TIMEOUT = 10        # seconds; uagents 0.26.1 waits 1 s, and the Almanac API takes about 2 s to answer
REFUSAL_INTERVAL = 3600         # seconds between two refusal notes to one sender, so a refusing peer can't make us loop
REQUEST_FORMAT = ('Send one JSON object: {"request_id": "<your name for this report>"}, optionally with '
                  '"as_of": "YYYY-MM-DD" (the cutoff; default: the latest transaction) and "nessie_tag" (default '
                  '"boh-1loc"). A report covers the one restaurant in the snapshot, from its opening balances through '
                  'as_of.')


def request_problem(text: str) -> str:
    """What is wrong with a message that is not a ReportRequest, in words a person or ASI:One's model can act on."""
    try:
        body = json.loads(text)
    except ValueError:
        return f"That is not JSON. {REQUEST_FORMAT}"
    if not isinstance(body, dict):
        return f"That is not a JSON object. {REQUEST_FORMAT}"
    if unknown := sorted(set(body) - set(ReportRequest.model_fields)):
        return f"Unknown field(s): {', '.join(map(str, unknown))[:200]}. {REQUEST_FORMAT}"
    try:
        ReportRequest.model_validate(body)
    except ValidationError as exc:
        return "; ".join(f"{'.'.join(map(str, e['loc'])) or 'request'}: {e['msg']}" for e in exc.errors()[:3]) + \
            f". {REQUEST_FORMAT}"
    return REQUEST_FORMAT


def dollars(cents) -> str:
    return f"${cents / 100:,.2f}"


def report_summary(root: Path, filename: str) -> str | None:
    """One paragraph a person can read: what the published report found, largest impact first."""
    try:
        report = json.loads((root / filename).read_text())
        summary, findings, as_of = report["summary"], report["findings"], report["as_of"]
        if not findings:
            return f"No findings through {as_of}."
        largest = sorted(findings, key=lambda f: abs(f["amounts"]["impact_cents"]), reverse=True)[:3]
        return (f"{len(findings)} findings through {as_of} ({summary['errors']} errors, {summary['needs_owner']} for "
                f"the owner to decide): {dollars(summary['total_impact_cents'])} total impact, "
                f"{dollars(summary['recoverable_cents'])} recoverable, {dollars(summary['ongoing_monthly_cents'])} a "
                f"month ongoing. Largest: " + "; ".join(
                    f"{f['root_cause'].replace('_', ' ')}, {f['counterparty']} "
                    f"({dollars(abs(f['amounts']['impact_cents']))})" for f in largest) + ".")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


class LocalRegistration(AgentRegistrationPolicy):
    """Keep the localhost demo off the public agent registry."""

    async def register(self, agent_identifier, identity, protocols, endpoints, metadata=None):
        return None


def chat_message(text: str, end=False):
    """Create a standard chat message, ending the request only after its final response."""
    content = [TextContent(type="text", text=text)]
    if end:
        content.append(EndSessionContent(type="end-session"))
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=content)


def error_response(request_id, exc):
    """The safe error reply: a diagnostic and trace ID, never a filename."""
    return {"request_id": request_id, "status": "error", "error": failure_message(exc), "error_type": type(exc).__name__,
            "trace_id": getattr(exc, "bookkeeper_trace_id", None)}


def refuser(note: str):
    """refuse(ctx, sender): log every refused sender, but send each the note at most once an hour, so a refusing peer
    can't make two agents loop."""
    told = {}                      # sender -> when it was last sent the note

    async def refuse(ctx: Context, sender: str):
        ctx.logger.warning(f"Refused a request from {sender}")
        now = time.monotonic()
        for old in [s for s, at in told.items() if now - at >= REFUSAL_INTERVAL]:
            del told[old]
        if sender not in told:
            told[sender] = now
            await ctx.send(sender, chat_message(note, end=True))
    return refuse


async def acknowledge(ctx: Context, sender: str, message: ChatMessage):
    """Acknowledge the message ID and return its text."""
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=message.msg_id))
    return "\n".join(part.text for part in message.content if isinstance(part, TextContent))


def build_protocol(service: Bookkeeper, verifier_address: str, allowed: set[str] | None = None,
                   orchestrator: str | None = None):
    """Bind requests to the service while retaining the sender's session context.

    A new draft goes to the Verifier, which may send it back for revision rounds; the orchestrator is answered when the
    Verifier replies with the published report. allowed: the only senders whose requests run (None: anyone); others
    get a note at most once an hour. Senders other than the orchestrator are told a new run has started, and every
    report reply carries a readable `summary`."""
    protocol = Protocol(spec=chat_protocol_spec)
    pending = {}                   # report_id -> orchestrator address, while the Verifier works
    refuse = refuser("This Bookkeeper takes requests only from the Hermes orchestrator and its owner."
                     + (f" Ask the orchestrator instead: {orchestrator}" if orchestrator else ""))

    def report_reply(result) -> dict:
        response = result.model_dump(mode="json")
        if summary := report_summary(service.root, result.filename):
            response["summary"] = summary
        return response

    async def send_draft(ctx: Context, drafted: Drafted):
        await ctx.send(verifier_address, chat_message(VerifyRequest(
            request_id=drafted.request_id, report_id=drafted.report_id, draft=drafted.draft, round=drafted.round).model_dump_json()))
        ctx.logger.info(f"Request {drafted.request_id}: draft round {drafted.round} sent to the Verifier")

    async def verified(ctx: Context, text: str):
        try:
            body = json.loads(text)
            orchestrator = pending.get(body.get("report_id"))
        except (ValueError, AttributeError):
            orchestrator = None
        if orchestrator is None:
            ctx.logger.warning("Ignoring a Verifier reply for no pending report")
            return
        if body.get("type") == "revise":
            try:
                await send_draft(ctx, await service.revise(RevisionRequest.model_validate(body)))
                return
            except Exception as exc:
                ctx.logger.error(f"Revision failed: {failure_message(exc)}")
                body = dict(error_response(body.get("request_id"), exc), report_id=body.get("report_id"))
        pending.pop(body.get("report_id"), None)
        if body.get("status") == "error":
            ctx.logger.error(f"Verification failed: {body.get('error')}")
            response = {k: body.get(k) for k in ("request_id", "status", "error", "error_type", "trace_id")}
        else:
            try:
                response = report_reply(service.complete(body["report_id"]))
            except Exception as exc:
                ctx.logger.error(f"Recording the verified report failed: {failure_message(exc)}")
                response = error_response(body.get("request_id"), exc)
        await ctx.send(orchestrator, chat_message(json.dumps(response), end=True))

    @protocol.on_message(ChatMessage)
    async def handle_message(ctx: Context, sender: str, message: ChatMessage):
        text = await acknowledge(ctx, sender, message)
        if not text.strip():
            return
        if sender == verifier_address:
            await verified(ctx, text)
            return
        if allowed is not None and sender not in allowed:
            await refuse(ctx, sender)
            return
        try:
            request = ReportRequest.model_validate_json(text)
        except ValidationError as exc:
            problem = request_problem(text)
            ctx.logger.error(f"Bookkeeper request failed: {problem} (from {sender}: {text[:200]!r})")
            response = {**error_response(None, exc), "error": problem}
            await ctx.send(sender, chat_message(json.dumps(response), end=True))
            return
        try:
            if sender != orchestrator and not service.started(request, sender):
                await ctx.send(sender, chat_message(       # prose, not JSON: the orchestrator's reply matcher skips it
                    f"Working on {request.request_id}: the Bookkeeper investigates the books, then its Verifier "
                    "repeats the investigation and checks every finding. This can take up to an hour; the report "
                    "comes back here."))
            outcome = await service.run(request, sender)
            if isinstance(outcome, Drafted):
                pending[outcome.report_id] = sender
                await send_draft(ctx, outcome)
                return
            response = report_reply(outcome)
        except Exception as exc:
            ctx.logger.error(f"Bookkeeper request failed: {failure_message(exc)}")
            response = error_response(request.request_id, exc)
        await ctx.send(sender, chat_message(json.dumps(response), end=True))

    @protocol.on_message(ChatAcknowledgement)
    async def handle_ack(ctx: Context, sender: str, message: ChatAcknowledgement):
        ctx.logger.debug(f"Message acknowledged by {sender}")

    return protocol


def build_verifier_protocol(verifier: Verifier, bookkeeper_address: str):
    """Verify drafts sent by the configured Bookkeeper; reply with a revision request or the published report's filename.
    Anyone else gets a note at most once an hour, and nothing is verified."""
    protocol = Protocol(spec=chat_protocol_spec)
    refuse = refuser("This is the Hermes Bookkeeper's Verifier: it checks only its Bookkeeper's drafts.")

    @protocol.on_message(ChatMessage)
    async def handle_message(ctx: Context, sender: str, message: ChatMessage):
        text = await acknowledge(ctx, sender, message)
        if not text.strip():
            return
        if sender != bookkeeper_address:
            await refuse(ctx, sender)
            return
        request = None
        try:
            request = VerifyRequest.model_validate_json(text)
            outcome = await verifier.verify(request)
            response = outcome.model_dump(mode="json")
        except Exception as exc:
            ctx.logger.error(f"Verification failed: {failure_message(exc)}")
            outcome, response = None, dict(error_response(request.request_id if request else None, exc),
                                           report_id=request.report_id if request else None)
        await ctx.send(sender, chat_message(json.dumps(response), end=not isinstance(outcome, RevisionRequest)))

    @protocol.on_message(ChatAcknowledgement)
    async def handle_ack(ctx: Context, sender: str, message: ChatAcknowledgement):
        ctx.logger.debug(f"Verification reply acknowledged by {sender}")

    return protocol


def peer_address(address_var, seed_var):
    """The other agent's address: set explicitly, or derived from its seed when both agents share one .env."""
    if address := os.getenv(address_var):
        return address
    if seed := os.getenv(seed_var):
        return Identity.from_seed(seed, 0).address
    raise ValueError(f"Set {address_var} (or {seed_var} when both agents share this .env) before serving")


class Health(Model):
    status: str = "ok"


def env_flag(var):
    return os.getenv(var, "false").lower() == "true"


def keep_serving(agent):
    """uagents 0.26.1 runs a mailbox agent's server only if it has a REST route, and without the server it stops at
    once (MailboxClient.run returns immediately). Give it one, on loopback: the mailbox, not this server, delivers."""
    uagents.asgi.HOST = "127.0.0.1"

    @agent.on_rest_get("/health", Health)
    async def health(ctx: Context) -> Health:
        return Health()


def local_agent(name, prefix, port, peers):
    """A uAgent on localhost; outside mailbox mode it resolves only the given peers {address: endpoint}.

    In mailbox mode it passes no endpoint (in uagents 0.26.1 an explicit endpoint overrides mailbox=True). The
    inspector routes, which any web page can call (CORS *), are served only while <prefix>_INSPECTOR=true, for the
    one-time POST /connect that creates the Agentverse mailbox."""
    seed = os.getenv(f"{prefix}_SEED")
    if not seed:
        raise ValueError(f"Set {prefix}_SEED to a private, stable seed before serving")
    mailbox = env_flag(f"{prefix}_MAILBOX")
    if mailbox:
        # the Almanac API alone: a mailbox agent needs no on-chain registration, which wants FET in its wallet
        options = {"mailbox": True, "enable_agent_inspector": env_flag(f"{prefix}_INSPECTOR"),
                   "registration_policy": AlmanacApiRegistrationPolicy(timeout=ALMANAC_API_TIMEOUT)}
    else:
        options = {"endpoint": [f"http://127.0.0.1:{port}/submit"], "enable_agent_inspector": False,
                   "registration_policy": LocalRegistration(), "resolve": RulesBasedResolver(peers)}
    agent = Agent(name=name, seed=seed, port=port, publish_agent_details=mailbox, report_events=False, **options)
    if mailbox:
        keep_serving(agent)
    return agent, mailbox


def orchestrator_address():
    """The Hermes orchestrator: BOOKKEEPER_ORCHESTRATOR_ADDRESS, or derived from ORCHESTRATOR_SEED in this .env, or None."""
    try:
        return peer_address("BOOKKEEPER_ORCHESTRATOR_ADDRESS", "ORCHESTRATOR_SEED")
    except ValueError:
        return None


def allowed_senders(orchestrator):
    """Mailbox mode's only requesters: the orchestrator and BOOKKEEPER_ALLOWED_SENDERS (comma-separated addresses)."""
    extra = {a.strip() for a in os.getenv("BOOKKEEPER_ALLOWED_SENDERS", "").split(",") if a.strip()}
    return extra | ({orchestrator} if orchestrator else set())


def make_agent(root: Path, port=8001):
    """Create the Bookkeeper uAgent; mailbox use requires explicit environment configuration. In mailbox mode only
    allowed_senders() may request reports; in local mode only local peers can reach it, as before."""
    verifier = peer_address("BOOKKEEPER_VERIFIER_ADDRESS", "VERIFIER_SEED")
    agent, mailbox = local_agent("bookkeeper", "BOOKKEEPER", port, {
        Identity.from_seed(LOCAL_CLIENT_SEED, 0).address:
            os.getenv("BOOKKEEPER_LOCAL_CLIENT_ENDPOINT", "http://127.0.0.1:8002/submit"),
        verifier: os.getenv("BOOKKEEPER_VERIFIER_ENDPOINT", "http://127.0.0.1:8003/submit")})
    orchestrator = orchestrator_address()
    allowed = allowed_senders(orchestrator) if mailbox else None
    if mailbox and not allowed:
        LOG.warning("No orchestrator (ORCHESTRATOR_SEED or BOOKKEEPER_ORCHESTRATOR_ADDRESS) and no "
                    "BOOKKEEPER_ALLOWED_SENDERS: nobody can request a report")
    agent.include(build_protocol(Bookkeeper(root, model=os.getenv("ASI_MODEL", "asi1")), verifier, allowed, orchestrator),
                  publish_manifest=mailbox)
    return agent


def make_verifier_agent(root: Path, port=8003):
    """Create the Verifier uAgent, which answers only the configured Bookkeeper."""
    bookkeeper = peer_address("VERIFIER_BOOKKEEPER_ADDRESS", "BOOKKEEPER_SEED")
    agent, mailbox = local_agent("bookkeeper-verifier", "VERIFIER", port, {
        bookkeeper: os.getenv("VERIFIER_BOOKKEEPER_ENDPOINT", "http://127.0.0.1:8001/submit")})
    agent.include(build_verifier_protocol(Verifier(root), bookkeeper), publish_manifest=mailbox)
    return agent


def mailbox_exists(seed: str, transport: httpx.BaseTransport | None = None) -> bool:
    """Whether Agentverse has a mailbox for this seed's agent: the request its mailbox client polls, signed as it."""
    identity, agentverse = Identity.from_seed(seed, 0), AgentverseConfig()
    with httpx.Client(transport=transport, timeout=30) as http:
        response = http.get(f"{agentverse.agents_api}/{identity.address}/mailbox",
                            headers={"Authorization": f"Agent {MailboxClient(identity, agentverse).attestation}"})
    if response.status_code == 404:
        return False
    response.raise_for_status()
    return True


def mailboxes(prefixes=("BOOKKEEPER", "VERIFIER")):
    """(name, address, has a mailbox) for the agents whose seeds are <prefix>_SEED; the name is the prefix in lower case."""
    found = []
    for prefix in prefixes:
        seed = os.getenv(f"{prefix}_SEED")
        if not seed:
            raise ValueError(f"Set {prefix}_SEED to a private, stable seed first")
        found.append((prefix.lower(), Identity.from_seed(seed, 0).address, mailbox_exists(seed)))
    return found


def connect_mailbox(port: int, token: str, transport: httpx.BaseTransport | None = None):
    """Create the Agentverse mailbox of the agent serving on this machine's port with its inspector on (POST /connect,
    which uagents answers only from 127.0.0.1). Agentverse's refusal is raised, without the token."""
    with httpx.Client(transport=transport, timeout=60, trust_env=False) as http:
        response = http.post(f"http://127.0.0.1:{port}/connect", json={"user_token": token, "agent_type": "mailbox"})
    try:
        body = response.json()
    except ValueError:
        body = {}
    if response.status_code != 200 or not isinstance(body, dict) or not body.get("success"):
        detail = str(body.get("detail") if isinstance(body, dict) else "") or f"HTTP {response.status_code}"
        raise ValueError(f"Agentverse did not create the mailbox: {detail.replace(token, '[token]')[:300]}")


def run_client(target: str, request: ReportRequest, port=8002, endpoint="http://127.0.0.1:8001/submit"):
    """Send a JSON report request and print the final filename response."""
    client = Agent(name="bookkeeper-local-client", seed=LOCAL_CLIENT_SEED, port=port,
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
        if any(isinstance(part, EndSessionContent) for part in message.content):     # not the "working on it" note
            print("Response received; stop this client with Ctrl-C.", flush=True)

    @protocol.on_message(ChatAcknowledgement)
    async def ack(ctx: Context, sender: str, message: ChatAcknowledgement):
        ctx.logger.info("Bookkeeper acknowledged the request")

    client.include(protocol)
    client.run()
