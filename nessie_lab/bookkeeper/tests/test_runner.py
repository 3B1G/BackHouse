import json
from datetime import date

import pytest

from bookkeeper.cli import export_flags
from bookkeeper.contracts import ReportRequest
from bookkeeper.runner import Bookkeeper, immutable_json
from conftest import ScriptedClient


async def test_generate_unverified_idempotent_and_export(root, draft):
    client = ScriptedClient(draft)
    service = Bookkeeper(root, client)
    request = ReportRequest(request_id="req-1", as_of=date(2026, 1, 10))
    result = await service.run(request)
    path = root / result.filename
    saved = path.read_bytes()
    report = json.loads(saved)
    assert report["verification"]["passed"] is False
    assert report["verification"]["checked_at"] is None
    assert report["verification"]["findings_checked"] == 0
    assert report["balances"]["total_cents"] == 13000
    assert report["findings"][0]["amounts"]["actual_cents"] == 4000
    assert await service.run(request) == result
    assert client.calls == 2 and path.read_bytes() == saved
    restarted = Bookkeeper(root, ScriptedClient(draft))
    assert await restarted.run(request) == result
    with pytest.raises(ValueError, match="different parameters"):
        await service.run(ReportRequest(request_id="req-1", as_of=date(2026, 1, 9)))
    with pytest.raises(FileExistsError):
        immutable_json(path, {})
    export_flags(root, path, root / "flags.json")
    assert json.loads((root / "flags.json").read_text()) == [{"bank_id": "Tn2"}]
    trace = json.loads(next((root / "data/bookkeeper_traces").glob("*.json")).read_text())
    assert {e["tool"] for e in trace["events"] if "tool" in e} == {"query_books", "query_nessie"}
    assert "TSECRET" not in json.dumps(trace)


@pytest.mark.parametrize("failure", ["malformed", "network", "budget", "missing_tool"])
async def test_failures_do_not_publish_report(root, draft, failure):
    class Broken(ScriptedClient):
        async def complete(self, messages, tools):
            if failure == "network":
                raise RuntimeError("ASI API returned HTTP 401")
            if failure == "missing_tool":
                return {"content": json.dumps(draft)}
            response = await super().complete(messages, tools)
            if self.calls > 1:
                response["content"] = "not json"
            return response
    service = Bookkeeper(root, Broken(draft), max_turns=1 if failure == "budget" else 40)
    with pytest.raises(Exception):
        await service.run(ReportRequest(request_id="bad"))
    assert not list((root / "data/reports").glob("R-*.json"))
    trace = json.loads(next((root / "data/bookkeeper_traces").glob("*.json")).read_text())
    assert trace["status"] == "failed"


async def test_recover_report_published_before_receipt(root, draft):
    service = Bookkeeper(root, ScriptedClient(draft))
    request = ReportRequest(request_id="recover")
    result = await service.run(request)
    next((root / "data/reports/requests").glob("*.json")).unlink()
    assert await Bookkeeper(root).run(request) == result


async def test_reserves_last_turn_for_report(root, draft, caplog):
    class PersistentInvestigator(ScriptedClient):
        async def complete(self, messages, tools):
            if not tools:
                assert 'Tools are now disabled' in messages[-1]['content']
                return {"role": "assistant", "content": json.dumps(draft)}
            self.calls = 0
            return await super().complete(messages, tools)
    caplog.set_level('INFO', logger='bookkeeper')
    service = Bookkeeper(root, PersistentInvestigator(draft), max_turns=3)
    result = await service.run(ReportRequest(request_id='budget-final'))
    assert (root / result.filename).exists()
    assert 'turn 3/3 (writing report)' in caplog.text
    trace = json.loads(next((root / 'data/bookkeeper_traces').glob('*.json')).read_text())
    assert any('Tools are now disabled' in e.get('instruction', '') for e in trace['events'])


async def test_tool_request_after_finalization_fails_clearly(root, draft):
    class NeverStops(ScriptedClient):
        async def complete(self, messages, tools):
            self.calls = 0
            from bookkeeper.tools import tool_definitions
            return await super().complete(messages, tool_definitions())
    with pytest.raises(RuntimeError, match='Model requested tools during final report generation') as caught:
        await Bookkeeper(root, NeverStops(draft), max_turns=2).run(ReportRequest(request_id='never-stops'))
    trace = json.loads(next((root / 'data/bookkeeper_traces').glob('*.json')).read_text())
    assert trace['error_message'] == str(caught.value)
    assert caught.value.bookkeeper_trace_id == trace['trace_id']
    assert not list((root / 'data/reports').glob('R-*.json'))


