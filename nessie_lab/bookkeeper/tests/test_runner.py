import json
import re
import sqlite3
from datetime import date

import httpx
import pytest

from bookkeeper.bookkeeper_tools import succeeded
from bookkeeper.cli import export_flags
from bookkeeper.contracts import Drafted, ReportRequest
from bookkeeper.runner import Bookkeeper, OutputTruncated, immutable_json
from bookkeeper.verifier import Verifier, verify_in_process
from conftest import CUTOFF, ScriptedClient, batch_of, batch_result, tool_call

REAL_ASYNC_CLIENT = httpx.AsyncClient
PLAN = ["card_settlements", "cash_deposits", "delivery_payouts", "supplier:Supplier", "recurring_obligations",
        "unrecorded_charges"]


def only_trace(root):
    return json.loads(next((root / "data/bookkeeper_traces").glob("*.json")).read_text())


def trace_of(root, result):
    """The Bookkeeper's trace for a draft (Drafted) or a verified report (ReportResult)."""
    report = json.loads((root / (result.draft if isinstance(result, Drafted) else result.filename)).read_text())
    return json.loads((root / "data/bookkeeper_traces" / f"{report['bookkeeper']['trace_id']}.json").read_text())


def tool_events(trace):
    return [e for b in trace["batches"] for e in b["events"] if "result" in e]


async def test_draft_is_verified_published_idempotent_and_exported(root, draft):
    client = ScriptedClient(draft)
    service = Bookkeeper(root, client)
    request = ReportRequest(request_id="req-1", as_of=date(2026, 1, 10))
    drafted = await service.run(request)
    assert isinstance(drafted, Drafted) and drafted.draft == f"data/drafts/{drafted.report_id}.json"
    assert not list((root / "data/reports").glob("R-*.json"))         # the Bookkeeper never publishes
    unverified = json.loads((root / drafted.draft).read_text())
    assert unverified["verification"] == {"passed": False, "checked_at": None, "findings_checked": 0, "findings_rejected": 0,
                                          "rounds": 0, "notes": ["Verification was not run; this is an unverified Bookkeeper report."]}
    assert await service.run(request) == drafted and client.calls == len(PLAN) + 1     # a redelivery re-sends the draft

    verifier_client = ScriptedClient(draft)
    result = await verify_in_process(service, Verifier(root, verifier_client), drafted)
    assert result.verified is True and result.filename == f"data/reports/{drafted.report_id}.json"
    assert verifier_client.calls == len(PLAN) + 1 and client.calls == len(PLAN) + 1
    path = root / result.filename
    saved = path.read_bytes()
    report = json.loads(saved)
    assert report["verification"]["passed"] is True and report["verification"]["findings_checked"] == 1
    assert report["verification"]["findings_rejected"] == 0 and report["verification"]["checked_at"]
    assert report["verification"]["rounds"] == 1
    assert report["schema_version"] == "1.2" and report["balances"]["total_cents"] == 15000
    assert report["verifier"]["trace_id"] != report["bookkeeper"]["trace_id"]
    assert report["sources"] == {"nessie_tag": "boh-1loc", "bank_snapshot": "data/nessie_sync_boh-1loc.db", "bank_rows": 67,
                                 "books_db": "data/books.db"}
    assert [f["id"] for f in report["findings"]] == ["F1"] and report["findings"][0]["amounts"]["actual_cents"] == 4000
    assert report["summary"] == dict(errors=0, needs_owner=1, total_impact_cents=4000, recoverable_cents=0, ongoing_monthly_cents=0)
    assert await service.run(request) == result
    assert await Bookkeeper(root, ScriptedClient(draft)).run(request) == result
    assert client.calls == len(PLAN) + 1 and path.read_bytes() == saved
    with pytest.raises(ValueError, match="different parameters"):
        await service.run(ReportRequest(request_id="req-1", as_of=date(2026, 1, 9)))
    with pytest.raises(FileExistsError):
        immutable_json(path, {})
    export_flags(root, path, root / "flags.json")
    assert json.loads((root / "flags.json").read_text()) == [{"bank_id": "Tn2"}]
    trace = only_trace(root)
    assert trace["format"] == "batched/1" and trace["toolset"]["version"] == "domain-11/3" and trace["status"] == "drafted"
    assert [p["id"] for p in trace["plan"]] == PLAN == [b["id"] for b in trace["batches"]]
    assert {b["status"] for b in trace["batches"]} == {"done"} and trace["assembly"]["unreviewed"] == []
    assert trace["sources"]["as_of"] == CUTOFF and trace["balances"]["total_cents"] == 15000
    assert {e["tool"] for e in tool_events(trace)} == {"search_bank_transactions", "search_costs"}
    text = json.dumps(trace)
    assert "TSECRET" not in text and "|ref=" not in text and not re.search(r'"T(n|p)\d', text)
    assert all(e["result"]["cutoff"] == CUTOFF for e in tool_events(trace))


async def test_each_batch_gets_a_fresh_small_context_and_its_tools(root, draft):
    client = ScriptedClient(draft)
    await Bookkeeper(root, client).run(ReportRequest(request_id="fresh"))
    offered = dict(client.seen_tools)
    assert offered["card_settlements"] == ["get_bank_transaction", "search_bank_transactions", "get_agreements", "get_card_batches"]
    assert "search_costs" not in offered["cash_deposits"] and "get_account_balances" in offered["recurring_obligations"]
    trace = only_trace(root)
    for record in trace["batches"]:
        system, user = record["initial_messages"]
        assert f"\nBatch: {record['id']}\n" in system["content"] and "BatchResult schema" in system["content"]
        context = json.loads(user["content"])
        assert context["batch"] == record["id"] and context["window"]["to"] <= trace["sources"]["as_of"]
        assert ("balances" in context) == (record["id"] == "recurring_obligations")


