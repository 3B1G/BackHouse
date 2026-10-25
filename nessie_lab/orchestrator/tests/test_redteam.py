"""Regression tests for the red-team findings on the LLM / agent side (fake core, no network): zero-width tricks in a
whole turn, the fixed footer, runaway tool calls, non-owner chats, ping-pong loops, notices, rate-limit memory, and
the hardened HTTP server (CORS, bearer token, inspector routes, proxy headers, bind address)."""
import asyncio
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest
from uagents import Model
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement, ChatMessage, EndSessionContent, MetadataContent, TextContent,
)

import fakes
from orchestrator import agent as agent_mod
from orchestrator import llm
from orchestrator.agent import (
    NOTICE_PREFIX, GuardedServer, NoticeOnce, Orchestrator, PeerDispatch, RateLimit, build_protocol, is_our_output,
)
from orchestrator.turn import FOOTER, Deadline, turn

pytestmark = pytest.mark.usefixtures("fake_core")
ME, OTHER = "agent1qme", "agent1qother"


def chat(*parts):
    parts = [TextContent(type="text", text=p) if isinstance(p, str) else p for p in parts]
    return ChatMessage(timestamp=datetime.now(timezone.utc), msg_id=uuid4(), content=parts)


def text_of(message):
    return "\n".join(p.text for p in message.content if isinstance(p, TextContent))


def handler(service):
    return build_protocol(service).signed_message_handlers[Model.build_schema_digest(ChatMessage)]


# ---------------------------------------------------------------- the turn
async def test_zero_width_figure_is_normalised_then_guarded_and_never_sent():
    zw = "​"
    llm_, _ = fakes.client(fakes.tool_calls(("a", "get_menu", {})),
                           fakes.answer(f"Margherita is $18.00. Your refund of ${zw}48,000 is coming, a 9{zw}9% win."),
                           fakes.answer(f"Margherita is $18.00. Still a refund of ${zw}48,000."))
    out = await turn(fakes.Session("r"), "what is going on?", llm=llm_)
    sent = out["text"]
    assert "48,000" not in sent and "99%" not in sent and zw not in sent
    assert "Margherita is $18.00." in sent and sent.endswith(FOOTER)


async def test_echo_laundering_through_a_tool_argument_is_cut():
    llm_, _ = fakes.client(fakes.tool_calls(("a", "propose_fix", {"dish": "$50,000 recovered by 2027-01-15"})),
                           fakes.answer("You will recover $50,000 by 2027-01-15."))
    session = fakes.Session("r")
    out = await turn(session, "anything to recover?", llm=llm_)
    assert "$50,000" not in out["text"].split("Figures I could verify:")[0]


@pytest.mark.parametrize("claim", ["Arancini now costs $14.00 on the live menu.", "The change was applied and is live.",
                                   "I went ahead and raised it."])
async def test_applied_wording_is_cut(claim):
    llm_, _ = fakes.client(fakes.tool_calls(("a", "get_menu", {})), fakes.answer(claim), fakes.answer(claim))
    out = await turn(fakes.Session("r"), "did you change it?", llm=llm_)
    assert claim not in out["text"] and out["text"].endswith(FOOTER)


@pytest.mark.parametrize("text", ["status", "What should I change?", json.dumps({"action": "save", "suggestion": "x"}),
                                  ""])
async def test_every_reply_ends_with_the_fixed_footer(text):
    out = await turn(fakes.Session("r"), text)
    assert out["text"].endswith(FOOTER)