async def test_asi_final_request_disables_tools(monkeypatch, draft):
    import httpx
    from bookkeeper.runner import ASIClient
    captured = []
    timeouts = []
    monkeypatch.setenv('ASI_REPORT_TIMEOUT_SECONDS', '600')

    def respond(request):
        captured.append(json.loads(request.content))
        timeouts.append(request.extensions['timeout'])
        return httpx.Response(200, json={'choices': [{'finish_reason': 'stop',
            'message': {'content': json.dumps(draft)}}]})

    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    await ASIClient(api_key='test-only').complete([{'role': 'user', 'content': 'Return JSON'}], [])
    assert captured[0]['tool_choice'] == 'none'
    assert captured[0]['response_format'] == {'type': 'json_object'}
    assert 'tools' not in captured[0]
    assert timeouts[0]['read'] == 600
    assert timeouts[0]['connect'] == 15


async def test_resume_final_timeout_preserves_evidence_and_cash(root, draft):
    import httpx
    import sqlite3

    class TimeoutAtWriting(ScriptedClient):
        async def complete(self, messages, tools):
            if not tools:
                raise httpx.ReadTimeout('writing timed out')
            return await super().complete(messages, tools)

    with pytest.raises(httpx.ReadTimeout) as failure:
        await Bookkeeper(root, TimeoutAtWriting(draft), max_turns=2).run(ReportRequest(request_id='timed-out'))
    trace_id = failure.value.bookkeeper_trace_id
    original_trace = (root / 'data/bookkeeper_traces' / f'{trace_id}.json').read_bytes()
    with sqlite3.connect(root / 'data/books.db') as con:
        con.execute('UPDATE accounts SET opening_balance_cents=0')

    class FinalWriter:
        calls = 0
        async def complete(self, messages, tools):
            self.calls += 1
            assert tools == []
            assert 'Tools are now disabled' in messages[-1]['content']
            assert any(m['role'] == 'tool' and 'n2' in m['content'] for m in messages)
            return {'role': 'assistant', 'content': json.dumps(draft)}

    writer = FinalWriter()
    service = Bookkeeper(root, writer)
    result = await service.resume(trace_id)
    report = json.loads((root / result.filename).read_text())
    assert report['balances']['total_cents'] == 19000
    assert report['verification']['passed'] is False
    assert (root / 'data/bookkeeper_traces' / f'{trace_id}.json').read_bytes() == original_trace
    assert await service.resume(trace_id) == result
    assert writer.calls == 1
    receipt = next((root / 'data/reports/requests').glob('*.json'))
    receipt.unlink()
    assert await service.resume(trace_id) == result
    assert writer.calls == 1


async def test_resume_rejects_investigation_timeout_and_invalid_path(root, draft):
    import httpx
    class TimeoutEarly:
        async def complete(self, messages, tools):
            raise httpx.ReadTimeout('investigation timeout')
    with pytest.raises(httpx.ReadTimeout) as failure:
        await Bookkeeper(root, TimeoutEarly()).run(ReportRequest(request_id='early'))
    with pytest.raises(ValueError, match='Only a final'):
        await Bookkeeper(root).resume(failure.value.bookkeeper_trace_id)
    with pytest.raises(ValueError, match='Invalid trace'):
        await Bookkeeper(root).resume('../../outside')


def test_api_timeouts_are_configurable(monkeypatch):
    from bookkeeper.runner import ASIClient
    monkeypatch.setenv('ASI_REPORT_TIMEOUT_SECONDS', '480')
    monkeypatch.setenv('ASI_READ_TIMEOUT_SECONDS', '90')
    client = ASIClient(api_key='test-only')
    assert client.report_timeout == 480 and client.read_timeout == 90
    monkeypatch.setenv('ASI_REPORT_TIMEOUT_SECONDS', '0')
    with pytest.raises(ValueError, match='positive'):
        ASIClient(api_key='test-only')