async def test_failed_batch_is_disclosed_not_fatal(root, draft):
    class OneBroken(ScriptedClient):
        async def complete(self, messages, tools):
            if batch_of(messages) == "cash_deposits":
                self.calls += 1
                return {"role": "assistant", "content": "not json"}
            return await super().complete(messages, tools)
    result = await Bookkeeper(root, OneBroken(draft)).run(ReportRequest(request_id="partial"))
    report = json.loads((root / result.draft).read_text())
    assert any(l.startswith("Not reviewed: Cash deposits (") for l in report["limitations"])
    trace = trace_of(root, result)
    cash = next(b for b in trace["batches"] if b["id"] == "cash_deposits")
    assert cash["status"] == "unreviewed" and "BatchResult" in cash["reason"]
    assert trace["assembly"]["unreviewed"] == [["cash_deposits", cash["reason"]]]


@pytest.mark.parametrize("failure", ["network", "missing_tool", "all_malformed"])
async def test_failures_do_not_publish_report(root, draft, failure):
    class Broken(ScriptedClient):
        async def complete(self, messages, tools):
            if failure == "network":
                raise RuntimeError("ASI API returned HTTP 401")
            if failure == "missing_tool":
                return {"content": json.dumps(batch_result(draft))}
            response = await super().complete(messages, tools)
            if not response.get("tool_calls"):
                response["content"] = "not json"
            return response
    with pytest.raises(Exception) as caught:
        await Bookkeeper(root, Broken(draft)).run(ReportRequest(request_id="bad"))
    if failure == "all_malformed":
        assert "No investigation batch produced a result" in str(caught.value)
    assert not list((root / "data/reports").glob("R-*.json")) and not (root / "data/drafts").exists()
    trace = only_trace(root)
    assert trace["status"] == "failed" and trace["error_message"]


async def test_recover_report_published_before_receipt(root, draft):
    service = Bookkeeper(root, ScriptedClient(draft))
    request = ReportRequest(request_id="recover")
    result = await verify_in_process(service, Verifier(root, ScriptedClient(draft)), await service.run(request))
    next((root / "data/reports/requests").glob("*.json")).unlink()
    assert await Bookkeeper(root).run(request) == result
    assert service.complete(result.report_id) == result


async def test_saved_draft_is_sent_again_without_a_new_investigation(root, draft):
    request = ReportRequest(request_id="pending")
    drafted = await Bookkeeper(root, ScriptedClient(draft)).run(request)

    class NeverCalled:
        async def complete(self, messages, tools):
            raise AssertionError("a saved draft must not be investigated again")
    service = Bookkeeper(root, NeverCalled())
    assert await service.run(request) == drafted
    with pytest.raises(ValueError, match="has not published"):
        service.complete(drafted.report_id)
    with pytest.raises(ValueError, match="Invalid report"):
        service.complete("R-../../x")


async def test_batch_turn_cap_reserves_writing_turn(root, draft, caplog):
    class Persistent(ScriptedClient):
        async def complete(self, messages, tools):
            if batch_of(messages) == "unrecorded_charges" and tools:
                self.calls += 1
                return {"role": "assistant", "content": None,
                        "tool_calls": [tool_call(f"{self.calls}-{i}", n, a) for i, (n, a) in enumerate(self.CALLS)]}
            if not tools and batch_of(messages) == "unrecorded_charges":
                assert "Tools are now disabled" in messages[-1]["content"]
            return await super().complete(messages, tools)
    caplog.set_level("INFO", logger="bookkeeper")
    result = await Bookkeeper(root, Persistent(draft), max_turns=3).run(ReportRequest(request_id="cap"))
    assert (root / result.draft).exists()
    assert "unrecorded_charges turn 3/3 (writing result)" in caplog.text
    record = next(b for b in trace_of(root, result)["batches"] if b["id"] == "unrecorded_charges")
    assert sum("message" in e for e in record["events"]) == 3 and record["events"][-2]["instruction"].startswith("Data gathering")


def test_plan_turn_caps():
    from bookkeeper.batches import RECURRING_TURNS, SETTLEMENT_TURNS, SUPPLIER_TURNS, UNRECORDED_TURNS
    assert (SETTLEMENT_TURNS, SUPPLIER_TURNS, RECURRING_TURNS, UNRECORDED_TURNS) == (6, 10, 12, 10)


async def test_tool_request_while_writing_marks_batch_unreviewed(root, draft):
    class AsksForTools(ScriptedClient):
        async def complete(self, messages, tools):
            if batch_of(messages) == "supplier:Supplier":
                self.calls += 1
                return {"role": "assistant", "content": None, "tool_calls": [tool_call(self.calls, "search_costs", {})]}
            return await super().complete(messages, tools)
    result = await Bookkeeper(root, AsksForTools(draft), max_turns=2).run(ReportRequest(request_id="asks"))
    record = next(b for b in trace_of(root, result)["batches"] if b["id"] == "supplier:Supplier")
    assert record["status"] == "unreviewed" and "asked for tools while writing" in record["reason"]


SSE = {"content-type": "text/event-stream"}


def sse(*chunks, done=True):
    """An ASI streamed reply: one `data:` line per chunk, then [DONE]."""
    return "".join([f"data: {json.dumps(c)}\n\n" for c in chunks] + (["data: [DONE]\n\n"] if done else [])).encode()


def delta(finish=None, **fields):
    return {"choices": [{"index": 0, "delta": fields, **({"finish_reason": finish} if finish else {})}]}


