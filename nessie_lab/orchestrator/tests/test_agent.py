import asyncio
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from uagents import Model
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, MetadataContent, StartSessionContent, TextContent,
)
from uagents_core.contrib.protocols.chat.cards import create_card_response_content, validate_card_payload_json

import fakes
from orchestrator import agent as agent_mod
from orchestrator import register
from orchestrator.agent import (
    NOTICE_PREFIX, AskRequest, LocalFirstResolver, Orchestrator, PeerDispatch, RateLimit, build_ask_handler,
    build_protocol, parse_peers, review_card,
)
from orchestrator.turn import FOOTER

pytestmark = pytest.mark.usefixtures("fake_core")

SENDER = "agent1qsender"
SCOUT = "agent1qscout"
SRC = Path(agent_mod.__file__).parent


def handler_for(service, ignore=()):
    return build_protocol(service, set(ignore)).signed_message_handlers[Model.build_schema_digest(ChatMessage)]


def service_with(*replies, allowed=(SENDER,), cards=True, **kw):
    llm, script = fakes.client(*replies) if replies else (None, None)
    svc = Orchestrator("root", None if allowed is None else set(allowed), cards=cards,
                       llm_factory=(lambda: llm), **kw)
    return svc, script


def chat(*parts):
    parts = [TextContent(type="text", text=p) if isinstance(p, str) else p for p in parts]
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=parts)


async def send(service, *parts, sender=SENDER, ctx=None, ignore=()):
    ctx = ctx or SimpleNamespace(send=AsyncMock(), logger=Mock(), send_and_receive=AsyncMock())
    msg = chat(*parts)
    await handler_for(service, ignore)(ctx, sender, msg)
    sent = [c.args[1] for c in ctx.send.await_args_list]
    return sent, msg


def text_of(message):
    return "\n".join(p.text for p in message.content if isinstance(p, TextContent))


def body_of(message):
    """The reply without the fixed footer every turn ends with."""
    text = text_of(message)
    assert text.endswith(FOOTER), text
    return text[: -len(FOOTER)].rstrip()


async def test_ack_first_then_text_and_end_session():
    svc, _ = service_with(fakes.answer("All good."))
    sent, msg = await send(svc, "is anything wrong with the books")
    assert isinstance(sent[0], ChatAcknowledgement) and sent[0].acknowledged_msg_id == msg.msg_id
    assert len(sent) == 2 and body_of(sent[1]) == "All good."
    assert isinstance(sent[1].content[-1], EndSessionContent)
    assert not any(isinstance(p, MetadataContent) for p in sent[1].content)


async def test_unlisted_sender_gets_only_an_ack_and_no_turn():
    svc, script = service_with(fakes.answer("x"))
    sent, _ = await send(svc, "hello", sender="agent1qstranger")
    assert len(sent) == 1 and isinstance(sent[0], ChatAcknowledgement) and script.requests == []
    assert svc.sessions == {}


async def test_mailbox_mode_takes_anyone():
    svc, _ = service_with(fakes.answer("hi"), allowed=None)
    sent, _ = await send(svc, "hello there", sender="agent1qanyone")
    assert body_of(sent[-1]) == "hi"


async def test_rate_limit_ten_per_ten_minutes_per_sender():
    t = [0.0]
    svc, _ = service_with(fakes.answer("ok"), allowed=None)
    svc.limiter = RateLimit(clock=lambda: t[0])
    texts = [text_of((await send(svc, "q"))[0][-1]) for _ in range(11)]
    assert texts[:10] == ["ok\n\n" + FOOTER] * 10 and texts[10].startswith(f"{NOTICE_PREFIX} Rate limited")
    sent, _ = await send(svc, "q")                                       # the notice goes out once per window
    assert len(sent) == 1 and isinstance(sent[0], ChatAcknowledgement)
    assert body_of((await send(svc, "q", sender="agent1qother"))[0][-1]) == "ok"
    t[0] = 601
    assert body_of((await send(svc, "q"))[0][-1]) == "ok"


