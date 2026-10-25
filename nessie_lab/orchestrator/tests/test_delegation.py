"""Part B: the orchestrator delegates first. In `serve`, with SCOUT_ADDR / BOOKKEEPER_ADDR set, delegation is the
DEFAULT (ORCHESTRATOR_USE_AGENTS=0/1 overrides); the in-process fallback is marked degraded; a chat never starts a
Bookkeeper run unless ORCHESTRATOR_USE_AGENTS=1 is explicit; the Bookkeeper's late reply is matched by request_id.
Fake core, no network."""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import pytest
from uagents import Model
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage, TextContent

import fakes
from orchestrator import agent as agent_mod
from orchestrator.agent import Orchestrator, PeerDispatch, PeerReplies, build_protocol

pytestmark = pytest.mark.usefixtures("fake_core")
SCOUT, BK, ME = "agent1qscout", "agent1qbookkeeper", "agent1qme"


def chat(text):
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=[TextContent(type="text", text=text)])


def text_of(message):
    return "\n".join(p.text for p in message.content if isinstance(p, TextContent))


def _agent(tmp_path, monkeypatch, **env):
    monkeypatch.setenv("ORCHESTRATOR_SEED", f"delegation seed {uuid4().hex}")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return agent_mod.make_agent(tmp_path, port=8951)


async def test_delegation_is_the_default_in_serve_when_addresses_are_set(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch, SCOUT_ADDR=SCOUT, BOOKKEEPER_ADDR=BK)
    svc = a.orchestrator
    assert svc.use_agents is True and svc.delegating and svc.bk_from_chat is False
    d = svc.dispatch(SimpleNamespace())
    assert isinstance(d, PeerDispatch) and d.scout == SCOUT and d.bookkeeper == BK and d.replies is svc.replies
    assert d.bk_from_chat is False and d.me == a.address
    assert ("POST", "/bookkeeper") in a._rest_handlers


@pytest.mark.parametrize("flag,use,chat_runs", [("0", False, False), ("1", True, True)])
async def test_use_agents_still_overrides(tmp_path, monkeypatch, flag, use, chat_runs):
    svc = _agent(tmp_path, monkeypatch, SCOUT_ADDR=SCOUT, BOOKKEEPER_ADDR=BK, ORCHESTRATOR_USE_AGENTS=flag).orchestrator
    assert svc.use_agents is use and svc.bk_from_chat is chat_runs
    assert (svc.dispatch(SimpleNamespace()) is None) is (not use)
    if use:                                               # a non-owner's chat never asks for a run, even with =1
        assert svc.dispatch(SimpleNamespace(), owner=False).bk_from_chat is False


async def test_no_addresses_means_in_process(tmp_path, monkeypatch):
    svc = _agent(tmp_path, monkeypatch).orchestrator
    assert svc.use_agents is False and svc.dispatch(SimpleNamespace()) is None


def test_local_client_default_endpoint_is_not_the_scouts_port():
    assert agent_mod.LOCAL_CLIENT_ENDPOINT.endswith(":8007/submit")       # the Market scout listens on 8006


async def test_served_brief_asks_the_scout_first_and_falls_back_degraded():
    svc = Orchestrator("root", {"agent1qowner"}, use_agents=True, scout=SCOUT, address=ME)
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock(), send_and_receive=AsyncMock(side_effect=asyncio.TimeoutError))
    handler = build_protocol(svc, {SCOUT}).signed_message_handlers[Model.build_schema_digest(ChatMessage)]
    await handler(ctx, "agent1qowner", chat("how are we doing"))
    reply = ctx.send.await_args_list[-1].args[1]
    assert ctx.send_and_receive.await_args.args[0] == SCOUT               # delegated first
    assert "degraded" in text_of(reply) and "market scout agent timed out" in text_of(reply)
    assert fakes.BRIEFS and "scan" in fakes.BRIEFS[-1]                    # the brief reused the fallback scan
    assert [n for n, _ in fakes.CALLS] == ["scan_market"]                 # the in-process fallback ran once