class DropsMidStream(httpx.AsyncByteStream):
    """Sends part of a reply, then the connection drops."""

    async def __aiter__(self):
        yield sse(delta(content="par"), done=False)
        raise httpx.ReadError("connection dropped")


def mock_asi(monkeypatch, *replies):
    """Route ASIClient through a mock transport answering with each reply in turn (the last one repeats).

    A reply is SSE bytes, an HTTP status, an httpx exception class, or a fresh AsyncByteStream. Returns (payloads, timeouts)."""
    sent, timeouts, queue = [], [], list(replies)

    def respond(request):
        sent.append(json.loads(request.content))
        timeouts.append(request.extensions["timeout"])
        reply = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(reply, type):
            raise reply("mock failure", request=request)
        if isinstance(reply, int):
            return httpx.Response(reply, json={"error": "no"})
        if isinstance(reply, httpx.AsyncByteStream):
            return httpx.Response(200, stream=reply, headers=SSE)
        return httpx.Response(200, content=reply, headers=SSE)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: REAL_ASYNC_CLIENT(transport=httpx.MockTransport(respond), **kwargs))
    monkeypatch.setenv("ASI_RETRY_BACKOFF_SECONDS", "0")
    return sent, timeouts


def ok_reply():
    return sse(delta(content="ok", finish="stop"))


async def test_asi_streams_and_assembles_text_reasoning_and_usage(monkeypatch):
    from bookkeeper.runner import ASIClient
    monkeypatch.setenv("ASI_REPORT_TIMEOUT_SECONDS", "600")
    sent, timeouts = mock_asi(monkeypatch, sse(
        delta(role="assistant", content=""), delta(reasoning_content="Th"), delta(reasoning_content="ink"),
        delta(content='{"ok":'), delta(finish="stop", content=" true}"), {"choices": [], "usage": {"prompt_tokens": 9}}))
    message = await ASIClient(api_key="test-only").complete([{"role": "user", "content": "Return JSON"}], [])
    assert message == {"role": "assistant", "content": '{"ok": true}', "reasoning_content": "Think", "usage": {"prompt_tokens": 9}}
    payload = sent[0]
    assert payload["stream"] is True and payload["stream_options"] == {"include_usage": True}
    assert payload["tool_choice"] == "none" and payload["response_format"] == {"type": "json_object"} and "tools" not in payload
    assert payload["max_tokens"] == 32000 and timeouts[0]["read"] == 600 and timeouts[0]["connect"] == 15


async def test_asi_assembles_streamed_tool_calls(monkeypatch):
    from bookkeeper.runner import ASIClient
    sent, timeouts = mock_asi(monkeypatch, sse(
        delta(tool_calls=[{"index": 0, "id": "c0", "type": "function", "function": {"name": "get_cost", "arguments": '{"cost_'}}]),
        delta(tool_calls=[{"index": 0, "function": {"arguments": 'id": "now"}'}},
                          {"index": 1, "id": "c1", "function": {"name": "search_costs", "arguments": "{}"}}]),
        delta(finish="tool_calls"), {"choices": [], "usage": {"completion_tokens": 5}}))
    message = await ASIClient(api_key="test-only").complete([{"role": "user", "content": "x"}], [{"type": "function"}])
    assert message["content"] is None and message["usage"] == {"completion_tokens": 5}
    assert message["tool_calls"] == [
        {"id": "c0", "type": "function", "function": {"name": "get_cost", "arguments": '{"cost_id": "now"}'}},
        {"id": "c1", "type": "function", "function": {"name": "search_costs", "arguments": "{}"}}]
    assert sent[0]["tools"] == [{"type": "function"}] and sent[0]["max_tokens"] == 12000 and timeouts[0]["read"] == 120


async def test_asi_output_limit_raises_with_the_partial_reply(monkeypatch):
    from bookkeeper.runner import ASIClient
    mock_asi(monkeypatch, sse(delta(content="partial"), delta(finish="length"), {"choices": [], "usage": {"completion_tokens": 7}}))
    with pytest.raises(OutputTruncated) as cut:
        await ASIClient(api_key="test-only").complete([{"role": "user", "content": "x"}], [])
    assert cut.value.partial == {"role": "assistant", "content": "partial"} and cut.value.usage == {"completion_tokens": 7}


async def test_asi_retries_stalled_and_dropped_streams(monkeypatch, caplog):
    from bookkeeper.runner import ASIClient
    sent, _ = mock_asi(monkeypatch, httpx.ReadTimeout, httpx.ReadTimeout, ok_reply())
    message = await ASIClient(api_key="test-only").complete([{"role": "user", "content": "x"}], [])
    assert len(sent) == 3 and message["content"] == "ok" and message["retries"] == 2
    assert "ASI call failed (ReadTimeout); retry 2/2" in caplog.text
    sent, _ = mock_asi(monkeypatch, DropsMidStream(), ok_reply())
    message = await ASIClient(api_key="test-only").complete([{"role": "user", "content": "x"}], [])
    assert len(sent) == 2 and message["content"] == "ok" and message["retries"] == 1      # the partial reply is discarded


