"""Deterministic questionnaire progress (asked-tracking).

Reproduces the production restart-to-Q1 loop (LangSmith call 3025198f): the agent
had asked Q1-Q3, the caller said a bare "Hello" (a non-answer), and the model
RESTARTED the whole call from Q1 even with the full history present. A prompt-
only "never loop back" rule had already failed once, so progress is now computed
deterministically from history and pinned into the prompt.

Unit tests — pure functions, no DB/LLM.
"""
from __future__ import annotations

from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    questionnaire_asked_state,
    render_questionnaire_block,
)

# The exact 5 questions from the failing campaign.
QUESTIONS = [
    {"id": "q1", "type": "intent", "text": "This call is a small survey campaign, would you spare few seconds for us?", "required": "yes", "gate": True},
    {"id": "q2", "type": "intent", "text": "Are you looking to buy a home?", "required": "yes"},
    {"id": "q3", "type": "answer", "text": "What type of house are you looking to buy?", "desired_answer": ""},
    {"id": "q4", "type": "answer", "text": "What is your budget?", "desired_answer": ""},
    {"id": "q5", "type": "intent", "text": "Would you like to get a call back from our team regarding this project?", "required": "yes"},
]

# History at the moment of the T4 restart (assistant turns are the agent's asks).
HISTORY_AT_T4 = [
    {"role": "user", "content": "Hello"},
    {"role": "assistant", "content": "[warm]Hi, thanks for picking up — this call is a small survey campaign, would you spare few seconds for us?[/warm]"},
    {"role": "user", "content": "Yeah, sure, I can do that."},
    {"role": "assistant", "content": "[warm]Great, thanks — are you looking to buy a home?[/warm]"},
    {"role": "user", "content": "Yeah"},
    {"role": "assistant", "content": "[warm]Got it.[/warm] [question]What type of house are you looking to buy?[/question]"},
]


def _ctx() -> OutboundCampaignContext:
    return OutboundCampaignContext(
        campaign_id="c1",
        name="My Home",
        goal="Qualify home buyers.",
        agent_prompt="",
        objectives=[],
        exit_conditions=[],
        tone=None,
        doc_text=None,
        caller_name="Riya",
        company_name="My Home constructions",
        questions=QUESTIONS,
        question_threshold=3,
    )


def test_asked_state_detects_q1_q2_q3():
    state = questionnaire_asked_state(QUESTIONS, HISTORY_AT_T4)
    assert state["asked_numbers"] == [1, 2, 3]
    assert state["asked_count"] == 3
    assert state["next_number"] == 4
    assert state["last_asked_number"] == 3


def test_restart_scenario_pins_reask_not_restart():
    # The bug: caller says "Hello" (non-answer) after Q3 → model restarted at Q1.
    block = render_questionnaire_block(
        _ctx(), history=HISTORY_AT_T4, latest_user_text="Hello"
    )
    # A loud, top-of-prompt "do not restart" directive must be present...
    assert "DO NOT RESTART" in block
    assert "already greeted" in block.lower()
    # ...naming the questions already asked...
    assert "Q1, Q2, Q3" in block
    # ...and pinning a RE-ASK of the current question (Q3), never Q1.
    assert "RE-ASK Q3" in block
    assert "What type of house are you looking to buy?" in block
    # The directive must come BEFORE the static question list.
    assert block.index("DO NOT RESTART") < block.index("LEAD-CAPTURE QUESTIONNAIRE")


def test_real_answer_advances_to_next_question():
    # If the last reply IS a real answer to Q3, advance to Q4 (not re-ask).
    history = HISTORY_AT_T4 + [{"role": "user", "content": "A villa, three or four bedrooms"}]
    block = render_questionnaire_block(
        _ctx(), history=history, latest_user_text="A villa, three or four bedrooms"
    )
    assert "ASK THIS QUESTION NEXT" in block
    assert "What is your budget?" in block  # Q4
    assert "DO NOT RESTART" in block


