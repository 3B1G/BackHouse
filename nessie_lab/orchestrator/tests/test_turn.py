import json

import pytest

import fakes
from orchestrator import turn as turn_mod
from orchestrator.turn import FOOTER, clean_text, turn

pytestmark = pytest.mark.usefixtures("fake_core")


class Exploding:
    available = True

    async def chat(self, *a, **kw):
        raise AssertionError("the LLM must not be called")


@pytest.mark.parametrize("text", ["How are we doing?", "status", "Summary please", "give me a brief", "how's business"])
async def test_brief_intents_skip_the_llm(text):
    out = await turn(fakes.Session("r"), text, llm=Exploding())
    assert out["text"].startswith("Cash is $12,345.56") and out["degraded"] is False
    assert "deterministic brief" in out["notes"][0]


async def test_long_question_with_status_word_goes_to_the_llm():
    llm, script = fakes.client(fakes.answer("No figures here."))
    out = await turn(fakes.Session("r"), "what is the status of the Mission Dry Goods bill and should I pay it", llm=llm)
    assert len(script.requests) == 1 and out["text"] == "No figures here.\n\n" + FOOTER


async def test_no_key_falls_back_to_the_brief():
    out = await turn(fakes.Session("r"), "What should I change on the menu?")
    assert out["degraded"] is True and "no API key" in out["notes"][0] and "$12,345.56" in out["text"]


async def test_llm_error_falls_back_to_the_brief():
    llm, _ = fakes.client((402, {"error": "no credit"}), key="sk-secret-9")
    out = await turn(fakes.Session("r"), "What should I change?", llm=llm)
    assert out["degraded"] and "402" in out["notes"][0] and "sk-secret-9" not in json.dumps(out)


async def test_brief_failure_is_still_an_answer(monkeypatch):
    import sys

    def broken(session):
        raise RuntimeError("db gone")

    monkeypatch.setattr(sys.modules["orchestrator.brief"], "brief", broken)
    out = await turn(fakes.Session("r"), "status")
    assert out["degraded"] and "brief failed" in out["notes"][-1]


async def test_supported_answer_passes_untouched():
    llm, script = fakes.client(fakes.tool_calls(("c1", "get_menu", {})), fakes.answer("Margherita is $18.00."))
    out = await turn(fakes.Session("r"), "Margherita price?", llm=llm)
    assert out["text"] == "Margherita is $18.00.\n\n" + FOOTER and out["notes"] == [] and len(script.requests) == 2


async def test_unsupported_figures_get_one_fix_request():
    llm, script = fakes.client(fakes.tool_calls(("c1", "get_menu", {})), fakes.answer("Margherita is $19."),
                               fakes.answer("Margherita is $18."))
    out = await turn(fakes.Session("r"), "Margherita price?", llm=llm)
    assert out["text"] == "Margherita is $18.\n\n" + FOOTER and len(script.requests) == 3
    fix = script.requests[2]["messages"][-1]
    assert fix["role"] == "user" and "$19" in fix["content"]
    assert script.requests[2]["messages"][1]["content"] == "Margherita price?"     # continues the same conversation
    assert "asked the model once" in out["notes"][0]


async def test_still_unsupported_after_fix_is_cut_deterministically():
    llm, script = fakes.client(fakes.tool_calls(("c1", "get_menu", {})), fakes.answer("Margherita is $19. Hi."),
                               fakes.answer("Margherita is $18. Rivals charge $30."))
    out = await turn(fakes.Session("r"), "Margherita price?", llm=llm)
    assert len(script.requests) == 3
    assert "$30" not in out["text"].split("Figures I could verify:")[0]
    assert "Margherita is $18." in out["text"] and "Figures I could verify:\n- $18 (get_menu)" in out["text"]
    assert any("removed 1 sentence" in n for n in out["notes"])


async def test_proposal_gets_the_approve_command_even_if_the_model_omits_it(tripwire):
    llm, _ = fakes.client(fakes.tool_calls(("c1", "propose_fix", {"action": "reprice_item"})),
                          fakes.answer("I proposed a new Margherita price."))
    session = fakes.Session("r")
    out = await turn(session, "fix the menu", llm=llm)
    assert out["suggestions"] == [fakes.SUGGESTION_PATH]
    assert fakes.APPROVE in out["text"] and "Nothing has been changed" in out["text"]
    assert tripwire == []


async def test_claim_of_applying_is_fixed():
    llm, script = fakes.client(fakes.tool_calls(("c1", "propose_fix", {})),
                               fakes.answer("I have applied the change."), fakes.answer("I have applied it."))
    out = await turn(fakes.Session("r"), "change it", llm=llm)
    assert "applied" not in out["text"].replace("not applied", "") and len(script.requests) == 3


async def test_card_save_confirms_and_never_approves(tripwire):
    session = fakes.Session("r")
    fakes.call(session, "propose_fix", {})
    out = await turn(session, json.dumps({"action": "save", "suggestion": fakes.SUGGESTION_PATH}), llm=Exploding())
    assert fakes.APPROVE in out["text"] and "pending, not applied" in out["text"]
    assert out["suggestions"] == [fakes.SUGGESTION_PATH] and tripwire == []


async def test_card_discard_drops_the_suggestion_from_the_chat(tripwire):
    session = fakes.Session("r")
    fakes.call(session, "propose_fix", {})
    out = await turn(session, json.dumps({"action": "discard", "suggestion": fakes.SUGGESTION_PATH}), llm=Exploding())
    assert "Discarded" in out["text"] and out["suggestions"] == [] and tripwire == []


async def test_card_for_an_unknown_path_is_refused():
    session = fakes.Session("r")
    out = await turn(session, json.dumps({"action": "save", "suggestion": "../../etc/passwd"}), llm=Exploding())
    assert "don't have that suggestion" in out["text"] and out["suggestions"] == []


async def test_card_action_approve_is_not_a_card_action():
    assert turn_mod.card_action(json.dumps({"action": "approve", "suggestion": "x"})) is None


def test_clean_text_strips_escapes_and_controls():
    assert clean_text("\x1b[31mhi\x1b[0m​ there\x00") == "hi there"
    assert clean_text("a" * 5000) == "a" * 4000


async def test_empty_text():
    out = await turn(fakes.Session("r"), "  \x00 ", llm=Exploding())
    assert "Ask me" in out["text"]