async def test_asi_gives_up_after_retries_and_retries_only_transient_http_errors(monkeypatch, caplog):
    from bookkeeper.runner import ASIClient
    sent, _ = mock_asi(monkeypatch, httpx.ReadTimeout)
    with pytest.raises(httpx.ReadTimeout):
        await ASIClient(api_key="test-only").complete([{"role": "user", "content": "x"}], [])
    assert len(sent) == 3
    sent, _ = mock_asi(monkeypatch, 500, ok_reply())                  # the server's transient failure: retried
    message = await ASIClient(api_key="test-only").complete([{"role": "user", "content": "x"}], [])
    assert len(sent) == 2 and message["content"] == "ok" and message["retries"] == 1
    assert "ASI call failed (ASI API returned HTTP 500); retry 1/2" in caplog.text
    sent, _ = mock_asi(monkeypatch, 503)
    with pytest.raises(RuntimeError, match="HTTP 503"):              # ...until the retries run out
        await ASIClient(api_key="test-only").complete([{"role": "user", "content": "x"}], [])
    assert len(sent) == 3
    sent, _ = mock_asi(monkeypatch, 401)
    with pytest.raises(RuntimeError, match="HTTP 401"):              # any other HTTP error fails at once
        await ASIClient(api_key="test-only").complete([{"role": "user", "content": "x"}], [])
    assert len(sent) == 1
    mock_asi(monkeypatch, ok_reply())
    assert "retries" not in await ASIClient(api_key="test-only").complete([{"role": "user", "content": "x"}], [])


async def test_retries_recorded_on_the_event(root, draft):
    class Retried(ScriptedClient):
        async def complete(self, messages, tools):
            return {**await super().complete(messages, tools), "retries": 1}
    result = await Bookkeeper(root, Retried(draft)).run(ReportRequest(request_id="retried"))
    events = [e for b in trace_of(root, result)["batches"] for e in b["events"] if "message" in e]
    assert events and all(e["retries"] == 1 and "retries" not in e["message"] for e in events)


async def test_usage_recorded_per_call(root, draft):
    class WithUsage(ScriptedClient):
        async def complete(self, messages, tools):
            return {**await super().complete(messages, tools), "usage": {"prompt_tokens": len(messages)}}
    result = await Bookkeeper(root, WithUsage(draft)).run(ReportRequest(request_id="usage"))
    events = [e for b in trace_of(root, result)["batches"] for e in b["events"] if "message" in e]
    assert events and all(e["usage"]["prompt_tokens"] >= 2 and "usage" not in e["message"] for e in events)


class TimeoutAtWriting(ScriptedClient):
    """Says it is done after its tool round, then times out in that batch's result-writing call."""

    def __init__(self, draft, batch="unrecorded_charges", **kw):
        super().__init__(draft, **kw)
        self.batch = batch

    async def complete(self, messages, tools):
        if batch_of(messages) == self.batch and not tools:
            raise httpx.ReadTimeout("writing timed out")
        if batch_of(messages) == self.batch and len(messages) > 2:
            self.calls += 1
            return {"role": "assistant", "content": "Done investigating."}
        return await super().complete(messages, tools)


async def test_resume_batch_writing_timeout_preserves_evidence_and_cash(root, draft):
    with pytest.raises(httpx.ReadTimeout) as failure:
        await Bookkeeper(root, TimeoutAtWriting(draft)).run(ReportRequest(request_id="timed-out"))
    trace_id = failure.value.bookkeeper_trace_id
    original = (root / "data/bookkeeper_traces" / f"{trace_id}.json").read_bytes()
    failed = json.loads(original)
    assert failed["batches"][-1]["status"] == "failed" and failed["batches"][-1]["events"][-1]["instruction"].startswith("Data")
    for name in ["books.db", "nessie_sync_boh-1loc.db"]:                 # resume never reopens the sources
        (root / "data" / name).rename(root / "data" / f"{name}.hidden")

    class Writer:
        calls = 0

        async def complete(self, messages, tools):
            self.calls += 1
            assert tools == [] and batch_of(messages) == "unrecorded_charges"
            assert "Tools are now disabled" in messages[-1]["content"] and messages[-2]["content"] == "Done investigating."
            assert any(m["role"] == "tool" and "n2" in m["content"] for m in messages)
            return {"role": "assistant", "content": json.dumps(batch_result(draft))}
    writer = Writer()
    service = Bookkeeper(root, writer)
    result = await service.resume(trace_id)
    report = json.loads((root / result.draft).read_text())
    assert report["balances"]["total_cents"] == 17000 and report["findings"][0]["bank_ids"] == ["n2"]
    assert report["verification"]["passed"] is False
    assert (root / "data/bookkeeper_traces" / f"{trace_id}.json").read_bytes() == original
    resumed = trace_of(root, result)
    assert resumed["resumed_from"] == trace_id and resumed["batches"][-1]["status"] == "done"
    assert await service.resume(trace_id) == result and writer.calls == 1
    for name in ["books.db", "nessie_sync_boh-1loc.db"]:
        (root / "data" / f"{name}.hidden").rename(root / "data" / name)
    final = await verify_in_process(service, Verifier(root, ScriptedClient(draft)), result)
    assert final.verified and final.report_id == result.report_id
    assert await service.resume(trace_id) == final and writer.calls == 1
    next((root / "data/reports/requests").glob("*.json")).unlink()
    assert await service.resume(trace_id) == final and writer.calls == 1


