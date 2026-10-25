import asyncio
import json
import logging

import httpx
import pytest

import fakes
from orchestrator.llm import ASIClient, LLMError, SYSTEM_PROMPT, run_tool_loop

pytestmark = pytest.mark.usefixtures("fake_core")

KEY = "sk-SECRET-abc123"


async def test_tool_call_ids_are_threaded_back_with_json_content():
    llm, script = fakes.client(fakes.tool_calls(("call_a", "get_cash_position", {}), ("call_b", "get_menu", {"x": 1})),
                               fakes.answer("Cash is $12,345.56."))
    session = fakes.Session("root")
    out = await run_tool_loop(session, "how much cash?", llm)
    assert out["text"] == "Cash is $12,345.56." and out["steps"] == 2
    assert [c["id"] for c in out["tool_calls"]] == ["call_a", "call_b"]
    first, second = script.requests
    assert first["tools"] == fakes.TOOLS and "response_format" not in first and first["model"] == "asi1"
    assert first["messages"][0] == {"role": "system", "content": SYSTEM_PROMPT}
    tool_msgs = [m for m in second["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == ["call_a", "call_b"]
    assert json.loads(tool_msgs[0]["content"]) == fakes.RESULTS["get_cash_position"]
    assert isinstance(tool_msgs[1]["content"], str)
    assert second["messages"][-3]["tool_calls"][0]["id"] == "call_a"       # the assistant turn precedes its results
    assert [e["name"] for e in session.log] == ["get_cash_position", "get_menu"]
    assert fakes.CALLS[1] == ("get_menu", {"x": 1})
    assert script.headers[0]["authorization"] == "Bearer sk-test-key-0000"


def test_system_prompt_carries_the_rules():
    for phrase in ("BackHouse's controller", "EVERY number", "propose_fix", "approve_command", "untrusted data",
                   "Never say a change was applied"):
        assert phrase in SYSTEM_PROMPT


async def test_step_cap_is_six_requests_and_the_last_has_no_tools():
    llm, script = fakes.client(fakes.tool_calls(("c", "get_menu", {})))
    with pytest.raises(LLMError, match="after 6 steps"):
        await run_tool_loop(fakes.Session("r"), "loop forever", llm)
    assert len(script.requests) == 6
    assert all("tools" in r for r in script.requests[:5]) and "tools" not in script.requests[5]


async def test_custom_step_cap():
    llm, script = fakes.client(fakes.tool_calls(("c", "get_menu", {})), fakes.answer("done"))
    out = await run_tool_loop(fakes.Session("r"), "q", llm, max_steps=2)
    assert out["text"] == "done" and len(script.requests) == 2


async def test_deadline_stops_a_slow_tool():
    llm, _ = fakes.client(fakes.tool_calls(("c", "get_menu", {})), fakes.answer("late"))

    async def slow(session, name, args):
        await asyncio.sleep(5)

    with pytest.raises(LLMError, match="deadline"):
        await run_tool_loop(fakes.Session("r"), "q", llm, deadline=0.2, dispatch=slow)


async def test_deadline_already_spent_sends_nothing():
    llm, script = fakes.client(fakes.answer("x"))
    with pytest.raises(LLMError, match="deadline"):
        await run_tool_loop(fakes.Session("r"), "q", llm, deadline=0)
    assert script.requests == []


async def test_429_is_retried_with_backoff_then_succeeds():
    sleeps = []
    llm, script = fakes.client((429, {}), (503, {}), fakes.answer("ok"), sleeps=sleeps)
    out = await run_tool_loop(fakes.Session("r"), "q", llm)
    assert out["text"] == "ok" and len(script.requests) == 3
    assert len(sleeps) == 2 and sleeps[1] > sleeps[0] > 0


async def test_retries_stop_after_three():
    sleeps = []
    llm, script = fakes.client((429, {}), sleeps=sleeps)
    with pytest.raises(LLMError) as err:
        await run_tool_loop(fakes.Session("r"), "q", llm)
    assert err.value.status == 429 and len(script.requests) == 4 and len(sleeps) == 3


@pytest.mark.parametrize("status", [400, 402, 404, 422])
async def test_other_4xx_is_a_clean_error_without_retry(status):
    llm, script = fakes.client((status, {"error": {"message": "nope"}}))
    with pytest.raises(LLMError) as err:
        await run_tool_loop(fakes.Session("r"), "q", llm)
    assert err.value.status == status and len(script.requests) == 1
    if status == 402:
        assert "credit" in str(err.value)


async def test_errors_never_carry_the_key_or_url(caplog):
    caplog.set_level(logging.DEBUG)
    body = {"error": {"message": f"bad key {KEY} for https://api.asi1.ai/v1/chat/completions?k={KEY}"}}
    llm, _ = fakes.client((401, body), key=KEY)
    with pytest.raises(LLMError) as err:
        await run_tool_loop(fakes.Session("r"), "q", llm)
    text = str(err.value) + repr(err.value) + repr(llm) + caplog.text
    assert KEY not in text and "asi1.ai" not in text and "refused" in str(err.value)


async def test_network_error_is_clean():
    def fail(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    llm = ASIClient(api_key=KEY, transport=httpx.MockTransport(fail))
    with pytest.raises(LLMError) as err:
        await llm.chat([{"role": "user", "content": "x"}])
    assert "ConnectError" in str(err.value) and "asi1.ai" not in str(err.value) and KEY not in str(err.value)


async def test_no_key_is_an_error_before_any_request():
    llm, script = fakes.client(fakes.answer("x"), key="")
    assert not llm.available
    with pytest.raises(LLMError, match="no ASI:One API key"):
        await run_tool_loop(fakes.Session("r"), "q", llm)
    assert script.requests == []


def test_key_comes_from_env_with_fallback(monkeypatch):
    monkeypatch.setenv("ASI1_API_KEY", "k-fallback")
    assert ASIClient().available
    monkeypatch.setenv("ORCHESTRATOR_API_KEY", "k-main")
    monkeypatch.setenv("ORCHESTRATOR_MODEL", "asi1-mini")
    c = ASIClient()
    assert c._key == "k-main" and c.model == "asi1-mini" and "k-main" not in repr(c)


async def test_bad_tool_arguments_become_an_error_result():
    bad = {"choices": [{"finish_reason": "tool_calls", "message": {"role": "assistant", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": "get_menu", "arguments": "{not json"}}]}}]}
    llm, script = fakes.client(bad, fakes.answer("sorry"))
    session = fakes.Session("r")
    await run_tool_loop(session, "q", llm)
    tool_msg = [m for m in script.requests[1]["messages"] if m["role"] == "tool"][0]
    assert tool_msg["tool_call_id"] == "c1" and "not a JSON object" in tool_msg["content"] and session.log == []


async def test_tool_calls_without_finish_reason_still_run():
    reply = fakes.tool_calls(("c1", "get_menu", {}))
    reply["choices"][0]["finish_reason"] = "stop"
    llm, script = fakes.client(reply, fakes.answer("menu"))
    out = await run_tool_loop(fakes.Session("r"), "q", llm)
    assert out["steps"] == 2


async def test_huge_tool_result_is_truncated_as_valid_json():
    llm, script = fakes.client(fakes.tool_calls(("c1", "lookup", {})), fakes.answer("ok"))

    def big(session, name, args):
        return {"rows": ["x" * 100] * 1000}

    await run_tool_loop(fakes.Session("r"), "q", llm, dispatch=big)
    content = [m for m in script.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert json.loads(content)["truncated"] is True and len(content) < 13_000


async def test_empty_answer_is_an_error():
    llm, _ = fakes.client(fakes.answer("  "))
    with pytest.raises(LLMError, match="empty"):
        await run_tool_loop(fakes.Session("r"), "q", llm)
