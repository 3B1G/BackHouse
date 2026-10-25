import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from uagents import Model
from uagents_core.contrib.protocols.chat import ChatMessage, ChatAcknowledgement, EndSessionContent

from bookkeeper.agent import build_protocol, build_verifier_protocol, chat_message
from bookkeeper.runner import Bookkeeper
from bookkeeper.verifier import Verifier
from conftest import ScriptedClient

VERIFIER = "agent1-verifier"
BOOKKEEPER = "agent1-bookkeeper"


def handler_of(protocol):
    return protocol.signed_message_handlers[Model.build_schema_digest(ChatMessage)]


def context():
    return SimpleNamespace(send=AsyncMock(), logger=Mock())


def body_of(call):
    return json.loads(call.args[1].content[0].text)


async def test_draft_goes_to_verifier_and_its_report_back_to_orchestrator(root, draft):
    bk_handler = handler_of(build_protocol(Bookkeeper(root, ScriptedClient(draft)), VERIFIER))
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
    bk_handler = handler_of(build_protocol(Bookkeeper(root, ScriptedClient(draft)), VERIFIER))
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
    bk_handler = handler_of(build_protocol(Bookkeeper(root, ScriptedClient(draft)), VERIFIER))
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
    body = body_of(ctx.send.call_args_list[-1])
    assert body["status"] == "error" and "only the configured Bookkeeper" in body["error"] and "filename" not in body
    assert not (root / "data/verifier_traces").exists()


async def test_bad_request_returns_no_filename(root):
    handler = handler_of(build_protocol(Bookkeeper(root), VERIFIER))
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
    handler = handler_of(build_protocol(FailingService(), VERIFIER))
    ctx = context()
    await handler(ctx, 'local-orchestrator', chat_message('{"request_id":"api-failed"}'))
    body = body_of(ctx.send.call_args_list[-1])
    assert body['error'] == 'ASI API returned HTTP 401'
    assert body['trace_id'] == 'TR-test'
    assert 'filename' not in body
