"""Unit tests for the intent-first, policy-card-first decision flow.

These cover the ZapEats cancellation/refund matrix end-to-end at the
PolicyDecisionEngine + FastIntentRouter layer. They are pure-unit tests — no
DB, no Redis, no Qdrant, no Azure — so they can run in seconds against any
checkout.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.fast_intent_router import (
    INTENT_CANCELLATION_REQUEST,
    INTENT_GREETING,
    INTENT_REFUND_ELIGIBILITY,
    INTENT_UNKNOWN_GENERAL,
    FastIntentRouter,
)
from app.services.language_intent import detect_language_switch
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.policy_card_extractor import (
    OUTCOME_EIGHTY_PERCENT_REFUND,
    OUTCOME_FULL_REFUND_ORIGINAL,
    OUTCOME_FULL_REFUND_WALLET,
    OUTCOME_NO_CANCELLATION,
    PolicyCardExtractor,
    STATUS_ACCEPTED_NOT_PREPARING,
    STATUS_ANY,
    STATUS_NOT_ACCEPTED,
    STATUS_OUT_FOR_DELIVERY,
    STATUS_PREPARING,
    TOPIC_CANCELLATION,
)
from app.services.policy_decision_engine import (
    DEC_EXACT_MATCH,
    DEC_LIVE_STATUS_NEEDED,
    DEC_MATRIX_RESPONSE,
    PolicyDecisionEngine,
    extract_live_context_from_history,
)
from app.services.tool_flow_policy import evaluate_tool_flow_policy
from app.services.tool_flow_questions import ensure_tool_flow_questions
from app.services.voice_turn_policy import evaluate_voice_turn_policy, extract_turn_entities


# ── Fixtures ────────────────────────────────────────────────────────────────

ZAPEATS_POLICY_TEXT = """\
Cancellation Policy