async def test_resume_several_batches_and_disclose_investigation_failures(root, draft):
    class Failing(ScriptedClient):
        async def complete(self, messages, tools):
            batch = batch_of(messages)
            if batch in ("unrecorded_charges", "supplier:Supplier") and not tools:
                raise httpx.ReadTimeout("writing timed out")
            if batch in ("unrecorded_charges", "supplier:Supplier") and len(messages) > 2:
                return {"role": "assistant", "content": "Done investigating."}
            if batch == "supplier:Supplier":                 # one tool round, so it reaches its writing turn
                return {"role": "assistant", "content": None, "tool_calls": [tool_call("s", "search_costs", {"limit": 1})]}
            if batch == "delivery_payouts":
                raise httpx.ConnectError("investigation lost")
            return await super().complete(messages, tools)
    with pytest.raises((httpx.ReadTimeout, httpx.ConnectError)) as failure:
        await Bookkeeper(root, Failing(draft), concurrency=2).run(ReportRequest(request_id="several"))
    failed = only_trace(root)
    status = {b["id"]: b["status"] for b in failed["batches"]}
    assert status == {"card_settlements": "done", "cash_deposits": "done", "delivery_payouts": "failed",
                      "supplier:Supplier": "failed", "recurring_obligations": "done", "unrecorded_charges": "failed"}

    class Writer:
        seen = []

        async def complete(self, messages, tools):
            self.seen.append(batch_of(messages))
            return {"role": "assistant", "content": json.dumps(batch_result(draft if batch_of(messages) == "unrecorded_charges" else None))}
    writer = Writer()
    result = await Bookkeeper(root, writer).resume(failure.value.bookkeeper_trace_id)
    assert writer.seen == ["supplier:Supplier", "unrecorded_charges"]
    report = json.loads((root / result.draft).read_text())
    assert report["findings"][0]["bank_ids"] == ["n2"]
    assert "Not reviewed: Delivery payouts (failed during investigation (ConnectError))." in report["limitations"]


async def test_batches_run_in_parallel_up_to_the_limit(root, draft):
    import asyncio

    class Slow(ScriptedClient):
        active = peak = 0

        async def complete(self, messages, tools):
            Slow.active += 1
            Slow.peak = max(Slow.peak, Slow.active)
            await asyncio.sleep(0.05)
            Slow.active -= 1
            return await super().complete(messages, tools)
    Slow.peak = 0
    result = await Bookkeeper(root, Slow(draft), concurrency=3).run(ReportRequest(request_id="parallel"))
    assert Slow.peak == 3
    trace = trace_of(root, result)
    assert [b["id"] for b in trace["batches"]] == PLAN                # plan order, whatever order they finished in
    Slow.peak = 0
    await Bookkeeper(root, Slow(draft), concurrency=1).run(ReportRequest(request_id="serial"))
    assert Slow.peak == 1
    with pytest.raises(ValueError, match="at least 1"):
        Bookkeeper(root, concurrency=-1)


async def test_direct_and_fenced_results_need_no_extra_call(root, draft):
    class Fenced(ScriptedClient):
        async def complete(self, messages, tools):
            response = await super().complete(messages, tools)
            if not response.get("tool_calls"):
                response["content"] = "```json\n" + response["content"] + "\n```"
            return response
    client = Fenced(draft)
    result = await Bookkeeper(root, client).run(ReportRequest(request_id="fenced"))
    assert client.calls == len(PLAN) + 1 and json.loads((root / result.draft).read_text())["findings"][0]["bank_ids"] == ["n2"]
    assert all(len(tools) == len(next(t for b, t in client.seen_tools if b == batch)) for batch, tools in client.seen_tools)


async def test_resume_rejects_investigation_timeout_one_sided_evidence_and_bad_path(root, draft):
    class TimeoutEarly:
        async def complete(self, messages, tools):
            raise httpx.ReadTimeout("investigation timeout")
    with pytest.raises(httpx.ReadTimeout) as failure:
        await Bookkeeper(root, TimeoutEarly()).run(ReportRequest(request_id="early"))
    with pytest.raises(ValueError, match="Only a result-writing"):
        await Bookkeeper(root).resume(failure.value.bookkeeper_trace_id)
    with pytest.raises(ValueError, match="Invalid trace"):
        await Bookkeeper(root).resume("../../outside")
    with pytest.raises(httpx.ReadTimeout) as failure:
        await Bookkeeper(root, TimeoutAtWriting(draft, calls=[("search_bank_transactions", {})])).run(
            ReportRequest(request_id="one-sided"))

    class Writer:
        async def complete(self, messages, tools):
            return {"role": "assistant", "content": json.dumps(batch_result(draft))}
    with pytest.raises(ValueError, match="both bank and books"):
        await Bookkeeper(root, Writer()).resume(failure.value.bookkeeper_trace_id)


def test_api_settings_are_configurable(monkeypatch):
    from bookkeeper.runner import ASIClient
    monkeypatch.setenv("ASI_REPORT_TIMEOUT_SECONDS", "480")
    monkeypatch.setenv("ASI_READ_TIMEOUT_SECONDS", "90")
    monkeypatch.setenv("ASI_REPORT_MAX_TOKENS", "16000")
    client = ASIClient(api_key="test-only")
    assert client.report_timeout == 480 and client.read_timeout == 90 and client.report_max_tokens == 16000
    monkeypatch.setenv("ASI_REPORT_TIMEOUT_SECONDS", "0")
    with pytest.raises(ValueError, match="positive"):
        ASIClient(api_key="test-only")
    monkeypatch.setenv("ASI_REPORT_TIMEOUT_SECONDS", "480")
    monkeypatch.setenv("ASI_MAX_TOKENS", "0")
    with pytest.raises(ValueError, match="token limits"):
        ASIClient(api_key="test-only")
    monkeypatch.setenv("ASI_MAX_TOKENS", "12000")
    monkeypatch.setenv("ASI_RETRIES", "5")
    monkeypatch.setenv("ASI_RETRY_BACKOFF_SECONDS", "1.5")
    client = ASIClient(api_key="test-only")
    assert client.retries == 5 and client.backoff == 1.5
    monkeypatch.setenv("ASI_RETRIES", "-1")
    with pytest.raises(ValueError, match="must not be negative"):
        ASIClient(api_key="test-only")


