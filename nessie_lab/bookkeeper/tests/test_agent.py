import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from uagents import Model
from uagents.registration import AlmanacApiRegistrationPolicy
from uagents_core.identity import Identity
from uagents_core.contrib.protocols.chat import ChatMessage, ChatAcknowledgement, EndSessionContent

from bookkeeper.agent import (
    ALMANAC_API_TIMEOUT, build_protocol, build_verifier_protocol, chat_message, connect_mailbox, mailbox_exists,
    report_summary,
)
from bookkeeper.contracts import ReportRequest
from bookkeeper.runner import Bookkeeper
from bookkeeper.verifier import Verifier
from conftest import ScriptedClient

VERIFIER = "agent1-verifier"
BOOKKEEPER = "agent1-bookkeeper"
ORCHESTRATOR = "local-orchestrator"


def handler_of(protocol):
    return protocol.signed_message_handlers[Model.build_schema_digest(ChatMessage)]


def context():
    return SimpleNamespace(send=AsyncMock(), logger=Mock())


def body_of(call):
    return json.loads(call.args[1].content[0].text)


async def test_draft_goes_to_verifier_and_its_report_back_to_orchestrator(root, draft):
    bk_handler = handler_of(build_protocol(Bookkeeper(root, ScriptedClient(draft)), VERIFIER, orchestrator=ORCHESTRATOR))
    verifier_handler = handler_of(build_verifier_protocol(Verifier(root, ScriptedClient(draft)), BOOKKEEPER))
    ctx = context()
    request = chat_message(json.dumps({"request_id": "orchestrator-1"}))
    await bk_handler(ctx, "local-orchestrator", request)
    sent = ctx.send.call_args_list
    assert len(sent) == 2 and isinstance(sent[0].args[1], ChatAcknowledgement)
    assert sent[0].args[0] == "local-orchestrator" and sent[0].args[1].acknowledged_msg_id == request.msg_id
    assert sent[1].args[0] == VERIFIER                                 # the orchestrator waits; the draft is verified first
    verify = body_of(sent[1])
    assert verify["draft"] == f"data/drafts/{verify['report_id']}.json" and verify["request_id"] == "orchestrator-1"
    assert not list((root / "data/reports").glob("R-*.json"))

    verifier_ctx = context()
    await verifier_handler(verifier_ctx, BOOKKEEPER, sent[1].args[1])
    reply = verifier_ctx.send.call_args_list[-1]
    assert reply.args[0] == BOOKKEEPER and isinstance(reply.args[1].content[1], EndSessionContent)
    assert body_of(reply)["verified"] is True and (root / body_of(reply)["filename"]).exists()

    ctx = context()
    await bk_handler(ctx, VERIFIER, reply.args[1])
    final = ctx.send.call_args_list[-1]
    assert final.args[0] == "local-orchestrator" and isinstance(final.args[1].content[1], EndSessionContent)
    body = body_of(final)
    assert body["verified"] is True and body["filename"] == f"data/reports/{verify['report_id']}.json"
    assert list((root / "data/reports/requests").glob("*.json"))      # the receipt: a redelivery is answered at once

    ctx = context()
    await bk_handler(ctx, "local-orchestrator", request)
    assert ctx.send.call_args_list[-1].args[0] == "local-orchestrator" and body_of(ctx.send.call_args_list[-1]) == body


async def test_revision_round_goes_back_to_the_verifier_before_the_orchestrator_hears(root, draft):
    nine = json.loads(json.dumps(draft))
    nine["findings"][0]["amounts"].update(actual_cents=9000, impact_cents=9000)
    bk_handler = handler_of(build_protocol(Bookkeeper(root, ScriptedClient(draft)), VERIFIER, orchestrator=ORCHESTRATOR))
    verifier_handler = handler_of(build_verifier_protocol(Verifier(root, ScriptedClient(nine), max_rounds=2), BOOKKEEPER))
    ctx = context()
    await bk_handler(ctx, "local-orchestrator", chat_message(json.dumps({"request_id": "rounds-1"})))
    message = ctx.send.call_args_list[-1].args[1]
    for round in (1, 2):
        assert body_of(ctx.send.call_args_list[-1])["round"] == round and ctx.send.call_args_list[-1].args[0] == VERIFIER
        verifier_ctx = context()
        await verifier_handler(verifier_ctx, BOOKKEEPER, message)
        reply = verifier_ctx.send.call_args_list[-1]
        ctx = context()
        await bk_handler(ctx, VERIFIER, reply.args[1])
        message = ctx.send.call_args_list[-1].args[1]
        if round == 1:
            assert body_of(reply)["type"] == "revise" and len(reply.args[1].content) == 1        # the session stays open
    final = ctx.send.call_args_list[-1]
    assert final.args[0] == "local-orchestrator" and isinstance(final.args[1].content[1], EndSessionContent)
    assert body_of(final)["verified"] is True