Within 2 minutes of placing the order, the customer can cancel for a full refund to the original payment method.
Between 2 and 5 minutes, if the restaurant has not accepted the order, the customer can cancel for a full refund to the ZapEats Wallet.
Between 5 and 10 minutes, if the restaurant has accepted the order but has not started preparing it, only 80% is refundable. 20% is retained as a cancellation fee.
Once the order is marked Preparing, cancellation is not allowed.
Once the order is out for delivery, cancellation is not allowed.
"""


def _zapeats_cards() -> list[dict[str, Any]]:
    cards = PolicyCardExtractor.extract(ZAPEATS_POLICY_TEXT)
    # Stamp them as approved/active so the engine accepts them.
    for index, card in enumerate(cards):
        card["id"] = f"test:policy:{index}"
        card["approval_status"] = "approved"
        card["status"] = "active"
        card["policy_version"] = "pv_test"
    return cards


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


# ── 1) Cancellation answer without live context (the original failure) ──────

def test_cancellation_policy_without_live_context_must_not_say_simple_yes():
    cards = _zapeats_cards()
    decision = PolicyDecisionEngine.evaluate(
        intent=INTENT_REFUND_ELIGIBILITY,
        topic="cancellation",
        user_text="Can I get refunded after 5 minutes?",
        policy_cards=cards,
        live_context=None,
        current_policy_version="pv_test",
    )
    assert decision.answered is True
    assert decision.answer is not None
    text = decision.answer.lower()

    # Must not collapse to a generic "yes".
    assert not text.startswith("yes")
    # Must mention the 5–10 minute window.
    assert "5" in text and "10" in text
    # Must mention 80% refund.
    assert "80%" in text or "80 percent" in text
    # Must mention the 20% cancellation fee.
    assert "20%" in text or "20 percent" in text
    # Must mention the Preparing / Out for Delivery no-cancellation rule
    # OR ask for status — either is acceptable as long as it's not a flat "yes".
    assert ("preparing" in text and "out for delivery" in text) or "status" in text


# ── 2) Within 2 minutes → full refund to original payment ────────────────────

def test_within_2_minutes_full_refund_original_payment():
    cards = _zapeats_cards()
    decision = PolicyDecisionEngine.evaluate(
        intent=INTENT_CANCELLATION_REQUEST,
        topic="cancellation",
        user_text="Cancel my order",
        policy_cards=cards,
        live_context={"order_age_minutes": 1, "status": STATUS_ANY},
        current_policy_version="pv_test",
    )
    assert decision.answered is True
    assert decision.decision_code == DEC_EXACT_MATCH
    assert decision.matched_condition["outcome"] == OUTCOME_FULL_REFUND_ORIGINAL
    text = decision.answer.lower()
    assert "full refund" in text
    assert "original payment" in text


# ── 3) 2–5 minutes, restaurant not accepted → wallet refund ──────────────────

def test_2_to_5_minutes_not_accepted_wallet_refund():
    cards = _zapeats_cards()
    decision = PolicyDecisionEngine.evaluate(
        intent=INTENT_CANCELLATION_REQUEST,
        topic="cancellation",
        user_text="Cancel my order",
        policy_cards=cards,
        live_context={"order_age_minutes": 3, "status": STATUS_NOT_ACCEPTED},
        current_policy_version="pv_test",
    )
    assert decision.answered is True
    assert decision.decision_code == DEC_EXACT_MATCH
    assert decision.matched_condition["outcome"] == OUTCOME_FULL_REFUND_WALLET
    text = decision.answer.lower()
    assert "wallet" in text


# ── 4) 5–10 minutes, accepted-not-preparing → 80% refund + 20% fee ───────────

def test_5_to_10_minutes_accepted_not_preparing_partial_refund():
    cards = _zapeats_cards()
    decision = PolicyDecisionEngine.evaluate(
        intent=INTENT_CANCELLATION_REQUEST,
        topic="cancellation",
        user_text="Cancel my order",
        policy_cards=cards,
        live_context={"order_age_minutes": 6, "status": STATUS_ACCEPTED_NOT_PREPARING},
        current_policy_version="pv_test",
    )
    assert decision.answered is True
    assert decision.decision_code == DEC_EXACT_MATCH
    assert decision.matched_condition["outcome"] == OUTCOME_EIGHTY_PERCENT_REFUND
    text = decision.answer.lower()
    assert "80%" in text or "80 percent" in text
    assert "20%" in text or "20 percent" in text
    assert "cancellation fee" in text


# ── 5) Preparing → no cancellation ──────────────────────────────────────────

def test_preparing_no_cancellation():
    cards = _zapeats_cards()
    decision = PolicyDecisionEngine.evaluate(
        intent=INTENT_CANCELLATION_REQUEST,
        topic="cancellation",
        user_text="Cancel my order",
        policy_cards=cards,
        live_context={"order_age_minutes": 7, "status": STATUS_PREPARING},
        current_policy_version="pv_test",
    )
    assert decision.answered is True
    assert decision.matched_condition["outcome"] == OUTCOME_NO_CANCELLATION
    text = decision.answer.lower()
    assert "preparing" in text
    assert "not allowed" in text or "no cancellation" in text


# ── 6) Out for delivery → no cancellation + complaint-flow redirect ─────────

def test_out_for_delivery_no_cancellation():
    cards = _zapeats_cards()
    decision = PolicyDecisionEngine.evaluate(
        intent=INTENT_CANCELLATION_REQUEST,
        topic="cancellation",
        user_text="Cancel my order",
        policy_cards=cards,
        live_context={"order_age_minutes": 15, "status": STATUS_OUT_FOR_DELIVERY},
        current_policy_version="pv_test",
    )
    assert decision.answered is True
    assert decision.matched_condition["outcome"] == OUTCOME_NO_CANCELLATION
    text = decision.answer.lower()
    assert "out for delivery" in text
    assert "complaint" in text or "missing item" in text or "quality" in text


# ── 7) Greeting → fast template, no Qdrant / no LLM ─────────────────────────

def test_greeting_intent_router_returns_greeting():
    result = FastIntentRouter.classify("hi")
    assert result.intent == INTENT_GREETING
    assert result.sensitive is False
    assert result.requires_live_status is False
    # The voice pipeline uses this to short-circuit before any cache/Qdrant/LLM.
    assert result.confidence >= 0.9


# ── 8) Sensitive intent must not be cached ──────────────────────────────────

def test_sensitive_intent_is_flagged_for_no_cache():
    result = FastIntentRouter.classify("can I cancel my order?")
    assert result.intent == INTENT_CANCELLATION_REQUEST
    assert result.sensitive is True
    assert result.requires_live_status is True


def test_telugu_language_switch_is_detected():
    assert detect_language_switch("నేను తెలుగులో మాట్లాడదాం అనుకుంటున్నాను.") == "te"


def test_telugu_refund_phrase_routes_to_sensitive_policy():
    result = FastIntentRouter.classify(
        "ఆర్డర్ పెట్టిన 5 మినిట్స్కే క్యాన్సిల్ కూడా చేసేసాను, నాకు రీఫండ్ వస్తదా?",
        language="te",
    )
    assert result.intent == INTENT_REFUND_ELIGIBILITY
    assert result.sensitive is True
    assert result.requires_live_status is True


def test_telugu_stt_refund_mishearing_routes_to_policy():
    result = FastIntentRouter.classify("డిఫెండ్ వస్తదా?", language="te")
    assert result.intent == INTENT_REFUND_ELIGIBILITY
    assert result.sensitive is True


def test_telugu_minute_phrase_extracts_order_age():
    ctx = extract_live_context_from_history(
        [],
        current_user_text="ఆర్డర్ పెట్టిన 5 మినిట్స్కే క్యాన్సిల్ చేసాను",
    )
    assert ctx is not None
    assert ctx["order_age_minutes"] == 5


def test_telugu_clinic_location_rewrites_to_english_retrieval_query():
    query = NokvoOneVoicePipeline.retrieval_query_for("ఇది క్లినిక్ ఎక్కడ ఉందండి లొకేషనా?")
    assert "clinic" in query
    assert "location" in query
    assert NokvoOneVoicePipeline.should_skip_translate_for_native_query(
        "ఇది క్లినిక్ ఎక్కడ ఉందండి లొకేషనా?"
    ) is True


def test_telugu_short_location_rewrites_to_english_retrieval_query():
    query = NokvoOneVoicePipeline.retrieval_query_for("ఎన్నిక్ లొకేషన్ ఎక్కడ?")
    assert "location" in query
    assert "business" in query
    assert NokvoOneVoicePipeline.should_skip_translate_for_native_query("ఎన్నిక్ లొకేషన్ ఎక్కడ?") is True


# ── 9) RAG fallback path is preserved for general non-sensitive FAQs ────────

def test_general_faq_routes_to_rag_not_sensitive():
    result = FastIntentRouter.classify("what are your business hours")
    assert result.intent == INTENT_UNKNOWN_GENERAL
    assert result.sensitive is False
    assert result.requires_live_status is False


def test_bare_indian_mobile_number_is_not_treated_as_filler():
    result = evaluate_voice_turn_policy("7569672503", history=[], state={})
    assert result is not None
    assert result["intent"] == "phone_number_capture"
    assert result["entities"]["phone"] == "7569672503"
    assert "phone number" in result["answer"].lower()


def test_expected_order_number_is_not_reclassified_as_phone():
    history = [{"role": "assistant", "content": "What is your order number?"}]
    result = evaluate_voice_turn_policy("7569672503", history=history, state={})
    assert result is not None
    assert result["intent"] == "reference_number_capture"
    assert result["entities"]["reference_number"] == "7569672503"


def test_appointment_flow_collects_phone_and_next_slot():
    state = {"appointment": {"active": True, "pending_slot": "phone", "reason": "red eye", "patient_name": "Ravi"}}
    result = evaluate_voice_turn_policy("7569672503", history=[], state=state)
    assert result is not None
    assert result["intent"] == "appointment_flow"
    assert result["state_patch"]["appointment"]["phone"] == "7569672503"
    assert result["state_slot"] == "preferred_date"
    assert "date" in result["answer"].lower()


def test_appointment_flow_does_not_capture_reason_as_patient_name():
    state = {"appointment": {"active": True, "pending_slot": "patient_name", "reason": "eye checkup"}}
    result = evaluate_voice_turn_policy("It's an eye checkup actually", history=[], state=state)
    assert result is not None
    assert result["intent"] == "appointment_flow"
    assert result["state_slot"] == "patient_name"
    assert "patient_name" not in result["state_patch"]["appointment"]
    assert "name" in result["answer"].lower()


def test_appointment_flow_corrects_misheard_reason_and_keeps_language():
    state = {"appointment": {"active": True, "pending_slot": "patient_name", "reason": "Mental checkup"}}
    result = evaluate_voice_turn_policy(
        "General checkup, it's not mental checkup",
        history=[],
        state=state,
        language="te",
    )
    assert result is not None
    assert result["intent"] == "appointment_flow"
    assert result["state_slot"] == "patient_name"
    assert result["state_patch"]["appointment"]["reason"] == "General checkup"
    assert result["entities"]["corrected_reason"] == "General checkup"
    assert "patient_name" not in result["state_patch"]["appointment"]
    assert "Patient full name" in result["answer"]


def test_telugu_appointment_request_enters_fast_slot_flow():
    result = evaluate_voice_turn_policy("నాకు ఒక అపాయింట్మెంట్ కావాలి.", history=[], state={}, language="te")

    assert result is not None
    assert result["intent"] == "appointment_flow"
    assert result["state_slot"] == "reason"
    assert "Visit reason" in result["answer"]


def test_telugu_appointment_flow_collects_reason_name_phone_and_no_urgent():
    state = {"appointment": {"active": True, "pending_slot": "reason"}}
    reason = evaluate_voice_turn_policy("జనరల్ చెకప్ అండి.", history=[], state=state, language="te")
    assert reason is not None
    assert reason["state_patch"]["appointment"]["reason"] == "జనరల్ చెకప్"
    assert reason["state_slot"] == "patient_name"
    assert "Patient full name" in reason["answer"]

    name = evaluate_voice_turn_policy(
        "నా పేరు నిహార్.",
        history=[],
        state=reason["state_patch"],
        language="te",
    )
    assert name is not None
    assert name["state_patch"]["appointment"]["patient_name"] == "నిహార్"
    assert name["state_slot"] == "phone"
    assert "phone number" in name["answer"]

    phone = evaluate_voice_turn_policy("7569672503", history=[], state=name["state_patch"], language="te")
    assert phone is not None
    assert phone["state_patch"]["appointment"]["phone"] == "7569672503"
    assert phone["state_slot"] == "preferred_date"
    assert "Preferred date" in phone["answer"]

    urgent_state = {
        "appointment": {
            "active": True,
            "pending_slot": "urgent_symptoms",
            "reason": "జనరల్ చెకప్",
            "patient_name": "నిహార్",
            "phone": "7569672503",
            "preferred_date": "20th May",
            "preferred_time": "12 PM",
            "visit_type": "first_visit",
        }
    }
    urgent = evaluate_voice_turn_policy("లేదు అలాంటివి ఏం లేవు.", history=[], state=urgent_state, language="te")
    assert urgent is not None
    assert urgent["state_slot"] == "complete"
    assert urgent["state_patch"]["appointment"]["urgent_symptoms"] == "none_reported"


def test_first_turn_pivot_before_booking_yields_to_rag():
    """Caller's FIRST message expresses an intent to book AND asks a KB
    question first. The FSM must NOT start collecting slots — it must yield
    to RAG so the question gets answered."""
    result = evaluate_voice_turn_policy(
        "Uh, before I book an appointment, can you just tell me about all the services offered by your clinic?",
        history=[],
        state={},
        language="en",
    )
    assert result is None


def test_first_turn_but_first_pivot_yields_to_rag():
    result = evaluate_voice_turn_policy(
        "I want to book an appointment but first, what are your timings?",
        history=[],
        state={},
        language="en",
    )
    assert result is None


def test_first_turn_prior_to_booking_pivot_yields_to_rag():
    result = evaluate_voice_turn_policy(
        "Prior to booking, could you walk me through the consultation fees?",
        history=[],
        state={},
        language="en",
    )
    assert result is None


def test_first_turn_plain_booking_request_still_enters_flow():
    """Negative case — a normal first-turn booking request without a pivot must
    still enter the slot-fill FSM and ask for the reason."""
    result = evaluate_voice_turn_policy(
        "I would like to book an appointment.",
        history=[],
        state={},
        language="en",
    )
    assert result is not None
    assert result["intent"] == "appointment_flow"
    assert result["state_slot"] == "reason"


def test_side_question_during_booking_yields_to_rag():
    state = {"appointment": {"active": True, "pending_slot": "reason"}}
    history = [
        {"role": "user", "content": "I would like to book an appointment."},
        {"role": "assistant", "content": "What eye concern or reason should I note for the visit?"},
    ]
    result = evaluate_voice_turn_policy(
        "Before that, could you just list out all the services offered by your clinic?",
        history=history,
        state=state,
        language="en",
    )
    assert result is None


def test_side_question_pricing_during_booking_yields_to_rag():
    state = {"appointment": {"active": True, "pending_slot": "patient_name", "reason": "red eye"}}
    result = evaluate_voice_turn_policy(
        "Actually first, what's the consultation fee?",
        history=[],
        state=state,
        language="en",
    )
    assert result is None


def test_appointment_request_with_date_still_enters_flow():
    """Question-shaped sentences that carry slot data must NOT yield to RAG."""
    result = evaluate_voice_turn_policy(
        "Can I get an appointment tomorrow?",
        history=[],
        state={},
        language="en",
    )
    assert result is not None
    assert result["intent"] == "appointment_flow"


def test_booking_resumes_after_side_question_returns_pending_slot():
    """After yielding for a KB question, the next slot answer still fills correctly."""
    state = {"appointment": {"active": True, "pending_slot": "reason"}}
    result = evaluate_voice_turn_policy(
        "I have severe redness in my left eye for two days",
        history=[],
        state=state,
        language="en",
    )
    assert result is not None
    assert result["intent"] == "appointment_flow"
    assert result["state_patch"]["appointment"]["reason"]
    assert result["state_slot"] == "patient_name"


def test_cancellation_mid_booking_yields_to_rag():
    """Caller pivots mid-booking to a cancellation request → yield to RAG so
    the sensitive-policy handler can take over."""
    state = {"appointment": {"active": True, "pending_slot": "patient_name", "reason": "red eye"}}
    result = evaluate_voice_turn_policy(
        "Actually wait, I want to cancel my previous order first",
        history=[],
        state=state,
        language="en",
    )
    assert result is None


def test_refund_mid_booking_yields_to_rag():
    state = {"appointment": {"active": True, "pending_slot": "phone", "reason": "eye checkup", "patient_name": "Ravi"}}
    result = evaluate_voice_turn_policy(
        "Can I get a refund on my last appointment before I book again?",
        history=[],
        state=state,
        language="en",
    )
    assert result is None


def test_resumed_booking_prefixes_with_coming_back():
    """When the FSM resumes after a digression (deferred_for_kb=True), the
    next slot question is prefixed with 'Coming back to your booking — ' and
    the flag is cleared from the state patch."""
    state = {
        "appointment": {
            "active": True,
            "pending_slot": "reason",
            "deferred_for_kb": True,
        }
    }
    # User gives a vague non-reason; FSM will re-ask the same slot, but the
    # answer should now be prefixed because deferred_for_kb was set.
    result = evaluate_voice_turn_policy(
        "okay",
        history=[],
        state=state,
        language="en",
    )
    assert result is not None
    assert result["intent"] == "appointment_flow"
    assert result["answer"].startswith("Coming back to your booking — ")
    # Flag is consumed (cleared) so future turns don't double-prefix.
    assert "deferred_for_kb" not in result["state_patch"]["appointment"]


def test_resumed_booking_prefix_is_localized():
    state = {
        "appointment": {
            "active": True,
            "pending_slot": "reason",
            "deferred_for_kb": True,
        }
    }
    result = evaluate_voice_turn_policy(
        "ok",
        history=[],
        state=state,
        language="hi",
    )
    assert result is not None
    assert "बुकिंग पर वापस आते हैं" in result["answer"]


def test_resumed_booking_does_not_prefix_when_flag_absent():
    """Normal next-slot question (no prior digression) must NOT have the prefix."""
    state = {"appointment": {"active": True, "pending_slot": "reason"}}
    result = evaluate_voice_turn_policy(
        "okay",
        history=[],
        state=state,
        language="en",
    )
    assert result is not None
    assert not result["answer"].startswith("Coming back to your booking")


def test_mark_appointment_deferred_writes_state_patch(monkeypatch):
    """`_mark_appointment_deferred` merges deferred_for_kb=True into appointment state."""
    captured: list[dict[str, Any]] = []

    async def fake_merge_state(tenant_res, call_id, patch):
        captured.append({"call_id": call_id, "patch": patch})

    monkeypatch.setattr(
        "app.services.agent_session_store.AgentSessionStore.merge_state",
        fake_merge_state,
    )
    tenant_res = SimpleNamespace(tenant_id="t-1", provider_status={})
    prior_appointment = {"active": True, "pending_slot": "reason", "reason": None}

    _run(
        NokvoOneVoicePipeline._mark_appointment_deferred(
            tenant_res, "call-1", prior_appointment
        )
    )

    assert len(captured) == 1
    assert captured[0]["call_id"] == "call-1"
    assert captured[0]["patch"]["appointment"]["deferred_for_kb"] is True
    # Other fields preserved.
    assert captured[0]["patch"]["appointment"]["pending_slot"] == "reason"
    assert captured[0]["patch"]["appointment"]["active"] is True


def test_mark_appointment_deferred_noops_when_not_active(monkeypatch):
    """Skip the merge when there's no active booking to defer."""
    captured: list[dict[str, Any]] = []

    async def fake_merge_state(tenant_res, call_id, patch):
        captured.append(patch)

    monkeypatch.setattr(
        "app.services.agent_session_store.AgentSessionStore.merge_state",
        fake_merge_state,
    )
    tenant_res = SimpleNamespace(tenant_id="t-1", provider_status={})

    _run(NokvoOneVoicePipeline._mark_appointment_deferred(tenant_res, "call-1", {}))
    _run(
        NokvoOneVoicePipeline._mark_appointment_deferred(
            tenant_res, None, {"active": True}
        )
    )

    assert captured == []