def test_no_directive_before_first_question():
    # Empty history (call just opened) → no progress directive, normal opening.
    block = render_questionnaire_block(_ctx(), history=[], latest_user_text="Hello")
    assert "DO NOT RESTART" not in block
    assert "LEAD-CAPTURE QUESTIONNAIRE" in block


def test_all_questions_asked_directs_to_close():
    full_history = HISTORY_AT_T4 + [
        {"role": "user", "content": "A villa"},
        {"role": "assistant", "content": "[question]What is your budget?[/question]"},
        {"role": "user", "content": "Around two crore"},
        {"role": "assistant", "content": "[question]Would you like to get a call back from our team regarding this project?[/question]"},
    ]
    state = questionnaire_asked_state(QUESTIONS, full_history)
    assert state["asked_numbers"] == [1, 2, 3, 4, 5]
    assert state["next_number"] is None
    block = render_questionnaire_block(
        _ctx(), history=full_history, latest_user_text="Yes please"
    )
    assert "asked every question" in block.lower()


def test_backend_does_not_match_unasked_questions():
    # Only Q1 asked → Q2-Q5 must NOT be flagged asked (no spurious token overlap).
    history = [
        {"role": "assistant", "content": "[warm]Hi, this call is a small survey campaign, would you spare few seconds for us?[/warm]"},
    ]
    state = questionnaire_asked_state(QUESTIONS, history)
    assert state["asked_numbers"] == [1]
    assert state["next_number"] == 2


# ═══════════════════════════════════════════════════════════════════════════
# AUTHORITATIVE delivered-set tracking (2026-07-06 loop incident, call 63e263db):
# the agent asked all 4 questions then re-asked Q4 and cycled Q2→Q3→… because
# (a) fuzzy tracking missed LLM-paraphrased / native-script lines and (b) no
# close existed without an outro. Delivered-union makes progress MONOTONIC.
# ═══════════════════════════════════════════════════════════════════════════
import inspect

import pytest

from app.services.agent_outbound_context import (
    get_delivered_questions,
    next_question_to_advance,
    next_verbatim_question,
    questionnaire_is_complete,
    set_delivered_questions,
)

_QS4 = [
    {"id": "a1", "type": "intent", "text": "Are you interested in receiving more details about the apartments?", "required": "yes", "desired_answer": ""},
    {"id": "b2", "type": "answer", "text": "Would you prefer a brochure emailed to you or to schedule a site visit?", "desired_answer": "site visit"},
    {"id": "c3", "type": "answer", "text": "Which configuration are you considering, three or four BHK?", "desired_answer": "3bhk"},
    {"id": "d4", "type": "intent", "text": "Would you like our team to call you back regarding this project?", "required": "yes", "desired_answer": ""},
]


@pytest.fixture(autouse=True)
def _clear_ambient_delivered():
    set_delivered_questions(None)
    yield
    set_delivered_questions(None)


def test_paraphrase_invisible_to_fuzzy_but_covered_by_delivered():
    history = [
        {"role": "assistant", "content": "Shall I send you our PDF or set up a quick tour?"},
    ]
    fuzzy_only = questionnaire_asked_state(_QS4, history)
    assert 2 not in fuzzy_only["asked_numbers"]  # the incident's failure mode
    with_delivered = questionnaire_asked_state(_QS4, history, delivered=[1, 2])
    assert with_delivered["next_number"] == 3    # monotonic — never points backwards


def test_native_script_and_evicted_history_covered_by_delivered():
    history = [{"role": "assistant", "content": "क्या आप साइट विज़िट करना चाहेंगे?"}]
    assert questionnaire_asked_state(_QS4, history, delivered=[1, 2, 3])["next_number"] == 4
    # Opener/Q1 evicted by the history cap → empty history, delivered still holds.
    state = questionnaire_asked_state(_QS4, [], delivered=[1, 2, 3, 4])
    assert state["asked_count"] == 4 and state["next_number"] is None


