import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from uagents import Model
from uagents_core.contrib.protocols.chat import ChatMessage, ChatAcknowledgement, EndSessionContent

from bookkeeper.agent import build_protocol, chat_message
from bookkeeper.runner import Bookkeeper
from conftest import ScriptedClient


async def test_local_protocol_ack_then_filename(root, draft):
    service = Bookkeeper(root, ScriptedClient(draft))
    protocol = build_protocol(service)
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
    request = chat_message(json.dumps({"request_id": "orchestrator-1"}))
    handler = protocol.signed_message_handlers[Model.build_schema_digest(ChatMessage)]
    await handler(ctx, "local-orchestrator", request)
    sent = ctx.send.call_args_list
    assert len(sent) == 2
    assert isinstance(sent[0].args[1], ChatAcknowledgement)
    assert sent[0].args[1].acknowledged_msg_id == request.msg_id
    assert all(call.args[0] == "local-orchestrator" for call in sent)
    response = sent[1].args[1]
    body = json.loads(response.content[0].text)
    assert body["verified"] is False
    assert (root / body["filename"]).exists()
    assert isinstance(response.content[1], EndSessionContent)


async def test_bad_request_returns_no_filename(root):
    protocol = build_protocol(Bookkeeper(root))
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
    handler = protocol.signed_message_handlers[Model.build_schema_digest(ChatMessage)]
    await handler(ctx, "local-orchestrator", chat_message("not JSON"))
    body = json.loads(ctx.send.call_args_list[-1].args[1].content[0].text)
    assert body["status"] == "error" and "filename" not in body


async def test_model_error_returns_actionable_message_and_trace(root):
    class FailingService:
        async def run(self, request, sender):
            exc = RuntimeError('ASI API returned HTTP 401')
            exc.bookkeeper_trace_id = 'TR-test'
            raise exc
    protocol = build_protocol(FailingService())
    ctx = SimpleNamespace(send=AsyncMock(), logger=Mock())
    handler = protocol.signed_message_handlers[Model.build_schema_digest(ChatMessage)]
    await handler(ctx, 'local-orchestrator', chat_message('{"request_id":"api-failed"}'))
    body = json.loads(ctx.send.call_args_list[-1].args[1].content[0].text)
    assert body['error'] == 'ASI API returned HTTP 401'
    assert body['trace_id'] == 'TR-test'
    assert 'filename' not in body