def test_llm_digression_check_returns_classification_for_kb_question(monkeypatch):
    """When the classifier flags a KB question, _llm_check_booking_digression
    returns the ClassifiedIntent so the caller can bypass the FSM."""
    from app.services.llm_intent_classifier import ClassifiedIntent

    async def fake_classify(text, *, tenant_res, history, timeout_ms=800):
        return ClassifiedIntent(
            intent="kb_question",
            needs_kb=True,
            sensitive=False,
            sentiment="neutral",
            reason="services question",
        )

    monkeypatch.setattr(
        "app.services.llm_intent_classifier.LLMIntentClassifier.classify",
        fake_classify,
    )
    tenant_res = SimpleNamespace(tenant_id="t-1", provider_status={})

    result = _run(
        NokvoOneVoicePipeline._llm_check_booking_digression(
            tenant_res, "what about your covid protocol", []
        )
    )
    assert result is not None
    assert result.intent == "kb_question"


def test_llm_digression_check_promotes_cancellation(monkeypatch):
    from app.services.llm_intent_classifier import ClassifiedIntent

    async def fake_classify(text, *, tenant_res, history, timeout_ms=800):
        return ClassifiedIntent(
            intent="cancellation_request",
            needs_kb=False,
            sensitive=True,
            sentiment="negative",
            reason="cancel request mid-booking",
        )

    monkeypatch.setattr(
        "app.services.llm_intent_classifier.LLMIntentClassifier.classify",
        fake_classify,
    )
    tenant_res = SimpleNamespace(tenant_id="t-1", provider_status={})

    result = _run(
        NokvoOneVoicePipeline._llm_check_booking_digression(
            tenant_res, "scrap that I want to undo my last order", []
        )
    )
    assert result is not None
    assert result.intent == "cancellation_request"


