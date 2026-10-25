"""Agent Chat Protocol boundary for the ASI orchestrator, the Bookkeeper and Verifier agents, and the local client."""
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from uagents import Agent, Context, Protocol
from uagents.resolver import RulesBasedResolver
from uagents_core.identity import Identity
from uagents_core.registration import AgentRegistrationPolicy
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, TextContent, chat_protocol_spec,
)

from .contracts import Drafted, ReportRequest, RevisionRequest, VerifyRequest
from .runner import Bookkeeper, failure_message
from .verifier import Verifier

LOCAL_CLIENT_SEED = "bookkeeper-local-demo-client-not-for-production"


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


async def acknowledge(ctx: Context, sender: str, message: ChatMessage):
    """Acknowledge the message ID and return its text."""
    await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=message.msg_id))
    return "\n".join(part.text for part in message.content if isinstance(part, TextContent))


def build_protocol(service: Bookkeeper, verifier_address: str):
    """Bind requests to the service while retaining the sender's session context.

    A new draft goes to the Verifier, which may send it back for revision rounds; the orchestrator is answered when the
    Verifier replies with the published report."""
    protocol = Protocol(spec=chat_protocol_spec)
    pending = {}                   # report_id -> orchestrator address, while the Verifier works

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
                response = service.complete(body["report_id"]).model_dump(mode="json")
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
        request = None
        try:
            request = ReportRequest.model_validate_json(text)
            outcome = await service.run(request, sender)
            if isinstance(outcome, Drafted):
                pending[outcome.report_id] = sender
                await send_draft(ctx, outcome)
                return
            response = outcome.model_dump(mode="json")
        except Exception as exc:
            ctx.logger.error(f"Bookkeeper request failed: {failure_message(exc)}")
            response = error_response(request.request_id if request else None, exc)
        await ctx.send(sender, chat_message(json.dumps(response), end=True))

    @protocol.on_message(ChatAcknowledgement)
    async def handle_ack(ctx: Context, sender: str, message: ChatAcknowledgement):
        ctx.logger.debug(f"Message acknowledged by {sender}")

    return protocol


def build_verifier_protocol(verifier: Verifier, bookkeeper_address: str):
    """Verify drafts sent by the configured Bookkeeper; reply with a revision request or the published report's filename."""
    protocol = Protocol(spec=chat_protocol_spec)

    @protocol.on_message(ChatMessage)
    async def handle_message(ctx: Context, sender: str, message: ChatMessage):
        text = await acknowledge(ctx, sender, message)
        if not text.strip():
            return
        request = None
        try:
            if sender != bookkeeper_address:
                raise ValueError("Verification failed: only the configured Bookkeeper may request verification")
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


def local_agent(name, seed_var, mailbox_var, port, peers):
    """A uAgent on localhost; outside mailbox mode it resolves only the given peers {address: endpoint}."""
    seed = os.getenv(seed_var)
    if not seed:
        raise ValueError(f"Set {seed_var} to a private, stable seed before serving")
    mailbox = os.getenv(mailbox_var, "false").lower() == "true"
    local_options = {} if mailbox else {"registration_policy": LocalRegistration(), "resolve": RulesBasedResolver(peers)}
    agent = Agent(name=name, seed=seed, port=port, mailbox=mailbox,
                  endpoint=[f"http://127.0.0.1:{port}/submit"], enable_agent_inspector=False,
                  publish_agent_details=mailbox, report_events=False, **local_options)
    return agent, mailbox


def make_agent(root: Path, port=8001):
    """Create the Bookkeeper uAgent; mailbox use requires explicit environment configuration."""
    verifier = peer_address("BOOKKEEPER_VERIFIER_ADDRESS", "VERIFIER_SEED")
    agent, mailbox = local_agent("bookkeeper", "BOOKKEEPER_SEED", "BOOKKEEPER_MAILBOX", port, {
        Identity.from_seed(LOCAL_CLIENT_SEED, 0).address:
            os.getenv("BOOKKEEPER_LOCAL_CLIENT_ENDPOINT", "http://127.0.0.1:8002/submit"),
        verifier: os.getenv("BOOKKEEPER_VERIFIER_ENDPOINT", "http://127.0.0.1:8003/submit")})
    agent.include(build_protocol(Bookkeeper(root, model=os.getenv("ASI_MODEL", "asi1")), verifier), publish_manifest=mailbox)
    return agent


def make_verifier_agent(root: Path, port=8003):
    """Create the Verifier uAgent, which answers only the configured Bookkeeper."""
    bookkeeper = peer_address("VERIFIER_BOOKKEEPER_ADDRESS", "BOOKKEEPER_SEED")
    agent, mailbox = local_agent("bookkeeper-verifier", "VERIFIER_SEED", "VERIFIER_MAILBOX", port, {
        bookkeeper: os.getenv("VERIFIER_BOOKKEEPER_ENDPOINT", "http://127.0.0.1:8001/submit")})
    agent.include(build_verifier_protocol(Verifier(root), bookkeeper), publish_manifest=mailbox)
    return agent


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
        print("Response received; stop this client with Ctrl-C.", flush=True)

    @protocol.on_message(ChatAcknowledgement)
    async def ack(ctx: Context, sender: str, message: ChatAcknowledgement):
        ctx.logger.info("Bookkeeper acknowledged the request")

    client.include(protocol)
    client.run()