@pytest.mark.parametrize("script,ok", [
    ([("search_bank_transactions", {})], False),                                     # books never consulted
    ([("search_costs", {})], False),                                                 # bank never consulted
    ([("get_bank_transaction", {"bank_id": "nope"}), ("search_costs", {})], False),  # errors are not evidence
    ([("get_card_batches", {"date_from": "2026-01-01", "date_to": "2026-01-10"}),    # not available in this batch
      ("search_bank_transactions", {})], False),
    ([("get_payee_history", {"text": "SUPPLIER"}), ("get_cost", {"cost_id": "now"})], True),   # any usable pair, not all 11
])
async def test_requires_evidence_from_both_groups(root, draft, script, ok):
    service = Bookkeeper(root, ScriptedClient(draft, script))
    if ok:
        assert (root / (await service.run(ReportRequest(request_id="evidence"))).draft).exists()
        return
    with pytest.raises(ValueError, match="both bank and books"):
        await service.run(ReportRequest(request_id="evidence"))
    assert not list((root / "data/reports").glob("R-*.json"))


async def test_tool_call_limits_and_bad_arguments(root, draft):
    script = ScriptedClient.CALLS + [("get_payee_history", {"text": "A"})] * 18 + [("get_cost", {"cost_id": "now"})] * 3
    result = await Bookkeeper(root, ScriptedClient(draft, script)).run(ReportRequest(request_id="many"))
    results = tool_events(trace_of(root, result))
    assert len(results) == 23 and all(succeeded(e["result"]) for e in results[:20])
    assert all(e["result"]["error"].startswith("Not run: at most 20 tool calls") for e in results[20:])

    class BadArguments(ScriptedClient):
        async def complete(self, messages, tools):
            response = await super().complete(messages, tools)
            if response.get("tool_calls"):
                response["tool_calls"].append({"id": "bad", "type": "function", "function": {"name": "search_costs", "arguments": "{x"}})
                response["tool_calls"].append(tool_call(9, "get_cost", {"cost_id": "now", "extra": 1}))
            return response
    result = await Bookkeeper(root, BadArguments(draft)).run(ReportRequest(request_id="bad-args"))
    errors = {e["tool_call_id"]: e["result"]["error"] for e in tool_events(trace_of(root, result)) if "error" in e["result"]}
    assert errors["bad"] == "tool arguments must be a JSON object" and "unknown field 'extra'" in errors["c9"]


class DoneThenWrite(ScriptedClient):
    """The investigating batch searches, says it is done (or is cut off), then writes its result with tools disabled."""

    def __init__(self, draft, truncate=None):
        super().__init__(draft)
        self.truncate = truncate             # None or "investigation"

    async def complete(self, messages, tools):
        if batch_of(messages) != "unrecorded_charges" or len(messages) == 2:
            return await super().complete(messages, tools)
        self.calls += 1
        self.seen_tools.append((batch_of(messages), [t["function"]["name"] for t in tools]))
        if tools:
            if self.truncate == "investigation":
                raise OutputTruncated({"content": '{"coverage": {"bank_rows_rev'}, {"completion_tokens": 12000})
            return {"role": "assistant", "content": "Investigation complete; ready to write the result."}
        assert "Tools are now disabled" in messages[-1]["content"]
        return {"role": "assistant", "content": json.dumps(batch_result(self.draft))}


@pytest.mark.parametrize("truncate", [None, "investigation"])
async def test_done_note_or_cut_off_reply_hands_over_to_writing(root, draft, truncate):
    client = DoneThenWrite(draft, truncate)
    result = await Bookkeeper(root, client).run(ReportRequest(request_id=f"handoff-{truncate}"))
    report = json.loads((root / result.draft).read_text())
    assert report["findings"][0]["bank_ids"] == ["n2"]
    offered = [len(tools) for batch, tools in client.seen_tools if batch == "unrecorded_charges"]
    assert offered == [5, 5, 0]
    record = next(b for b in trace_of(root, result)["batches"] if b["id"] == "unrecorded_charges")
    cut = [e for e in record["events"] if "truncated_message" in e]
    assert len(cut) == (1 if truncate else 0) and record["events"][-2]["instruction"].startswith("Data gathering")


async def test_cut_off_result_marks_batch_unreviewed(root, draft):
    class CutOffSupplier(ScriptedClient):
        async def complete(self, messages, tools):
            if batch_of(messages) == "supplier:Supplier" and not tools:
                raise OutputTruncated({"content": '{"coverage"'}, {"completion_tokens": 32000})
            if batch_of(messages) == "supplier:Supplier":
                return {"role": "assistant", "content": "Nothing more to check."}
            return await super().complete(messages, tools)
    result = await Bookkeeper(root, CutOffSupplier(draft)).run(ReportRequest(request_id="cut-supplier"))
    record = next(b for b in trace_of(root, result)["batches"] if b["id"] == "supplier:Supplier")
    assert record["status"] == "unreviewed" and "cut off" in record["reason"]
    assert record["events"][-1]["truncated_message"] == {"content": '{"coverage"'}