def test_llm_digression_check_returns_none_for_slot_like_answers(monkeypatch):
    """Classifier saying smalltalk/unclear/order_status must NOT bypass the FSM —
    those are signals that the caller might still be answering the slot."""
    from app.services.llm_intent_classifier import ClassifiedIntent

    for intent_str in ("smalltalk", "unclear", "order_status"):
        async def fake_classify(text, *, tenant_res, history, timeout_ms=800, _i=intent_str):
            return ClassifiedIntent(
                intent=_i,
                needs_kb=False,
                sensitive=False,
                sentiment="neutral",
                reason="test",
            )

        monkeypatch.setattr(
            "app.services.llm_intent_classifier.LLMIntentClassifier.classify",
            fake_classify,
        )
        tenant_res = SimpleNamespace(tenant_id="t-1", provider_status={})
        result = _run(
            NokvoOneVoicePipeline._llm_check_booking_digression(
                tenant_res, "yeah okay", []
            )
        )
        assert result is None, f"Should not bypass FSM for intent={intent_str}"


def test_llm_digression_check_returns_none_on_classifier_fallback(monkeypatch):
    """When the classifier itself fell back (timeout / error), don't bypass FSM."""
    from app.services.llm_intent_classifier import ClassifiedIntent

    async def fake_classify(text, *, tenant_res, history, timeout_ms=800):
        return ClassifiedIntent(
            intent="kb_question",
            needs_kb=True,
            sensitive=False,
            sentiment="neutral",
            reason="timeout",
            fallback=True,
        )

    monkeypatch.setattr(
        "app.services.llm_intent_classifier.LLMIntentClassifier.classify",
        fake_classify,
    )
    tenant_res = SimpleNamespace(tenant_id="t-1", provider_status={})

    result = _run(
        NokvoOneVoicePipeline._llm_check_booking_digression(
            tenant_res, "anything", []
        )
    )
    assert result is None


