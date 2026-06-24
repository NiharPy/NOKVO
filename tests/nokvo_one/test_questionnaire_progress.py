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