async def test_message_size_cap():
    svc, script = service_with(fakes.answer("x"))
    sent, _ = await send(svc, "a" * 4001)
    assert "too long" in text_of(sent[-1]) and script.requests == []


async def test_busy_when_a_turn_is_running_for_that_sender():
    svc, script = service_with(fakes.answer("x"))
    svc.running.add(SENDER)
    sent, _ = await send(svc, "q")
    assert text_of(sent[-1]).startswith(f"{NOTICE_PREFIX} busy") and script.requests == []


async def test_concurrent_messages_from_one_sender_one_is_busy():
    gate = asyncio.Event()

    class SlowLLM:
        available = True

        async def chat(self, messages, tools=None, deadline=None):
            await gate.wait()
            return fakes.answer("done")

    svc = Orchestrator("root", {SENDER}, llm_factory=SlowLLM)
    first = asyncio.create_task(send(svc, "q1"))
    await asyncio.sleep(0.05)
    sent, _ = await send(svc, "q2")
    assert text_of(sent[-1]).startswith(f"{NOTICE_PREFIX} busy")
    gate.set()
    sent, _ = await first
    assert body_of(sent[-1]) == "done" and svc.running == set()


async def test_start_session_resets_state():
    svc, _ = service_with(fakes.answer("ok"))
    await send(svc, "q")
    old = svc.sessions[SENDER]
    await send(svc, StartSessionContent(type="start-session"), "q")
    assert svc.sessions[SENDER] is not old


async def test_start_session_alone_gets_a_greeting():
    svc, script = service_with(fakes.answer("x"))
    sent, _ = await send(svc, StartSessionContent(type="start-session"))
    assert "controller" in text_of(sent[-1]) and script.requests == []


async def test_card_only_when_a_suggestion_was_proposed_and_all_strings(tripwire):
    svc, _ = service_with(fakes.tool_calls(("c1", "propose_fix", {})), fakes.answer("Proposed."))
    sent, _ = await send(svc, "raise the pizza price")
    reply = sent[-1]
    cards = [p for p in reply.content if isinstance(p, MetadataContent)]
    assert len(cards) == 1 and not any(isinstance(p, EndSessionContent) for p in reply.content)
    meta = cards[0].metadata
    assert all(isinstance(k, str) and isinstance(v, str) for k, v in meta.items())
    assert meta["card_protocol_version"] == "1" and meta["card_kind"] == "review"
    assert meta["requires_card_interaction"] == "true"
    validate_card_payload_json("review", meta["card_payload"])
    payload = json.loads(meta["card_payload"])
    assert all(isinstance(r["label"], str) and isinstance(r["value"], str) for r in payload["summary_rows"])
    assert payload["approve_cta"]["label"] == "Save" and payload["reject_cta"]["label"] == "Discard"
    assert payload["approve_cta"]["selection"] == {"action": "save", "suggestion": fakes.SUGGESTION_PATH}
    for cta in (payload["approve_cta"], payload["reject_cta"]):
        assert all(isinstance(v, str) for v in cta["selection"].values())
    rows = {r["label"]: r["value"] for r in payload["summary_rows"]}
    assert rows["New price"] == "$20.00" and rows["Status"] == "proposed, not applied"
    assert fakes.APPROVE in rows.values()

    # the click comes back as a card response (MetadataContent): save confirms, never approves, no second card
    click = create_card_response_content(selection={"action": "save", "suggestion": fakes.SUGGESTION_PATH})
    sent, _ = await send(svc, click)
    assert "pending, not applied" in text_of(sent[-1]) and fakes.APPROVE in text_of(sent[-1])
    assert not any(isinstance(p, MetadataContent) for p in sent[-1].content)
    assert tripwire == []


async def test_no_card_when_cards_are_off():
    svc, _ = service_with(fakes.tool_calls(("c1", "propose_fix", {})), fakes.answer("Proposed."), cards=False)
    sent, _ = await send(svc, "raise the pizza price")
    assert not any(isinstance(p, MetadataContent) for p in sent[-1].content)
    assert isinstance(sent[-1].content[-1], EndSessionContent)


