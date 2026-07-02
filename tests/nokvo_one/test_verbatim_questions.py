"""APEX Phase 3 — verbatim per-language question delivery decision logic.

next_verbatim_question() decides whether to SPEAK the next question verbatim (clean
advance, and only when it's pre-translated) or defer to the LLM (re-ask, non-answer,
trailing-off, unanswered final intent, off-script, or no translation).
verbatim_line_for_language() picks the session-language string with fallback.
"""
from __future__ import annotations

from app.services.agent_outbound_context import (
    gate_failed,
    next_verbatim_question,
    verbatim_line_for_language,
)

_I18N = {"en": "What is your budget?", "hi": "आपका बजट क्या है?", "te": "మీ బడ్జెట్ ఎంత?"}


def _qs():
    return [
        {"id": "q1", "type": "intent", "text": "Are you looking to buy?", "required": "yes",
         "text_i18n": {"en": "Are you looking to buy?", "hi": "क्या आप खरीदना चाहते हैं?", "te": "మీరు కొనాలనుకుంటున్నారా?"}},
        {"id": "q2", "type": "answer", "text": "What is your budget?", "desired_answer": "any", "text_i18n": _I18N},
    ]


def _hist(*assistant_lines):
    # A minimal history: alternating opener/answers; only assistant turns matter to
    # asked-tracking (matched by content-token overlap).
    h = [{"role": "assistant", "content": "Hi, quick call from Acme."}]
    for line in assistant_lines:
        h.append({"role": "user", "content": "ok"})
        h.append({"role": "assistant", "content": line})
    return h


def test_first_question_advances_after_opener():
    # Opener played, caller acknowledged — ask Q1 verbatim.
    plan = next_verbatim_question(_qs(), _hist(), "yeah sure")
    assert plan is not None and plan[0] == 1


def test_advances_to_next_after_q1_answered():
    plan = next_verbatim_question(_qs(), _hist("Are you looking to buy?"), "yes I am")
    assert plan is not None and plan[0] == 2 and plan[1]["id"] == "q2"


def test_off_script_defers_to_llm():
    # "who is this?" must NOT be answered by scripted playback.
    assert next_verbatim_question(_qs(), _hist(), "wait, who is this?") is None
    assert next_verbatim_question(_qs(), _hist(), "not interested") is None


def test_unanswered_final_intent_defers():
    # Both asked; last (only) intent-style question unanswered by a stray reply.
    qs = [{"id": "q1", "type": "intent", "text": "Are you interested?", "required": "yes",
           "text_i18n": {"en": "Are you interested?", "hi": "x", "te": "y"}}]
    hist = _hist("Are you interested?")
    # A reply with no yes/no cue → defer (LLM re-asks), not advance/close.
    assert next_verbatim_question(qs, hist, "the weather is nice today") is None


def test_no_translation_defers_to_llm():
    qs = [{"id": "q1", "type": "answer", "text": "What is your name?", "desired_answer": "any"}]  # no text_i18n
    assert next_verbatim_question(qs, _hist(), "ok go ahead") is None


def test_empty_questions_defers():
    assert next_verbatim_question([], _hist(), "hello") is None


def test_language_selection_with_fallback():
    assert verbatim_line_for_language(_I18N, "fallback", "hi") == _I18N["hi"]
    assert verbatim_line_for_language(_I18N, "fallback", "te-IN") == _I18N["te"]   # BCP-47 tag
    assert verbatim_line_for_language(_I18N, "fallback", "en-IN") == _I18N["en"]
    assert verbatim_line_for_language({"en": "only-en"}, "fallback", "hi") == "only-en"  # missing lang → en
    assert verbatim_line_for_language(None, "fallback", "hi") == "fallback"             # no i18n → authored


# ── Clarifying-question deferral (live counterpart of the scorer's answered=false) ──

def test_clarifying_question_defers_to_llm():
    qs = _qs()
    hist = _hist("Are you looking to buy?")  # Q1 asked; caller responds with a question
    for clarifier in (
        "where exactly is that?",
        "what do you mean?",
        "can you repeat that?",
        "sorry?",
        "ఎక్కడ ఉంది?",     # te "where is it?"
        "क्या मतलब?",       # hi "what do you mean?"
    ):
        assert next_verbatim_question(qs, hist, clarifier) is None, clarifier


def test_genuine_answer_resumes_after_clarifier():
    # After a clarifier defers, the NEXT real answer advances to Q2 verbatim.
    hist = _hist("Are you looking to buy?")
    plan = next_verbatim_question(_qs(), hist, "yes I am")
    assert plan is not None and plan[0] == 2 and plan[1]["id"] == "q2"


# ── Deterministic dealbreaker gate (gate_failed) ──

def _gate_qs():
    return [
        {"id": "g1", "type": "intent", "text": "Can you spare a few seconds for a survey?",
         "required": "yes", "gate": True},
        {"id": "q2", "type": "answer", "text": "What is your budget?", "desired_answer": "any"},
    ]


def test_gate_failed_on_clear_dealbreaker():
    hist = _hist("Can you spare a few seconds for a survey?")  # gate question asked
    assert gate_failed(_gate_qs(), hist, "no, I'm busy") is True
    assert gate_failed(_gate_qs(), hist, "nope") is True


def test_gate_not_failed_on_required_answer():
    hist = _hist("Can you spare a few seconds for a survey?")
    assert gate_failed(_gate_qs(), hist, "yes sure") is False  # required=yes → advance, not close


def test_gate_conservative_on_ambiguous_replies():
    # Ambiguous / sarcastic / non-committal / clarifying → must NOT hang up.
    hist = _hist("Can you spare a few seconds for a survey?")
    for reply in (
        "maybe",
        "hmm not sure",     # "sure"(affirm) + "not"(negate) cancel → ambiguous
        "why do you ask?",
        "yeah no",          # both cues → ambiguous
        "no yeah",
        "",                 # silence
        "the weather is nice",  # no yes/no cue at all
    ):
        assert gate_failed(_gate_qs(), hist, reply) is False, reply


def test_gate_ignores_non_gate_and_non_intent():
    # required="no" gate → the POSITIVE reply is the dealbreaker.
    no_gate = [{"id": "g1", "type": "intent", "text": "Are you on the do-not-call list?",
                "required": "no", "gate": True}]
    hist = _hist("Are you on the do-not-call list?")
    assert gate_failed(no_gate, hist, "yes I am") is True
    assert gate_failed(no_gate, hist, "no") is False
    # A non-gate intent never closes here.
    plain = [{"id": "g1", "type": "intent", "text": "Interested?", "required": "yes"}]
    assert gate_failed(plain, _hist("Interested?"), "no") is False
    # Empty questions.
    assert gate_failed([], _hist(), "no") is False