def test_llm_digression_check_swallows_classifier_exception(monkeypatch):
    """A raising classifier must not crash the route."""

    async def fake_classify(text, *, tenant_res, history, timeout_ms=800):
        raise RuntimeError("network down")

    monkeypatch.setattr(
        "app.services.llm_intent_classifier.LLMIntentClassifier.classify",
        fake_classify,
    )
    tenant_res = SimpleNamespace(tenant_id="t-1", provider_status={})

    result = _run(
        NokvoOneVoicePipeline._llm_check_booking_digression(
            tenant_res, "anything", []
        )
    )
    assert result is None


def test_completed_appointment_does_not_hijack_general_clinic_question():
    state = {
        "appointment": {
            "active": True,
            "completed": True,
            "pending_slot": None,
            "reason": "General Eye Checkup",
            "patient_name": "Nihar Neelala",
            "phone": "7569672503",
            "preferred_date": "20th May",
            "preferred_time": "1 PM",
            "visit_type": "first_visit",
            "urgent_symptoms": "none_reported",
        }
    }
    history = [
        {
            "role": "assistant",
            "content": (
                "Thanks, I have noted patient Nihar Neelala, phone 7569672503, "
                "reason General Eye Checkup, preferred time 20th May 1 PM. "
                "The clinic team can confirm exact availability."
            ),
        }
    ]

    result = evaluate_voice_turn_policy(
        "Can you tell me all the services offered by your clinic?",
        history=history,
        state=state,
        language="en",
    )

    assert result is None


def test_tool_flow_questions_regenerate_from_lead_schema():
    provider_status = {
        "business_template_schema_overrides": {
            "leads": [
                {"key": "name", "label": "Customer Name", "type": "text", "required": True},
                {"key": "phone", "label": "Phone", "type": "phone", "required": True},
                {"key": "preferred_location", "label": "Preferred Location", "type": "text", "required": True},
            ]
        }
    }

    updated, changed = ensure_tool_flow_questions(provider_status, "real_estate")
    assert changed is True
    questions = updated["tool_flow_questions"]
    assert questions["flows"]["leads_create"]["slots"][2]["key"] == "preferred_location"
    assert "location" in questions["flows"]["leads_create"]["slots"][2]["questions"]["en"].lower()

    updated_again, changed_again = ensure_tool_flow_questions(updated, "real_estate")
    assert changed_again is False
    assert updated_again["tool_flow_questions"]["schema_hash"] == questions["schema_hash"]

    updated["business_template_schema_overrides"]["leads"].append(
        {"key": "budget", "label": "Budget", "type": "currency", "required": True}
    )
    regenerated, changed_third = ensure_tool_flow_questions(updated, "real_estate")
    assert changed_third is True
    slots = [slot["key"] for slot in regenerated["tool_flow_questions"]["flows"]["leads_create"]["slots"]]
    assert "budget" in slots