def test_review_card_with_minimal_proposal():
    card = review_card({"path": "data/s.json"})
    assert all(isinstance(v, str) for v in card.metadata.values())
    validate_card_payload_json("review", card.metadata["card_payload"])


async def test_late_peer_reply_is_not_answered():
    svc, script = service_with(fakes.answer("x"), allowed=None)
    sent, _ = await send(svc, json.dumps({"status": "ok"}), sender=SCOUT, ignore={SCOUT})
    assert len(sent) == 1 and isinstance(sent[0], ChatAcknowledgement) and script.requests == []


async def test_resolver_checks_local_rules_before_the_fallback():
    fallback = SimpleNamespace(resolve=AsyncMock(return_value=("agent1qremote", ["https://agentverse/x"])))
    r = LocalFirstResolver({"agent1qlocal": "http://127.0.0.1:8003/submit"}, fallback)
    assert await r.resolve("agent1qlocal") == ("agent1qlocal", ["http://127.0.0.1:8003/submit"])
    fallback.resolve.assert_not_awaited()
    assert await r.resolve("test-agent://agent1qlocal") == ("agent1qlocal", ["http://127.0.0.1:8003/submit"])
    assert await r.resolve("agent1qremote") == ("agent1qremote", ["https://agentverse/x"])
    fallback.resolve.assert_awaited_once_with("agent1qremote")
    assert await LocalFirstResolver({}).resolve("agent1qremote") == (None, [])


@pytest.mark.parametrize("text,error", [
    ("agent1qa", "expected"), ("bob=http://127.0.0.1:1/submit", "agent1"),
    ("agent1qa=ftp://127.0.0.1/x", "http"), ("agent1qa=http://evil.example/submit", "127.0.0.1"),
    ("agent1qa=http://u:p@127.0.0.1/x", "user name"), ("agent1qa=http://127.0.0.1:99999/x", "valid URL"),
    ("agent1qa=http://127.0.0.1:1/x,agent1qa=http://127.0.0.1:2/x", "twice"),
])
def test_peers_are_validated(text, error):
    with pytest.raises(ValueError, match=error):
        parse_peers(text)


def test_peers_parse():
    assert parse_peers(" agent1qa=http://127.0.0.1:8003/submit ,, agent1qb=http://localhost:8007/submit") == {
        "agent1qa": "http://127.0.0.1:8003/submit", "agent1qb": "http://localhost:8007/submit"}
    assert parse_peers("agent1qa=https://host.example/submit", allow_remote=True)


async def test_agent_mode_scout_timeout_falls_back_in_process():
    session = fakes.Session("r")
    ctx = SimpleNamespace(send_and_receive=AsyncMock(side_effect=asyncio.TimeoutError))
    d = PeerDispatch(ctx, scout=SCOUT)
    result = await d(session, "scan_market", {"items": ["Margherita Pizza"]})
    assert result["degraded"] is True and result["positions"] == {"Margherita Pizza": "below_market"}
    assert d.degraded and "market scout agent timed out" in d.notes[0]
    assert [e["name"] for e in session.log] == ["scan_market"]           # the in-process tool logged it


async def test_agent_mode_scout_no_reply_or_error_status_falls_back():
    status = SimpleNamespace(detail="Timeout waiting for response")
    for side in [(None, status), (fakes_reply({"status": "busy"}), status)]:
        d = PeerDispatch(SimpleNamespace(send_and_receive=AsyncMock(return_value=side)), scout=SCOUT)
        result = await d(fakes.Session("r"), "scan_market", {})
        assert result["degraded"] and d.degraded


def fakes_reply(body):
    return chat(json.dumps(body))