async def test_resume_legacy_sql_trace(root, draft):
    trace_id = "TR-" + "a" * 32
    context = {"as_of": CUTOFF, "window_start": "2026-01-01", "bank_rows": 6,
               "balances": {"as_of": CUTOFF, "accounts": [], "total_cents": 13000}, "limitations": ["Legacy limitation."]}
    legacy = {"trace_id": trace_id, "request": {"request_id": "legacy-1", "as_of": CUTOFF, "nessie_tag": "boh-1loc"},
              "model": "asi1", "status": "failed", "error_type": "ReadTimeout",
              "initial_messages": [{"role": "system", "content": "old prompt"}, {"role": "user", "content": json.dumps(context)}],
              "events": [
                  {"turn": 0, "message": {"content": None, "tool_calls": [{"id": "c0"}, {"id": "c1"}]}},
                  {"tool_call_id": "c0", "tool": "query_nessie", "arguments": {}, "result": {"rows": [{"nessie_id": "n2"}]}},
                  {"tool_call_id": "c1", "tool": "query_books", "arguments": {}, "result": {"rows": []}},
                  {"turn": 1, "instruction": "Data gathering is complete. Tools are now disabled. Produce the final Draft JSON"}]}
    immutable_json(root / "data/bookkeeper_traces" / f"{trace_id}.json", legacy)

    class Writer:
        async def complete(self, messages, tools):
            assert tools == [] and any(m["role"] == "tool" and "n2" in m["content"] for m in messages)
            return {"role": "assistant", "content": json.dumps(draft)}
    service = Bookkeeper(root, Writer())
    result = await service.resume(trace_id)
    report = json.loads((root / result.draft).read_text())
    assert report["balances"]["total_cents"] == 13000 and report["sources"]["bank_rows"] == 6
    assert report["sources"]["bank_snapshot"] == "data/nessie_sync_boh-1loc.db" and "Legacy limitation." in report["limitations"]
    assert await service.resume(trace_id) == result

    one_sided = dict(legacy, trace_id="TR-" + "b" * 32, events=[e for e in legacy["events"] if e.get("tool") != "query_books"])
    immutable_json(root / "data/bookkeeper_traces" / f"{one_sided['trace_id']}.json", one_sided)
    with pytest.raises(ValueError, match="did not access both"):
        await service.resume(one_sided["trace_id"])


async def test_resume_single_conversation_domain_trace(root, draft):
    """Traces from the unbatched domain tools (domain-11/1 and /2) still resume through the conversation path."""
    trace_id = "TR-" + "d" * 32
    context = {"as_of": CUTOFF, "window_start": "2026-01-01", "bank_rows": 67,
               "balances": {"as_of": CUTOFF, "accounts": [], "total_cents": 15000}, "limitations": []}
    saved = {"trace_id": trace_id, "request": {"request_id": "single-1", "as_of": CUTOFF, "nessie_tag": "boh-1loc"},
             "model": "asi1", "status": "failed", "error_type": "ReadTimeout",
             "toolset": {"version": "domain-11/2", "groups": {"bank": ["search_bank_transactions"], "books": ["search_costs"]}},
             "sources": {"nessie_tag": "boh-1loc", "bank_snapshot": "data/nessie_sync_boh-1loc.db", "books_db": "data/books.db",
                         "bank_rows": 67, "as_of": CUTOFF, "window_start": "2026-01-01"},
             "initial_messages": [{"role": "system", "content": "p"}, {"role": "user", "content": json.dumps(context)}],
             "events": [{"turn": 0, "message": {"content": None, "tool_calls": [{"id": "c0"}, {"id": "c1"}]}},
                        {"tool_call_id": "c0", "tool": "search_bank_transactions", "arguments": {}, "result": {"rows": [], "cutoff": CUTOFF}},
                        {"tool_call_id": "c1", "tool": "search_costs", "arguments": {}, "result": {"rows": [], "cutoff": CUTOFF}},
                        {"turn": 1, "message": {"content": "done"}},
                        {"turn": 2, "instruction": "Data gathering is complete. Tools are now disabled."}]}
    immutable_json(root / "data/bookkeeper_traces" / f"{trace_id}.json", saved)

    class Writer:
        async def complete(self, messages, tools):
            assert messages[-2] == {"role": "assistant", "content": "done"}
            return {"role": "assistant", "content": json.dumps(draft)}
    report = json.loads((root / (await Bookkeeper(root, Writer()).resume(trace_id)).draft).read_text())
    assert report["sources"]["bank_rows"] == 67 and report["as_of"] == CUTOFF


async def test_saved_reports_are_returned_unchanged(root, draft):
    result = await Bookkeeper(root, ScriptedClient(draft)).run(ReportRequest(request_id="keep"))
    before = (root / result.draft).read_bytes()

    class NeverCalled:
        async def complete(self, messages, tools):
            raise AssertionError("a saved report must not be regenerated")
    assert await Bookkeeper(root, NeverCalled()).run(ReportRequest(request_id="keep")) == result
    assert (root / result.draft).read_bytes() == before


def made_up(draft):
    bad = json.loads(json.dumps(draft))
    bad["findings"][0]["bank_ids"] = ["made-up-id"]
    bad["findings"][0]["treatment"] = {"kind": "exclude", "bank_ids": ["made-up-id"]}
    return bad


def wrong_treatment(draft):
    bad = json.loads(json.dumps(draft))
    bad["findings"][0]["treatment"] = {"kind": "correct_to", "bank_id": "n2", "cents": 4000}
    return bad


@pytest.mark.parametrize("breaks,problem", [
    (made_up, "finding F1: bank ids no tool returned"),
    (wrong_treatment, "finding F1: unrecorded_charge findings take the exclude treatment, not correct_to"),
])
async def test_early_result_breaking_hand_off_rules_gets_one_corrected_write(root, draft, breaks, problem):
    class FixesIt(ScriptedClient):
        writes = []

        async def complete(self, messages, tools):
            if batch_of(messages) != "unrecorded_charges" or len(messages) == 2:
                return await super().complete(messages, tools)
            self.calls += 1
            if tools:                                          # an early result that breaks a hand-off rule
                return {"role": "assistant", "content": json.dumps(batch_result(breaks(self.draft)))}
            self.writes.append(messages[-1]["content"])
            return {"role": "assistant", "content": json.dumps(batch_result(self.draft))}
    client = FixesIt(draft)
    client.writes = []
    result = await Bookkeeper(root, client).run(ReportRequest(request_id="fix"))
    [instruction] = client.writes
    assert "Tools are now disabled" in instruction and f"Fix these problems in your result: {problem}" in instruction
    report = json.loads((root / result.draft).read_text())
    assert report["findings"][0]["bank_ids"] == ["n2"] and not any(l.startswith("Left out") for l in report["limitations"])