def test_real_estate_visit_flow_uses_schema_questions():
    provider_status, _ = ensure_tool_flow_questions({}, "real_estate")
    result = evaluate_tool_flow_policy(
        "I want to visit the property",
        business_type="real_estate",
        provider_status=provider_status,
        history=[],
        state={},
        language="te",
    )

    assert result is not None
    assert result["intent"] == "tool_flow"
    assert result["flow_key"] == "real_estate_site_visit"
    assert result["state_slot"] == "name"
    assert "name" in result["answer"]


def test_tool_flow_yields_when_caller_asks_side_question_mid_flow():
    """Mid-flow KB question must NOT be consumed as a slot value."""
    provider_status, _ = ensure_tool_flow_questions({}, "real_estate")
    state = {
        "tool_flow": {
            "active": True,
            "flow_key": "real_estate_site_visit",
            "tool_key": "qualify_lead_and_schedule_visit",
            "collected": {},
            "pending_slot": "name",
        }
    }
    result = evaluate_tool_flow_policy(
        "Before I book, can you tell me about the services you offer?",
        business_type="real_estate",
        provider_status=provider_status,
        history=[{"role": "assistant", "content": "May I have your name?"}],
        state=state,
        language="en",
    )
    assert result is None


def test_tool_flow_rejects_question_text_as_name_slot():
    """A question-shaped utterance must not get stored as a name."""
    provider_status, _ = ensure_tool_flow_questions({}, "real_estate")
    state = {
        "tool_flow": {
            "active": True,
            "flow_key": "real_estate_site_visit",
            "tool_key": "qualify_lead_and_schedule_visit",
            "collected": {},
            "pending_slot": "name",
        }
    }
    result = evaluate_tool_flow_policy(
        "Is it possible for you to explain me all the services offered by your clinic?",
        business_type="real_estate",
        provider_status=provider_status,
        history=[{"role": "assistant", "content": "May I have your name?"}],
        state=state,
        language="en",
    )
    # Either yields to RAG (None) or re-asks the same name slot without
    # capturing the question text into collected.name.
    if result is not None:
        assert result["state_slot"] == "name"
        collected = ((result.get("state_patch") or {}).get("tool_flow") or {}).get("collected") or {}
        assert "name" not in collected


def test_tool_flow_resumes_with_coming_back_prefix_after_kb_digression():
    """When deferred_for_kb is set, the next slot question gets the prefix."""
    provider_status, _ = ensure_tool_flow_questions({}, "real_estate")
    state = {
        "tool_flow": {
            "active": True,
            "flow_key": "real_estate_site_visit",
            "tool_key": "qualify_lead_and_schedule_visit",
            "collected": {},
            "pending_slot": "name",
            "deferred_for_kb": True,
        }
    }
    result = evaluate_tool_flow_policy(
        "My name is Rohan",
        business_type="real_estate",
        provider_status=provider_status,
        history=[{"role": "assistant", "content": "May I have your name?"}],
        state=state,
        language="en",
    )
    assert result is not None
    assert "Coming back to your booking" in result["answer"]
    flow_state = ((result.get("state_patch") or {}).get("tool_flow") or {})
    assert flow_state.get("collected", {}).get("name") == "Rohan"
    # Marker consumed.
    assert "deferred_for_kb" not in flow_state


def test_kb_topic_alone_yields_during_booking_even_without_question_word():
    """The legacy elif branch used to consume 'Before I book an appointment,
    is it possible for me to know all the services your clinic provides?' as
    the visit reason because the input contains 'appointment' and 'for'. The
    KB topic ('services') must override that consumption.
    """
    state = {"appointment": {"active": True, "pending_slot": "reason"}}
    hist = [{"role": "assistant", "content": "What eye concern or reason should I note for the visit?"}]
    for text in [
        "Before I book an appointment, is it possible for me to know all the services your clinic provides?",
        "Is it possible for me to know all the services your clinic provides",
        "list all the services you provide",
        "what doctors do you have",
        "how much are the fees",
    ]:
        result = evaluate_voice_turn_policy(text, history=hist, state=state, language="en")
        assert result is None, f"FSM consumed side question as slot value for: {text!r}"


def test_legitimate_reason_answers_still_advance_the_fsm():
    state = {"appointment": {"active": True, "pending_slot": "reason"}}
    hist = [{"role": "assistant", "content": "What eye concern or reason should I note for the visit?"}]
    for text in ["red eyes and itching", "eye pain for two days", "blurred vision"]:
        result = evaluate_voice_turn_policy(text, history=hist, state=state, language="en")
        assert result is not None, f"Legit reason wrongly dropped: {text!r}"
        assert result["state_slot"] == "patient_name"


def test_generic_appointment_request_asks_reason_first():
    result = evaluate_voice_turn_policy("I want an appointment", history=[], state={})
    assert result is not None
    assert result["intent"] == "appointment_flow"
    assert result["state_slot"] == "reason"
    assert "reason" in result["answer"].lower()


def test_urgent_eye_symptom_escalates_without_rag():
    result = evaluate_voice_turn_policy("I have sudden vision loss", history=[], state={})
    assert result is not None
    assert result["intent"] == "urgent_symptom"
    assert "urgent medical attention" in result["answer"].lower()


def test_phone_entity_accepts_plus_91_format():
    entities = extract_turn_entities("+91 75696 72503")
    assert entities["phone"] == "+917569672503"