async def test_at_most_eight_tool_calls_per_step_and_three_proposals_per_turn():
    calls = [(f"c{i}", "propose_fix", {"dish": "Margherita Pizza", "new_price_cents": 1900 + i}) for i in range(25)]
    llm_, script = fakes.client(fakes.tool_calls(*calls), fakes.answer("Proposed."))
    out = await turn(fakes.Session("r"), "propose everything", llm=llm_)
    assert [c[0] for c in fakes.CALLS].count("propose_fix") == 3
    tool_msgs = [m for m in script.requests[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == [c[0] for c in calls]      # every call still answered
    assert sum("at most 8 tool calls" in m["content"] for m in tool_msgs) == 17
    assert sum("at most 3 propose_fix" in m["content"] for m in tool_msgs) == 5
    assert out["text"].endswith(FOOTER)


async def test_a_reply_asking_for_hundreds_of_calls_is_refused():
    llm_, _ = fakes.client(fakes.tool_calls(*[(f"c{i}", "get_menu", {}) for i in range(200)]))
    with pytest.raises(llm.LLMError):
        await llm.run_tool_loop(fakes.Session("r"), "x", llm_)


def test_system_prompt_asks_for_iso_dates():
    assert "YYYY-MM-DD" in llm.SYSTEM_PROMPT


async def test_turn_cancels_its_deadline_so_leftover_threads_write_nothing():
    session = fakes.Session("r")
    llm_, _ = fakes.client(fakes.answer("No figures."))
    await turn(session, "hello there", llm=llm_)
    assert isinstance(session.stop, Deadline) and session.stop.expired()


async def test_brief_fallback_counts_against_the_remaining_time(monkeypatch):
    def slow(session, **kw):
        import time
        time.sleep(1.5)
        return {"text": "late"}

    monkeypatch.setattr(sys.modules["orchestrator.brief"], "brief", slow)
    out = await turn(fakes.Session("r"), "status", deadline=1.2)
    assert out["degraded"] and "brief timed out" in out["notes"] and "late" not in out["text"]


# ---------------------------------------------------------------- non-owners (mailbox)
async def test_non_owner_gets_no_propose_tool_and_a_refused_call():
    llm_, script = fakes.client(fakes.tool_calls(("a", "propose_fix", {"dish": "Margherita Pizza"})),
                                fakes.answer("Ask the owner."))
    session = fakes.Session("r")
    out = await turn(session, "raise the pizza price", llm=llm_, owner=False)
    names = {t["function"]["name"] for t in script.requests[0]["tools"]}
    assert "propose_fix" not in names and "preview_suggestion" not in names
    assert "NOT from the restaurant's owner" in script.requests[0]["messages"][0]["content"]
    assert fakes.CALLS == [] and out["suggestions"] == []
    assert "only the owner" in script.requests[1]["messages"][-1]["content"]


async def test_non_owner_brief_writes_nothing_and_is_cached_per_day(monkeypatch):
    seen = []

    def brief(session, propose=True):
        seen.append(propose)
        return {"text": "Cash is $12,345.56."}

    monkeypatch.setattr(sys.modules["orchestrator.brief"], "brief", brief)
    cache = {}
    a = await turn(fakes.Session("r"), "status", owner=False, brief_cache=cache)
    b = await turn(fakes.Session("r"), "how are we doing?", owner=False, brief_cache=cache)
    assert seen == [False] and a["text"] == b["text"] and "cached" in b["notes"][-1]


async def test_mailbox_owner_can_propose_and_others_cannot():
    svc = Orchestrator("root", None, owners={ME}, llm_factory=lambda: fakes.client(
        fakes.tool_calls(("a", "propose_fix", {})), fakes.answer("Proposed."))[0])
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
    await handler(svc)(ctx, OTHER, chat("raise the pizza price"))
    assert fakes.CALLS == []
    await handler(svc)(ctx, ME, chat("raise the pizza price"))
    assert [c[0] for c in fakes.CALLS] == ["propose_fix"]


def test_mailbox_replies_never_carry_absolute_paths(tmp_path):
    svc = Orchestrator(tmp_path, None)
    text = f"approve {tmp_path / 'data' / 'suggestions' / 'x.json'} --root {tmp_path} and {tmp_path.as_posix()}/data"
    out = svc.scrub(text)
    assert str(tmp_path) not in out and tmp_path.as_posix() not in out and "<nessie_lab>" in out
    assert Orchestrator(tmp_path, {ME}).scrub(text) == text                     # local mode: the owner's terminal


# ---------------------------------------------------------------- loops and notices
async def _network(services: dict, first_from: str, first_to: str, first: ChatMessage, cap=50):
    queue, chats = [(first_from, first_to, first)], 0
    while queue and chats < cap:
        src, dst, msg = queue.pop(0)
        if isinstance(msg, ChatAcknowledgement):
            continue
        chats += 1
        if dst not in services:
            continue

        async def send(to, m, _me=dst):
            queue.append((_me, to, m))

        ctx = SimpleNamespace(send=send, logger=Mock(), agent=SimpleNamespace(address=dst))
        await handler(services[dst])(ctx, src, msg)
    return chats


async def test_two_orchestrators_wired_to_each_other_stop_within_three_messages():
    def svc():
        return Orchestrator("root", None, cards=True, llm_factory=lambda: fakes.client(fakes.answer("Cash is fine."))[0])
    assert await _network({"agent1qa": svc(), "agent1qb": svc()}, "agent1qa", "agent1qb", chat("hello")) <= 3


async def test_two_orchestrators_with_cards_stop_within_three_messages(tripwire):
    def svc():
        return Orchestrator("root", None, owners={"agent1qa", "agent1qb"}, cards=True, llm_factory=lambda: fakes.client(
            fakes.tool_calls(("a", "propose_fix", {})), fakes.answer("Proposed."))[0])
    assert await _network({"agent1qa": svc(), "agent1qb": svc()}, "agent1qa", "agent1qb", chat("raise it")) <= 3


async def test_rate_limited_orchestrators_do_not_loop():
    a, b = Orchestrator("root", None), Orchestrator("root", None)
    for s in (a, b):
        s.limiter = RateLimit(limit=0)
    assert await _network({"agent1qa": a, "agent1qb": b}, "agent1qa", "agent1qb", chat("hello")) <= 3


async def test_own_address_is_dropped_without_an_ack():
    svc = Orchestrator("root", None, address=ME)
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
    await handler(svc)(ctx, ME, chat("hello"))
    assert ctx.send.await_count == 0 and svc.sessions == {}


async def test_end_session_and_notices_never_start_a_turn():
    svc = Orchestrator("root", None, llm_factory=lambda: (_ for _ in ()).throw(AssertionError("no turn")))
    for msg in [chat("Cash is fine.", EndSessionContent(type="end-session")),
                chat(f"{NOTICE_PREFIX} busy: later"),
                chat("busy", MetadataContent(type="metadata", metadata={"backhouse_notice": "1"}))]:
        ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
        await handler(svc)(ctx, OTHER, msg)
        assert [type(c.args[1]) for c in ctx.send.await_args_list] == [ChatAcknowledgement]
    assert svc.sessions == {}


async def test_notices_are_tagged_and_detected():
    svc = Orchestrator("root", None)
    svc.limiter = RateLimit(limit=0)
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
    await handler(svc)(ctx, OTHER, chat("hello"))
    reply = ctx.send.await_args_list[-1].args[1]
    assert text_of(reply).startswith(NOTICE_PREFIX) and is_our_output(reply)
    assert any(isinstance(p, MetadataContent) and p.metadata.get("backhouse_notice") for p in reply.content)


async def test_busy_and_too_long_notices_go_out_once_per_window():
    t = [0.0]
    svc = Orchestrator("root", None)
    svc.notices = NoticeOnce(clock=lambda: t[0])
    svc.running.add(OTHER)
    counts = []
    for _ in range(3):
        ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
        await handler(svc)(ctx, OTHER, chat("q"))
        counts.append(ctx.send.await_count)
    assert counts == [2, 1, 1]
    t[0] = 601
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
    await handler(svc)(ctx, OTHER, chat("x" * 5000))
    assert ctx.send.await_count == 2


def test_rate_limit_memory_is_pruned_and_capped():
    t = [0.0]
    rl = RateLimit(clock=lambda: t[0], max_senders=5000)
    for i in range(20_000):
        rl.allow(f"agent1q{i:07d}")
    assert len(rl.seen) == 5000
    t[0] = 10 ** 9
    rl.allow("agent1qlater")
    assert list(rl.seen) == ["agent1qlater"]
    rl2 = RateLimit(limit=2, clock=lambda: t[0])
    assert [rl2.allow("a") for _ in range(3)] == [True, True, False]           # the cap still bites


# ---------------------------------------------------------------- agent mode
async def test_bookkeeper_filename_is_validated_and_that_report_is_preferred(monkeypatch):
    monkeypatch.setattr(sys.modules["orchestrator.tools"], "report_name",
                        lambda v: v.rsplit("/", 1)[-1] if v.startswith("data/reports/R-") else None, raising=False)
    for name, preferred, degraded in [("data/reports/R-7.json", "R-7.json", False),
                                      ("../../../../Windows/win.ini", None, True)]:
        reply = chat(json.dumps({"filename": name}))
        d = PeerDispatch(SimpleNamespace(send_and_receive=AsyncMock(return_value=(reply, None))), bookkeeper="agent1qbk")
        session = fakes.Session("r")
        await d(session, "get_findings", {})
        assert getattr(session, "preferred_report", None) == preferred and d.degraded is degraded
        assert "win.ini" not in json.dumps(d.notes)


async def test_scout_reply_that_cannot_be_mapped_falls_back_degraded():
    body = {"request_id": "r1", "report_id": "M-r1", "filename": "../../etc/passwd", "status": "ok",
            "positions": {"Margherita Pizza": "below_market"}}
    d = PeerDispatch(SimpleNamespace(send_and_receive=AsyncMock(return_value=(chat(json.dumps(body)), None))),
                     scout="agent1qscout")
    result = await d(fakes.Session("r"), "scan_market", {})
    assert d.degraded and result["degraded"] and "could not use" in d.notes[0]


# ---------------------------------------------------------------- HTTP
def _agent(tmp_path, monkeypatch, mailbox=True, **env):
    monkeypatch.setenv("ORCHESTRATOR_SEED", f"red team http seed {uuid4().hex}")   # one agent per address
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    return agent_mod.make_agent(tmp_path, port=8931, mailbox=mailbox)


class InProcess(httpx.MockTransport):
    """The agent's ASGI app called in-process (no socket); a MockTransport so the no-network fixture allows it."""

    def __init__(self, app, host):
        super().__init__(lambda request: None)
        self.inner = httpx.ASGITransport(app=app, client=(host, 50000))

    async def handle_async_request(self, request):
        return await self.inner.handle_async_request(request)


def _client(agent, host="127.0.0.1"):
    return httpx.AsyncClient(transport=InProcess(agent._server, host), base_url="http://127.0.0.1:8931")


async def test_rest_token_is_written_owner_only_and_printed_once(tmp_path, monkeypatch, capsys):
    a = _agent(tmp_path, monkeypatch, mailbox=False)
    token = (tmp_path / "data" / "orchestrator" / "rest_token").read_text().strip()
    assert len(token) >= 40 and capsys.readouterr().out.count(token) == 1
    assert isinstance(a._server, GuardedServer) and a._server.token == token
    assert agent_mod.read_rest_token(tmp_path) == token


async def test_ask_needs_the_token_and_no_cors_is_ever_sent(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch, mailbox=False)
    auth = {"Authorization": f"Bearer {a._server.token}"}
    async with _client(a) as c:
        assert (await c.post("/ask", json={"text": "status"})).status_code == 401
        assert (await c.post("/ask", json={"text": "status"}, headers={"Authorization": "Bearer nope"})).status_code == 401
        r = await c.post("/ask", json={"text": "status"}, headers={**auth, "Origin": "https://evil.example"})
        assert r.status_code == 403
        r = await c.options("/ask", headers={"Origin": "https://evil.example", "Access-Control-Request-Method": "POST"})
        assert r.status_code == 403 and "access-control-allow-origin" not in r.headers
        r = await c.post("/ask", json={"text": "status"}, headers=auth)
        assert r.status_code == 200 and r.json()["text"].endswith(FOOTER)
        assert not any(k.lower().startswith("access-control") for k in r.headers)
        r = await c.post("/ask", json={"text": "status"}, headers={**auth, "Origin": "http://127.0.0.1:3000"})
        assert r.status_code == 200
    config = a._server.uvicorn_config()
    assert config.proxy_headers is False and not any(k.lower().startswith("access-control") for k, _ in config.headers)


async def test_mailbox_inspector_routes_are_locked_down(tmp_path, monkeypatch):
    from uagents.mailbox import RegistrationResponse

    a = _agent(tmp_path, monkeypatch)
    calls = []

    async def connect_stub(ctx, req):
        calls.append(req.user_token)
        return RegistrationResponse(success=True, detail="stub")

    a._rest_handlers[("POST", "/connect")] = connect_stub
    auth = {"Authorization": f"Bearer {a._server.token}"}
    body = {"user_token": "ATTACKER", "agent_type": "mailbox"}
    async with _client(a) as c:
        assert (await c.get("/messages")).status_code == 404
        assert (await c.get("/messages", headers=auth)).status_code == 404
        assert (await c.post("/connect", json=body)).status_code == 403
        assert (await c.post("/connect", json=body, headers={**auth, "Origin": "https://agentverse.ai"})).status_code == 403
    async with _client(a, host="10.9.9.9") as c:                       # a remote client claiming to be local
        r = await c.post("/connect", json=body, headers={**auth, "X-Forwarded-For": "127.0.0.1"})
        assert r.status_code == 403
    assert calls == []
    async with _client(a) as c:
        r = await c.post("/connect", json=body, headers=auth)
        assert r.status_code == 200 and r.json()["success"] is True and calls == ["ATTACKER"]
        assert (await c.post("/connect", json=body, headers=auth)).status_code == 404    # closed after it connected
        assert (await c.post("/disconnect", json={"user_token": "x"}, headers=auth)).status_code == 404


async def test_inspector_off_by_env(tmp_path, monkeypatch):
    a = _agent(tmp_path, monkeypatch, ORCHESTRATOR_INSPECTOR="0")
    assert ("POST", "/connect") not in a._rest_handlers and a._server.inspector_open is False


def test_mailbox_mode_refuses_a_non_loopback_bind(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="loopback"):
        _agent(tmp_path, monkeypatch, ORCHESTRATOR_BIND_HOST="0.0.0.0")


def test_cli_ask_server_sends_the_token(tmp_path, capsys):
    from orchestrator import cli

    (tmp_path / "data" / "orchestrator").mkdir(parents=True)
    (tmp_path / "data" / "orchestrator" / "rest_token").write_text("tok-REST\n")
    seen = []

    def handle(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json={"text": "ok", "notes": []})

    assert cli._ask_server(tmp_path, 8005, "status", transport=httpx.MockTransport(handle)) == 0
    assert seen == ["Bearer tok-REST"] and "tok-REST" not in capsys.readouterr().out