async def test_agent_mode_scout_success_logs_the_remote_result(monkeypatch):
    import sys
    session = fakes.Session("r")
    body = {"request_id": "x", "status": "ok", "positions": {"Tiramisu": "at_par"},
            "filename": "data/market_reports/M-x.json"}
    seen = []

    def adopt(session, filename, request_id, args):                     # the core maps the report file
        seen.append((filename, request_id))
        result = {"positions": [{"our_item": "Tiramisu", "position": "at_par"}], "via": "market_scout agent"}
        session.log.append({"name": "scan_market", "args": args, "result": result})
        return result

    monkeypatch.setattr(sys.modules["orchestrator.tools"], "adopt_market_report", adopt, raising=False)
    ctx = SimpleNamespace(send_and_receive=AsyncMock(return_value=(fakes_reply(body), None)))
    d = PeerDispatch(ctx, scout=SCOUT)
    result = await d(session, "scan_market", {"items": ["Tiramisu"]})
    assert result["positions"] == [{"our_item": "Tiramisu", "position": "at_par"}] and not d.degraded
    assert fakes.CALLS == [] and seen[0][0] == "data/market_reports/M-x.json"
    assert session.log[-1]["name"] == "scan_market" and session.log[-1]["result"]["via"] == "market_scout agent"
    dest, msg = ctx.send_and_receive.await_args.args
    kw = ctx.send_and_receive.await_args.kwargs
    assert dest == SCOUT and kw["response_type"] is ChatMessage and kw["timeout"] == 25
    request = json.loads(text_of(msg))
    assert re.fullmatch(r"[A-Za-z0-9._-]{1,64}", request["request_id"]) and request["items"] == ["Tiramisu"]
    assert request["offline"] is True and request["service"] == "dinner"


async def test_agent_mode_bookkeeper_timeout_still_answers():
    session = fakes.Session("r")
    ctx = SimpleNamespace(send_and_receive=AsyncMock(side_effect=asyncio.TimeoutError))
    d = PeerDispatch(ctx, bookkeeper="agent1qbk")
    result = await d(session, "get_findings", {})
    assert result["findings"][0]["finding_id"] == "F1" and d.degraded
    request = json.loads(text_of(ctx.send_and_receive.await_args.args[1]))
    assert set(request) == {"request_id", "nessie_tag", "as_of"} and request["nessie_tag"] == "boh-1loc"


async def test_agent_mode_through_the_handler_marks_the_reply_degraded():
    svc, _ = service_with(fakes.tool_calls(("c1", "scan_market", {})), fakes.answer("Margherita is below market."),
                          use_agents=True, scout=SCOUT)
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock(), send_and_receive=AsyncMock(side_effect=asyncio.TimeoutError))
    sent, _ = await send(svc, "how do our prices compare", ctx=ctx)
    assert "degraded" in text_of(sent[-1]) and "market scout agent timed out" in text_of(sent[-1])


async def test_rest_ask_runs_the_same_turn():
    svc, _ = service_with(fakes.tool_calls(("c1", "get_menu", {})), fakes.answer("Margherita is $18."))
    ask = build_ask_handler(svc)
    ctx = SimpleNamespace(logger=Mock(), send_and_receive=AsyncMock())
    out = await ask(ctx, AskRequest(text="Margherita price?"))
    assert out.text == "Margherita is $18.\n\n" + FOOTER and out.status == "ok" and "rest:local" in svc.sessions
    svc.running.add("rest:local")
    assert (await ask(ctx, AskRequest(text="again"))).status == "busy"
    assert (await ask(ctx, AskRequest(text="x" * 5000, session_id="b"))).status == "error"


def test_make_agent_requires_a_seed(tmp_path):
    with pytest.raises(ValueError, match="ORCHESTRATOR_SEED"):
        agent_mod.make_agent(tmp_path)


async def test_make_agent_local_binds_localhost_and_registers_ask(tmp_path, monkeypatch):
    import uagents.asgi

    monkeypatch.setattr(uagents.asgi, "HOST", "0.0.0.0")
    monkeypatch.setenv("ORCHESTRATOR_SEED", "test seed for the orchestrator " + "x" * 20)
    a = agent_mod.make_agent(tmp_path, port=8905)
    assert uagents.asgi.HOST == "127.0.0.1"
    assert ("POST", "/ask") in a._rest_handlers and ("POST", "/connect") not in a._rest_handlers
    assert a.orchestrator.allowed == {agent_mod.local_client_address(tmp_path)} and a.orchestrator.cards is False
    assert isinstance(a._resolver, LocalFirstResolver) and a._resolver.fallback is None
    assert (tmp_path / "data" / "orchestrator" / ".gitignore").exists()