async def test_final_write_breaking_hand_off_rules_gets_one_correction(root, draft, caplog):
    class FixesOnRequest(ScriptedClient):
        writes = []

        async def complete(self, messages, tools):
            if batch_of(messages) != "unrecorded_charges" or len(messages) == 2:
                return await super().complete(messages, tools)
            self.calls += 1
            assert tools == []
            self.writes.append(messages[-1]["content"])
            fixed = "Fix these problems" in messages[-1]["content"]
            return {"role": "assistant", "content": json.dumps(batch_result(self.draft if fixed else wrong_treatment(self.draft)))}
    caplog.set_level("INFO", logger="bookkeeper")
    client = FixesOnRequest(draft)
    client.writes = []
    result = await Bookkeeper(root, client, max_turns=2).run(ReportRequest(request_id="final-fix"))
    assert len(client.writes) == 2 and "take the exclude treatment, not correct_to" in client.writes[1]
    assert "unrecorded_charges turn 3/3 (writing result)" in caplog.text
    report = json.loads((root / result.draft).read_text())
    assert report["findings"][0]["treatment"]["kind"] == "exclude" and not any(l.startswith("Left out") for l in report["limitations"])


async def test_written_finding_breaking_hand_off_rules_is_left_out_and_disclosed(root, draft):
    class StillWrong(ScriptedClient):
        writes = 0

        async def complete(self, messages, tools):
            if batch_of(messages) == "unrecorded_charges" and len(messages) > 2:
                self.calls += 1
                self.writes += 1
                both = made_up(self.draft)
                both["findings"].append(json.loads(json.dumps(self.draft))["findings"][0] | {"id": "F2"})
                return {"role": "assistant", "content": json.dumps(batch_result(both))}
            return await super().complete(messages, tools)
    client = StillWrong(draft)
    result = await Bookkeeper(root, client, max_turns=2).run(ReportRequest(request_id="drop"))
    assert client.writes == 2                                                 # the write, then one correction that still fails
    report = json.loads((root / result.draft).read_text())
    assert [f["bank_ids"] for f in report["findings"]] == [["n2"]]          # the valid finding is kept
    assert any(l.startswith("Left out a Charges with no supporting document finding (Supplier, unrecorded_charge): "
                            "bank ids no tool returned in this batch: made-up-id") for l in report["limitations"])
    record = next(b for b in trace_of(root, result)["batches"] if b["id"] == "unrecorded_charges")
    assert record["dropped_findings"] == [{"id": "F1", "problems": ["bank ids no tool returned in this batch: made-up-id"]}]


async def test_resume_applies_the_hand_off_rules(root, draft):
    with pytest.raises(httpx.ReadTimeout) as failure:
        await Bookkeeper(root, TimeoutAtWriting(draft)).run(ReportRequest(request_id="resume-rules"))

    class WrongWriter:
        async def complete(self, messages, tools):
            return {"role": "assistant", "content": json.dumps(batch_result(made_up(draft)))}
    result = await Bookkeeper(root, WrongWriter()).resume(failure.value.bookkeeper_trace_id)
    report = json.loads((root / result.draft).read_text())
    assert report["findings"] == [] and any(l.startswith("Left out a Charges with no supporting document finding")
                                            for l in report["limitations"])


async def test_revise_redoes_only_named_batches_and_resumes_from_the_latest_draft(root, draft):
    from bookkeeper.contracts import RevisionRequest
    request = ReportRequest(request_id="revise")
    service = Bookkeeper(root, ScriptedClient(draft))
    first = await service.run(request)
    folder = root / "data/verifications" / first.report_id
    immutable_json(folder / "round-1.json", {"feedback": {"supplier:Supplier": [{"kind": "missed", "verifier_finding": {}}]}})
    ask = RevisionRequest(request_id="revise", report_id=first.report_id, round=2,
                          feedback=f"data/verifications/{first.report_id}/round-1.json")
    client = ScriptedClient(draft)
    second = await Bookkeeper(root, client).revise(ask)
    assert second.round == 2 and second.draft == f"data/drafts/{first.report_id}.r2.json"
    assert {batch for batch, _ in client.seen_tools} == {"supplier:Supplier"}
    trace = trace_of(root, second)
    assert trace["revision_of"] == trace_of(root, first)["trace_id"] and [b["id"] for b in trace["batches"]] == PLAN
    assert await Bookkeeper(root, client).revise(ask) == second and len(client.seen_tools) == 1     # idempotent
    assert await service.run(request) == second                                     # a redelivery resumes at round 2
    with pytest.raises(ValueError, match="under data/verifications"):
        await service.revise(ask.model_copy(update={"round": 3, "feedback": "data/drafts/x.json"}))
    with pytest.raises(ValueError, match="no round 3 draft"):
        await service.revise(ask.model_copy(update={"round": 4}))
    with sqlite3.connect(root / "data/nessie_sync_boh-1loc.db") as con:
        con.execute("INSERT INTO bank_tx (nessie_id, account, nessie_type, date, amount, status, counterparty, memo, bank_id) "
                    "VALUES ('late', 'CHS', 'deposit', '2026-01-09', 100, 'completed', NULL, 'BRANCH DEPOSIT', 'Tlate')")
    with pytest.raises(RuntimeError, match="snapshot changed"):
        await service.revise(ask.model_copy(update={"round": 3}))