def test_voice_router_routes_bare_phone_before_short_filler(monkeypatch):
    async def fake_history(*args, **kwargs):
        return []

    async def fake_state(*args, **kwargs):
        return {}

    monkeypatch.setattr("app.services.agent_session_store.AgentSessionStore.get_history", fake_history)
    monkeypatch.setattr("app.services.agent_session_store.AgentSessionStore.get_state", fake_state)
    tenant_res = SimpleNamespace(tenant_id="tenant-test", provider_status={})
    route = _run(
        NokvoOneVoicePipeline._route_turn(
            tenant_res,
            "7569672503",
            language="en",
            company_name="Test Clinic",
            call_id="call-1",
        )
    )
    assert route["route"] == "template"
    assert route["route_reason"] == "bare phone number needs confirmation"
    assert "phone number" in route["answer"].lower()
    assert "go on" not in route["answer"].lower()


def test_voice_appointment_completion_executes_create_tool(monkeypatch):
    org_id = uuid.uuid4()
    calls: list[dict[str, Any]] = []

    async def fake_history(*args, **kwargs):
        return []

    async def fake_state(*args, **kwargs):
        return {
            "appointment": {
                "active": True,
                "pending_slot": "urgent_symptoms",
                "reason": "red eye",
                "patient_name": "Ravi Kumar",
                "phone": "7569672503",
                "preferred_date": "tomorrow",
                "preferred_time": "5 pm",
                "visit_type": "first_visit",
            }
        }

    async def fake_context(db, tenant_res):
        return SimpleNamespace(industry="clinics"), {}, []

    async def fake_execute(db, organization_id, user_id, tool, arguments, **kwargs):
        calls.append({"tool": tool.key, "organization_id": organization_id, "arguments": arguments, "kwargs": kwargs})
        return {
            "ok": True,
            "id": str(uuid.uuid4()),
            "record_type": "appointment",
            "status": "assigned",
            "assignment_status": "assigned",
            "assigned_member_name": "Dr Rao",
        }

    class FakeDB:
        committed = False
        rolled_back = False

        async def commit(self):
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr("app.services.agent_session_store.AgentSessionStore.get_history", fake_history)
    monkeypatch.setattr("app.services.agent_session_store.AgentSessionStore.get_state", fake_state)
    monkeypatch.setattr(NokvoOneVoicePipeline, "_voice_business_context", staticmethod(fake_context))
    monkeypatch.setattr("app.services.nokvo_one_voice_pipeline.PredefinedToolsService.execute", fake_execute)

    db = FakeDB()
    tenant_res = SimpleNamespace(tenant_id="tenant-test", organization_id=org_id, provider_status={})
    route = _run(
        NokvoOneVoicePipeline._route_turn(
            tenant_res,
            "no urgent symptoms",
            language="en",
            company_name="Test Clinic",
            call_id="call-appointment",
            db=db,
        )
    )

    assert route["route"] == "template"
    assert db.committed is True
    assert calls and calls[0]["tool"] == "appointments_create"
    assert calls[0]["organization_id"] == org_id
    assert calls[0]["arguments"]["patient_name"] == "Ravi Kumar"
    assert calls[0]["arguments"]["phone"] == "7569672503"
    assert calls[0]["arguments"]["reason"] == "red eye"
    assert calls[0]["arguments"]["appointment_time"].endswith("+00:00")
    assert route["tool_calls"][0]["result"]["assignment_status"] == "assigned"
    assert route["state_patch"]["appointment"]["created_record_id"]
    assert "assigned to Dr Rao" in route["answer"]


def test_voice_real_estate_visit_flow_executes_macro(monkeypatch):
    org_id = uuid.uuid4()
    calls: list[dict[str, Any]] = []

    async def fake_history(*args, **kwargs):
        return []

    async def fake_state(*args, **kwargs):
        return {
            "tool_flow": {
                "active": True,
                "flow_key": "real_estate_site_visit",
                "pending_slot": "visit_time",
                "collected": {
                    "name": "Nihar",
                    "phone": "7569672503",
                    "visit_date": "tomorrow",
                },
            }
        }

    async def fake_context(db, tenant_res):
        return SimpleNamespace(industry="real_estate"), {}, []

    async def fake_execute(db, organization_id, user_id, tool, arguments, **kwargs):
        calls.append({"tool": tool.key, "organization_id": organization_id, "arguments": arguments, "kwargs": kwargs})
        return {
            "ok": True,
            "lead_id": str(uuid.uuid4()),
            "callback_id": str(uuid.uuid4()),
            "assignment_status": "assigned",
            "assigned_member_name": "Rahul",
        }

    class FakeDB:
        committed = False
        rolled_back = False

        async def commit(self):
            self.committed = True

        async def rollback(self):
            self.rolled_back = True

    monkeypatch.setattr("app.services.agent_session_store.AgentSessionStore.get_history", fake_history)
    monkeypatch.setattr("app.services.agent_session_store.AgentSessionStore.get_state", fake_state)
    monkeypatch.setattr(NokvoOneVoicePipeline, "_voice_business_context", staticmethod(fake_context))
    monkeypatch.setattr("app.services.nokvo_one_voice_pipeline.PredefinedToolsService.execute", fake_execute)

    db = FakeDB()
    tenant_res = SimpleNamespace(tenant_id="tenant-test", organization_id=org_id, provider_status={})
    route = _run(
        NokvoOneVoicePipeline._route_turn(
            tenant_res,
            "5 PM",
            language="te",
            company_name="Test Realty",
            call_id="call-real-estate",
            db=db,
        )
    )

    assert route["route"] == "template"
    assert db.committed is True
    assert calls and calls[0]["tool"] == "qualify_lead_and_schedule_visit"
    assert calls[0]["arguments"]["name"] == "Nihar"
    assert calls[0]["arguments"]["phone"] == "7569672503"
    assert calls[0]["arguments"]["visit_at"].endswith("+00:00")
    assert route["tool_calls"][0]["result"]["assigned_member_name"] == "Rahul"
    assert "Site visit request create" in route["answer"]
    assert "Rahul" in route["answer"]