async def test_make_agent_mailbox_has_connect_and_cards(tmp_path, monkeypatch):
    import uagents.asgi

    monkeypatch.setattr(uagents.asgi, "HOST", "0.0.0.0")
    monkeypatch.setenv("ORCHESTRATOR_SEED", "test seed for the orchestrator " + "y" * 20)
    a = agent_mod.make_agent(tmp_path, port=8906, mailbox=True)
    assert ("POST", "/connect") in a._rest_handlers and ("POST", "/ask") in a._rest_handlers
    assert a.orchestrator.allowed is None and a.orchestrator.cards is True
    assert a._resolver.fallback is not None and a._use_mailbox


async def test_ask_is_not_registered_off_loopback(tmp_path, monkeypatch):
    import uagents.asgi

    monkeypatch.setattr(uagents.asgi, "HOST", "0.0.0.0")
    monkeypatch.setenv("ORCHESTRATOR_SEED", "test seed for the orchestrator " + "z" * 20)
    monkeypatch.setenv("ORCHESTRATOR_BIND_HOST", "0.0.0.0")
    a = agent_mod.make_agent(tmp_path, port=8907)
    assert ("POST", "/ask") not in a._rest_handlers


def test_register_posts_the_token_and_never_prints_it(monkeypatch, capsys):
    seen = []

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        return httpx.Response(200, json={"success": True, "detail": "registered tok-SECRET"})

    def handler(request):
        seen.append((str(request.url), json.loads(request.content)))
        assert request.headers["authorization"] == "Bearer rest-BEARER"
        return httpx.Response(200, json={"success": True, "detail": "registered tok-SECRET rest-BEARER"})

    monkeypatch.setenv("AGENTVERSE_TOKEN", "tok-SECRET")
    monkeypatch.setattr(register, "load_env", lambda: None)
    monkeypatch.setattr(register, "rest_token", lambda root: "rest-BEARER")
    real = httpx.Client.__init__
    monkeypatch.setattr(httpx.Client, "__init__",
                        lambda self, **kw: real(self, **{**kw, "transport": httpx.MockTransport(handler)}))
    assert register.main(["--port", "8005"]) == 0
    assert seen == [("http://127.0.0.1:8005/connect", {"user_token": "tok-SECRET", "agent_type": "mailbox"})]
    out = capsys.readouterr().out
    assert "tok-SECRET" not in out and "rest-BEARER" not in out


def test_register_without_token(monkeypatch):
    monkeypatch.setattr(register, "load_env", lambda: None)
    assert register.connect()["success"] is False


def test_source_never_approves_applies_or_unlocks():
    """The never-approve invariant, statically: no orchestrator module touches the approval or apply paths."""
    forbidden = re.compile(r"record_approval|\.apply\(|owner\.unlock|unlock\(|Executor|cost_price\.owner|"
                           r"--live|INSERT\s+INTO|cost_price\.db")
    for name in ("llm.py", "guard.py", "turn.py", "agent.py", "register.py"):
        assert not forbidden.search((SRC / name).read_text(encoding="utf-8")), name


async def test_full_chat_with_save_click_never_approves(tripwire):
    svc, _ = service_with(fakes.tool_calls(("c1", "propose_fix", {})), fakes.answer("Proposed. Approve at the terminal."))
    await send(svc, "raise the pizza price")
    sent, _ = await send(svc, json.dumps({"action": "save", "suggestion": fakes.SUGGESTION_PATH}))
    assert "pending" in text_of(sent[-1])
    await send(svc, json.dumps({"action": "discard", "suggestion": fakes.SUGGESTION_PATH}))
    assert tripwire == []
