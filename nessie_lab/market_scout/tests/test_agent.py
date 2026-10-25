import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

from uagents import Model
from uagents_core.contrib.protocols.chat import ChatMessage, TextContent

from conftest import FakePages
from market_scout.agent import RateLimit, allowed_senders, build_protocol, local_client_address
from market_scout.extract import rule_reader
from market_scout.runner import MarketScout


def handler_for(service, **kw):
    kw.setdefault("allowed", {"agent1sender"})
    return build_protocol(service, **kw).signed_message_handlers[Model.build_schema_digest(ChatMessage)]


async def send(root, text, service=None, handler=None, sender="agent1sender"):
    service = service or MarketScout(root, readers=[rule_reader], pages=FakePages(), use_apify=False)
    handler = handler or handler_for(service)
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
    msg = ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type="text", text=text)])
    await handler(ctx, sender, msg)
    reply = ctx.send.await_args_list[-1].args[1]
    return json.loads(reply.content[0].text), reply


async def test_chat_request_returns_filename_and_positions(root):
    body, reply = await send(root, json.dumps({"request_id": "chat-1", "items": ["Margherita Pizza"]}))
    assert body["status"] == "ok" and body["filename"].startswith("data/market_reports/M-")
    assert body["positions"] == {"Margherita Pizza": "below_market"}
    assert reply.content[-1].type == "end-session"


async def test_bad_request_returns_error_not_crash(root):
    body, _ = await send(root, json.dumps({"request_id": "chat-2", "items": ["Pizza Hawaii"]}))
    assert body["status"] == "error" and "not on our dinner menu" in body["error"]


async def test_unlisted_sender_is_refused_without_a_scan(root, monkeypatch):
    pages = FakePages()
    service = MarketScout(root, readers=[rule_reader], pages=pages, use_apify=False)
    monkeypatch.delenv("MARKET_SCOUT_ALLOWED_SENDERS", raising=False)
    handler = handler_for(service, allowed=None)          # env empty: only the local demo client
    body, _ = await send(root, json.dumps({"request_id": "x-1"}), service, handler, sender="agent1stranger")
    assert body == {"status": "error", "error": "sender not allowed"} and pages.calls == []
    body, _ = await send(root, json.dumps({"request_id": "x-1"}), service, handler, sender=local_client_address(root))
    assert body["status"] == "ok"


def test_allowlist_comes_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("MARKET_SCOUT_ALLOWED_SENDERS", " agent1a, agent1b ,,")
    assert allowed_senders(tmp_path) == {"agent1a", "agent1b"}
    monkeypatch.setenv("MARKET_SCOUT_ALLOWED_SENDERS", "")
    assert allowed_senders(tmp_path) == {local_client_address(tmp_path)}


async def test_rate_limit_is_five_per_sender_per_ten_minutes(root):
    t = [0.0]
    service = MarketScout(root, readers=[rule_reader], pages=FakePages(), use_apify=False)
    handler = handler_for(service, allowed={"agent1sender", "agent1other"}, limiter=RateLimit(clock=lambda: t[0]))
    statuses = [(await send(root, "", service, handler))[0]["status"] for _ in range(6)]
    assert statuses == ["error"] * 5 + ["rate_limited"]
    assert (await send(root, "", service, handler, sender="agent1other"))[0]["status"] == "error"   # per sender
    t[0] = 599
    assert (await send(root, "", service, handler))[0]["status"] == "rate_limited"
    t[0] = 601
    assert (await send(root, "", service, handler))[0]["status"] == "error"


async def test_busy_scout_replies_at_once_instead_of_queueing(root):
    service = MarketScout(root, readers=[rule_reader], pages=FakePages(), use_apify=False)
    handler = handler_for(service)
    async with service.lock:                              # a scan is running
        body, _ = await send(root, json.dumps({"request_id": "busy-1"}), service, handler)
    assert body["status"] == "busy" and body["request_id"] == "busy-1"
    body, _ = await send(root, json.dumps({"request_id": "busy-1"}), service, handler)
    assert body["status"] == "ok"


async def test_invalid_request_id_is_an_error_reply(root):
    body, _ = await send(root, json.dumps({"request_id": "../../etc"}))
    assert body["status"] == "error" and body["error_type"] == "ValidationError"