async def test_verifier_error_is_forwarded_to_orchestrator(root, draft):
    bk_handler = handler_of(build_protocol(Bookkeeper(root, ScriptedClient(draft)), VERIFIER, orchestrator=ORCHESTRATOR))
    ctx = context()
    await bk_handler(ctx, "local-orchestrator", chat_message('{"request_id": "will-fail"}'))
    report_id = body_of(ctx.send.call_args_list[-1])["report_id"]
    error = {"request_id": "will-fail", "report_id": report_id, "status": "error", "error": "Verification failed: x",
             "error_type": "RuntimeError", "trace_id": "TR-v"}
    ctx = context()
    await bk_handler(ctx, VERIFIER, chat_message(json.dumps(error), end=True))
    final = ctx.send.call_args_list[-1]
    assert final.args[0] == "local-orchestrator"
    assert body_of(final) == {k: error[k] for k in ("request_id", "status", "error", "error_type", "trace_id")}
    ctx = context()
    await bk_handler(ctx, VERIFIER, chat_message(json.dumps(error), end=True))      # nothing pending any more
    assert len(ctx.send.call_args_list) == 1 and ctx.logger.warning.called


async def test_verifier_answers_only_its_bookkeeper(root, draft):
    verifier_handler = handler_of(build_verifier_protocol(Verifier(root, ScriptedClient(draft)), BOOKKEEPER))
    ctx = context()
    await verifier_handler(ctx, "someone-else", chat_message(json.dumps(
        {"request_id": "x", "report_id": "R-" + "0" * 64, "draft": "data/drafts/R-" + "0" * 64 + ".json"})))
    note = ctx.send.call_args_list[-1].args[1].content[0].text
    assert note.startswith("This is the Hermes Bookkeeper's Verifier") and "filename" not in note
    assert not (root / "data/verifier_traces").exists()
    ctx = context()                                                    # told once an hour: then acknowledged only
    await verifier_handler(ctx, "someone-else", chat_message("hello"))
    assert [c for c in ctx.send.call_args_list if isinstance(c.args[1], ChatMessage)] == []


async def test_bad_request_returns_no_filename(root):
    handler = handler_of(build_protocol(Bookkeeper(root), VERIFIER, orchestrator=ORCHESTRATOR))
    ctx = context()
    await handler(ctx, "local-orchestrator", chat_message("not JSON"))
    body = body_of(ctx.send.call_args_list[-1])
    assert body["status"] == "error" and "filename" not in body


async def test_model_error_returns_actionable_message_and_trace(root):
    class FailingService:
        async def run(self, request, sender):
            exc = RuntimeError('ASI API returned HTTP 401')
            exc.bookkeeper_trace_id = 'TR-test'
            raise exc
    handler = handler_of(build_protocol(FailingService(), VERIFIER, orchestrator=ORCHESTRATOR))
    ctx = context()
    await handler(ctx, 'local-orchestrator', chat_message('{"request_id":"api-failed"}'))
    body = body_of(ctx.send.call_args_list[-1])
    assert body['error'] == 'ASI API returned HTTP 401'
    assert body['trace_id'] == 'TR-test'
    assert 'filename' not in body


async def test_mailbox_agent_advertises_its_mailbox_and_keeps_serving_on_loopback(monkeypatch):
    import uagents.asgi
    from bookkeeper.agent import local_agent
    monkeypatch.setattr(uagents.asgi, "HOST", uagents.asgi.HOST)
    monkeypatch.setenv("TEST_SEED", "bookkeeper-mailbox-test-seed")
    monkeypatch.setenv("TEST_MAILBOX", "true")
    monkeypatch.delenv("TEST_INSPECTOR", raising=False)
    agent, mailbox = local_agent("test", "TEST", 8001, {})
    assert mailbox and agent._use_mailbox                              # an explicit endpoint would override the mailbox
    assert [e.url for e in agent._endpoints] == [agent.agentverse.mailbox_endpoint]
    assert {path for _, path in agent._rest_handlers} == {"/health"}   # keeps the agent running; no inspector routes
    assert uagents.asgi.HOST == "127.0.0.1"
    policy = agent._registration_policy                               # the Almanac API only: no FET, no contract
    assert isinstance(policy, AlmanacApiRegistrationPolicy) and policy._timeout == ALMANAC_API_TIMEOUT


def test_mailbox_exists_asks_agentverse_as_the_agent():
    seen = []

    def reply(status):
        def handler(request):
            seen.append(request)
            return httpx.Response(status, json=[] if status == 200 else {"detail": "not found"})
        return httpx.MockTransport(handler)

    assert mailbox_exists("bookkeeper-mailbox-test-seed", reply(200)) is True
    assert mailbox_exists("bookkeeper-mailbox-test-seed", reply(404)) is False
    address = Identity.from_seed("bookkeeper-mailbox-test-seed", 0).address
    assert seen[0].url.path.endswith(f"/{address}/mailbox") and seen[0].headers["Authorization"].startswith("Agent ")