# ── 10) (Bonus) Extractor produces structured conditions for the matrix ─────

def test_history_extractor_picks_up_order_age():
    history = [
        {"role": "assistant", "content": "How long ago did you place the order?"},
        {"role": "user", "content": "I placed it about 3 minutes ago"},
        {"role": "assistant", "content": "Got it."},
    ]
    ctx = extract_live_context_from_history(history)
    assert ctx is not None
    assert ctx["order_age_minutes"] == 3


def test_history_extractor_picks_up_status():
    history = [
        {"role": "user", "content": "the restaurant accepted it but hasn't started preparing"},
    ]
    ctx = extract_live_context_from_history(history)
    assert ctx is not None
    assert ctx["status"] == "accepted_not_preparing"


def test_history_extractor_returns_none_when_nothing_present():
    history = [
        {"role": "user", "content": "Hi, I need help with my order"},
        {"role": "assistant", "content": "Of course. How can I help?"},
    ]
    assert extract_live_context_from_history(history) is None


def test_multi_turn_cancellation_uses_history_context():
    """The user gave their order age in turn 1; in turn 2 they ask to cancel.
    The decision engine must use the prior context, not re-prompt."""
    cards = _zapeats_cards()
    history = [
        {"role": "assistant", "content": "How long ago did you place it?"},
        {"role": "user", "content": "About 1 minute ago"},
    ]
    live_ctx = extract_live_context_from_history(history)
    assert live_ctx is not None
    decision = PolicyDecisionEngine.evaluate(
        intent=INTENT_CANCELLATION_REQUEST,
        topic="cancellation",
        user_text="Then please cancel it",
        policy_cards=cards,
        live_context=live_ctx,
        current_policy_version="pv_test",
    )
    assert decision.answered is True
    assert decision.decision_code == DEC_EXACT_MATCH
    assert decision.matched_condition["outcome"] == "full_refund_original_payment"


def test_extractor_resolves_user_after_5_minutes_accepted_phrasing():
    """The real transcript: user said 'after 5 minutes I cancelled' and
    earlier 'restaurant accepted it'. Should resolve to the 5-10min /
    accepted_not_preparing row of the matrix, NOT 'no_cancellation'."""
    history = [
        {"role": "user", "content": "I'm asking you whether I can get a refund."},
        {"role": "assistant", "content": "I need to check your order status first."},
        {"role": "user", "content": "then Accept accepted it and then my order was is placed"},
        {"role": "assistant", "content": "Got it, your order was accepted by the restaurant."},
    ]
    current = "And for 5 minutes I didn't do anything, after 5 minutes I cancelled the order. So am I eligible for a refund?"
    ctx = extract_live_context_from_history(history, current_user_text=current)
    assert ctx is not None
    assert ctx["order_age_minutes"] == 5
    assert ctx["status"] == "accepted_not_preparing"

    cards = _zapeats_cards()
    decision = PolicyDecisionEngine.evaluate(
        intent=INTENT_REFUND_ELIGIBILITY,
        topic="cancellation",
        user_text=current,
        policy_cards=cards,
        live_context=ctx,
        current_policy_version="pv_test",
    )
    assert decision.answered is True
    assert decision.decision_code == DEC_EXACT_MATCH
    assert decision.matched_condition["outcome"] == "eighty_percent_refund"
    assert "80%" in decision.answer
    assert "20%" in decision.answer


def test_history_extractor_out_for_delivery():
    history = [
        {"role": "user", "content": "the rider already left, it's on the way"},
    ]
    ctx = extract_live_context_from_history(history)
    assert ctx is not None
    assert ctx["status"] == "out_for_delivery"


def test_wildcard_catch_all_does_not_match_partial_context():
    """A poorly-extracted condition with no window AND status=ANY must not
    match an age-only live_context. Otherwise it gives canned wrong answers
    like 'Cancellation is not allowed at this stage' for everything."""
    cards = [
        {
            "id": "test:bad_card",
            "approval_status": "approved",
            "status": "active",
            "policy_version": "pv_test",
            "card_type": "policy_rule",
            "topic": "cancellation",
            "intent": "customer_cancel_order",
            "conditions": [
                {
                    "order_age_minutes_min": None,
                    "order_age_minutes_max": None,
                    "status": "any",
                    "outcome": "no_cancellation",
                    "customer_message": "Cancellation is not allowed at this stage.",
                }
            ],
        },
    ]
    decision = PolicyDecisionEngine.evaluate(
        intent=INTENT_CANCELLATION_REQUEST,
        topic="cancellation",
        user_text="I cancelled within 5 minutes, can I get a refund?",
        policy_cards=cards,
        live_context={"order_age_minutes": 5},
        current_policy_version="pv_test",
    )
    # Should NOT return DEC_EXACT_MATCH with the catch-all condition.
    assert decision.decision_code != DEC_EXACT_MATCH


def test_extractor_produces_full_zapeats_matrix():
    cards = PolicyCardExtractor.extract(ZAPEATS_POLICY_TEXT)
    cancellation_cards = [c for c in cards if c["topic"] == TOPIC_CANCELLATION]
    assert cancellation_cards, "expected at least one cancellation policy card"
    outcomes = {
        cond["outcome"]
        for card in cancellation_cards
        for cond in card.get("conditions") or []
    }
    # Every leg of the matrix should appear.
    assert OUTCOME_FULL_REFUND_ORIGINAL in outcomes
    assert OUTCOME_FULL_REFUND_WALLET in outcomes
    assert OUTCOME_EIGHTY_PERCENT_REFUND in outcomes
    assert OUTCOME_NO_CANCELLATION in outcomes