async def test_served_brief_uses_the_scouts_report_when_it_answers(monkeypatch):
    import sys

    def adopt(session, filename, request_id, args):
        result = {"positions": [{"our_item": "Tiramisu", "position": "at_par"}], "via": "market_scout agent"}
        session.log.append({"name": "scan_market", "args": args, "result": result})
        return result

    monkeypatch.setattr(sys.modules["orchestrator.tools"], "adopt_market_report", adopt, raising=False)
    body = {"request_id": "x", "status": "ok", "filename": "data/market_reports/M-x.json"}
    svc = Orchestrator("root", {"agent1qowner"}, use_agents=True, scout=SCOUT, address=ME)
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock(),
                          send_and_receive=AsyncMock(return_value=(chat(json.dumps(body)), None)))
    result, _ = await svc.run(ctx, "agent1qowner", "status")
    assert result["degraded"] is False and fakes.CALLS == []
    assert fakes.BRIEFS[-1]["scan"]["via"] == "market_scout agent"


async def test_chat_get_findings_never_starts_a_bookkeeper_run_by_default():
    d = PeerDispatch(SimpleNamespace(send_and_receive=AsyncMock()), bookkeeper=BK, bk_from_chat=False)
    result = await d(fakes.Session("r"), "get_findings", {})
    assert d.ctx.send_and_receive.await_count == 0 and result["findings"]
    assert "never starts a Bookkeeper run" in d.notes[0] and not d.degraded


async def test_bookkeeper_reply_is_matched_by_request_id_outside_the_session():
    """The Bookkeeper answers from its Verifier handler (another session): the protocol hands the reply to the
    request waiting for it, and still only acknowledges it (no ping-pong)."""
    svc = Orchestrator("root", {"agent1qowner"}, use_agents=True, bookkeeper=BK, address=ME)
    handler = build_protocol(svc, {BK}).signed_message_handlers[Model.build_schema_digest(ChatMessage)]
    sent = []

    async def send(dest, msg):
        sent.append((dest, msg))
        if dest == BK and isinstance(msg, ChatMessage):
            rid = json.loads(text_of(msg))["request_id"]
            stranger = {"request_id": "someone-else", "filename": "data/reports/R-x.json"}
            reply = {"request_id": rid, "report_id": "R-1", "filename": "data/reports/R-1.json", "as_of": "2026-10-22",
                     "verified": True}
            asyncio.get_running_loop().call_soon(lambda: asyncio.ensure_future(handler(ctx, BK, chat(json.dumps(stranger)))))
            asyncio.get_running_loop().call_later(0.01, lambda: asyncio.ensure_future(
                handler(ctx, BK, chat(json.dumps(reply)))))

    ctx = SimpleNamespace(send=send, logger=Mock())
    d = PeerDispatch(ctx, bookkeeper=BK, replies=svc.replies, me=ME)
    reply, why = await d.ask_bookkeeper({"request_id": "orch-bk-1", "nessie_tag": "boh-1loc", "as_of": "2026-10-22"},
                                        timeout=2)
    await asyncio.sleep(0.05)
    assert why == "" and reply["filename"] == "data/reports/R-1.json" and reply["verified"] is True
    to_bk = [m for dest, m in sent if dest == BK]
    assert isinstance(to_bk[0], ChatMessage) and all(isinstance(m, ChatAcknowledgement) for m in to_bk[1:])
    assert svc.replies.waiting == {}


async def test_bookkeeper_reply_timeout_and_waiting_cap():
    replies = PeerReplies(limit=1)
    d = PeerDispatch(SimpleNamespace(send=AsyncMock()), bookkeeper=BK, replies=replies)
    assert await d.ask_bookkeeper({"request_id": "r1"}, timeout=0.05) == (None, "timed out")
    assert replies.waiting == {}
    replies.expect(BK, "r2")
    assert (await d.ask_bookkeeper({"request_id": "r3"}, timeout=0.05))[1] == "too many peer requests waiting"
