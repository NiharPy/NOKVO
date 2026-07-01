"""APEX Phase 3 — verbatim per-language question delivery decision logic.

next_verbatim_question() decides whether to SPEAK the next question verbatim (clean
advance, and only when it's pre-translated) or defer to the LLM (re-ask, non-answer,
trailing-off, unanswered final intent, off-script, or no translation).
verbatim_line_for_language() picks the session-language string with fallback.
"""
from __future__ import annotations

from app.services.agent_outbound_context import (
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