def test_ambient_contextvar_is_the_default_and_arg_overrides():
    set_delivered_questions([1, 2])
    assert questionnaire_asked_state(_QS4, [])["asked_numbers"] == [1, 2]
    assert questionnaire_asked_state(_QS4, [], delivered=[1])["asked_numbers"] == [1]
    assert sorted(get_delivered_questions()) == [1, 2]
    # Garbage numbers are ignored.
    assert questionnaire_asked_state(_QS4, [], delivered=[0, 99, "x", 2])["asked_numbers"] == [2]


def test_complete_via_delivered_even_when_fuzzy_sees_nothing():
    set_delivered_questions([1, 2, 3, 4])
    assert questionnaire_is_complete(_QS4, [], "Yeah, sure.") is True
    set_delivered_questions([1, 2, 3])
    assert questionnaire_is_complete(_QS4, [], "Yeah, sure.") is False


def test_advance_is_i18n_agnostic_but_verbatim_requires_i18n():
    set_delivered_questions([1])
    plan = next_question_to_advance(_QS4, [], "yes I am interested in the details")
    assert plan is not None and plan[0] == 2
    assert next_verbatim_question(_QS4, [], "yes I am interested in the details") is None
    qs_i18n = [dict(q, text_i18n={"en": q["text"]}) for q in _QS4]
    vplan = next_verbatim_question(qs_i18n, [], "yes I am interested in the details")
    assert vplan is not None and vplan[0] == 2
    # Clarifying replies never advance.
    assert next_question_to_advance(_QS4, [], "what do you mean?") is None


# ── i18n floor (verbatim delivery can always fire) ───────────────────────────

@pytest.mark.asyncio
async def test_translate_floors_on_total_failure(monkeypatch):
    from app.services import questionnaire_translation as qt
    from app.services.llm_pool import LLMPoolClient

    async def boom(*a, **k):
        raise RuntimeError("pool down")

    monkeypatch.setattr(LLMPoolClient, "chat", staticmethod(boom))
    q = {"questions": [{"id": "a1", "text": "Are you interested?"}], "outro": "Thanks!", "threshold": 1}
    out = await qt.translate_questionnaire(q)
    assert out["questions"][0]["text_i18n"] == {"en": "Are you interested?"}
    assert out["outro_i18n"] == {"en": "Thanks!"}


@pytest.mark.asyncio
async def test_translate_floors_partial_rows(monkeypatch):
    from app.services import questionnaire_translation as qt
    from app.services.llm_pool import LLMPoolClient

    async def partial(*a, **k):
        return ('{"items":[{"id":"a1","en":"E","hi":"H","te":"T"},'
                '{"id":"b2","en":"E2","hi":"H2","te":""}]}')

    monkeypatch.setattr(LLMPoolClient, "chat", staticmethod(partial))
    q = {"questions": [{"id": "a1", "text": "Q one?"}, {"id": "b2", "text": "Q two?"}], "threshold": 1}
    out = await qt.translate_questionnaire(q)
    assert out["questions"][0]["text_i18n"] == {"en": "E", "hi": "H", "te": "T"}
    assert out["questions"][1]["text_i18n"] == {"en": "Q two?"}  # floored


# ── classifier timeout + default outro ───────────────────────────────────────

def test_digression_guard_uses_config_timeout_not_500():
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    src = inspect.getsource(NokvoOneVoicePipeline._llm_check_booking_digression)
    assert "timeout_ms=500" not in src
    assert "NOKVO_INTENT_CLASSIFIER_TIMEOUT_MS" in src


def test_default_questionnaire_outro_languages():
    from app.services.nokvo_one_voice_stream_service import _default_questionnaire_outro

    assert "Thank you" in _default_questionnaire_outro("en")
    assert _default_questionnaire_outro("hi-IN") != _default_questionnaire_outro("en")
    assert _default_questionnaire_outro("te") != _default_questionnaire_outro("en")
    assert "Thank you" in _default_questionnaire_outro(None)
    assert "Thank you" in _default_questionnaire_outro("fr")  # unknown → en
