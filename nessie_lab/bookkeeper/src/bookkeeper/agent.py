"""Agent Chat Protocol boundary for the ASI orchestrator and local client."""
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

from .contracts import ReportRequest
from .runner import Bookkeeper, failure_message

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


def build_protocol(service: Bookkeeper):
    """Bind requests to the service while retaining the sender's session context."""
    protocol = Protocol(spec=chat_protocol_spec)

    @protocol.on_message(ChatMessage)
    async def handle_message(ctx: Context, sender: str, message: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(timezone.utc), acknowledged_msg_id=message.msg_id))
        text = "\n".join(part.text for part in message.content if isinstance(part, TextContent))
        if not text.strip():
            return
        request = None
        try:
            request = ReportRequest.model_validate_json(text)
            result = await service.run(request, sender)
            response = result.model_dump(mode="json")
        except Exception as exc:
            ctx.logger.error(f"Bookkeeper request failed: {failure_message(exc)}")
            response = {"request_id": request.request_id if request else None, "status": "error",
                        "error": failure_message(exc),
                        "error_type": type(exc).__name__,
                        "trace_id": getattr(exc, "bookkeeper_trace_id", None)}
        await ctx.send(sender, chat_message(json.dumps(response), end=True))

    @protocol.on_message(ChatAcknowledgement)
    async def handle_ack(ctx: Context, sender: str, message: ChatAcknowledgement):
        ctx.logger.debug(f"Report message acknowledged by {sender}")

    return protocol


def make_agent(root: Path, port=8001):
    """Create a local uAgent; mailbox use requires explicit environment configuration."""
    seed = os.getenv("BOOKKEEPER_SEED")
    if not seed:
        raise ValueError("Set BOOKKEEPER_SEED to a private, stable seed before serving")
    mailbox = os.getenv("BOOKKEEPER_MAILBOX", "false").lower() == "true"
    local_options = {} if mailbox else {
        "registration_policy": LocalRegistration(),
        "resolve": RulesBasedResolver({Identity.from_seed(LOCAL_CLIENT_SEED, 0).address:
            os.getenv("BOOKKEEPER_LOCAL_CLIENT_ENDPOINT", "http://127.0.0.1:8002/submit")}),
    }
    agent = Agent(name="bookkeeper", seed=seed, port=port, mailbox=mailbox,
                  endpoint=[f"http://127.0.0.1:{port}/submit"], enable_agent_inspector=False,
                  publish_agent_details=mailbox, report_events=False, **local_options)
    agent.include(build_protocol(Bookkeeper(root, model=os.getenv("ASI_MODEL", "asi1"))), publish_manifest=mailbox)
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