def test_connect_mailbox_posts_the_token_locally_and_never_repeats_it():
    sent = []

    def accept(request):
        sent.append(request)
        return httpx.Response(200, json={"success": True})

    connect_mailbox(8001, "secret-token", httpx.MockTransport(accept))
    assert str(sent[0].url) == "http://127.0.0.1:8001/connect"
    assert json.loads(sent[0].content) == {"user_token": "secret-token", "agent_type": "mailbox"}

    refuse = httpx.MockTransport(lambda request: httpx.Response(401, json={"detail": "bad token secret-token"}))
    with pytest.raises(ValueError) as refused:
        connect_mailbox(8001, "secret-token", refuse)
    assert "secret-token" not in str(refused.value) and "[token]" in str(refused.value)


async def test_unparsable_request_is_logged_with_its_sender_and_text(root):
    handler = handler_of(build_protocol(Bookkeeper(root), VERIFIER))
    ctx = context()
    await handler(ctx, "agent1-asi-one-user", chat_message("please run a report"))
    logged = ctx.logger.error.call_args.args[0]
    assert "agent1-asi-one-user" in logged and "'please run a report'" in logged


async def test_mailbox_bookkeeper_runs_only_for_allowed_senders_and_tells_others_once(root):
    service = SimpleNamespace(run=AsyncMock(), started=Mock(return_value=False), root=root)
    handler = handler_of(build_protocol(service, VERIFIER, allowed={ORCHESTRATOR}, orchestrator=ORCHESTRATOR))
    for _ in range(2):
        ctx = context()
        await handler(ctx, "agent1-stranger", chat_message('{"request_id": "r1"}'))
        replies = [c for c in ctx.send.call_args_list if isinstance(c.args[1], ChatMessage)]
        if _ == 0:                                                     # one note pointing to the orchestrator...
            assert len(replies) == 1 and ORCHESTRATOR in replies[0].args[1].content[0].text
        else:                                                          # ...then acknowledgements only
            assert replies == []
    service.run.assert_not_called()


async def test_a_person_is_told_a_run_started_and_the_orchestrator_is_not(root):
    service = SimpleNamespace(run=AsyncMock(side_effect=RuntimeError("ASI API returned HTTP 401")),
                              started=Mock(return_value=False), root=root)
    handler = handler_of(build_protocol(service, VERIFIER, allowed={ORCHESTRATOR, "agent1-owner"},
                                        orchestrator=ORCHESTRATOR))
    ctx = context()
    await handler(ctx, "agent1-owner", chat_message('{"request_id": "r1"}'))
    note = ctx.send.call_args_list[1].args[1]
    assert note.content[0].text.startswith("Working on r1") and len(note.content) == 1   # prose, and no end-session
    ctx = context()
    await handler(ctx, ORCHESTRATOR, chat_message('{"request_id": "r2"}'))
    assert [c.args[1] for c in ctx.send.call_args_list if isinstance(c.args[1], ChatMessage)][0].content[0].text \
        .startswith('{"request_id": "r2"')                            # straight to the (error) reply


async def test_a_request_with_unknown_fields_is_told_which_and_what_to_send(root):
    handler = handler_of(build_protocol(Bookkeeper(root), VERIFIER, orchestrator=ORCHESTRATOR))
    ctx = context()
    await handler(ctx, "agent1-owner", chat_message('{"request_id": "r1", "restaurant_id": "x", "period_end": "2026-09-30"}'))
    body = body_of(ctx.send.call_args_list[-1])
    assert body["status"] == "error" and body["error"].startswith("Unknown field(s): period_end, restaurant_id.")
    assert '"request_id"' in body["error"] and "as_of" in body["error"]


def test_report_summary_reads_the_published_report(tmp_path):
    finding = lambda cause, who, cents: {"root_cause": cause, "counterparty": who, "amounts": {"impact_cents": cents}}
    (tmp_path / "R-x.json").write_text(json.dumps({
        "as_of": "2026-10-23", "summary": {"errors": 2, "needs_owner": 1, "total_impact_cents": 150000,
                                           "recoverable_cents": 50000, "ongoing_monthly_cents": 2500},
        "findings": [finding("duplicate_charge", "Bay Produce", 42128), finding("missed_payment", "Sonoma Dairy", 127662),
                     finding("card_fee_creep", "TableTap", -1000)]}))
    assert report_summary(tmp_path, "R-x.json") == (
        "3 findings through 2026-10-23 (2 errors, 1 for the owner to decide): $1,500.00 total impact, $500.00 "
        "recoverable, $25.00 a month ongoing. Largest: missed payment, Sonoma Dairy ($1,276.62); duplicate charge, "
        "Bay Produce ($421.28); card fee creep, TableTap ($10.00).")
    assert report_summary(tmp_path, "missing.json") is None


async def test_started_knows_a_request_that_already_has_a_draft(root, draft):
    service = Bookkeeper(root, ScriptedClient(draft))
    request = ReportRequest(request_id="r1")
    assert not service.started(request, "agent1-owner")
    await service.run(request, "agent1-owner")
    assert service.started(request, "agent1-owner") and not service.started(request, "agent1-someone-else")
