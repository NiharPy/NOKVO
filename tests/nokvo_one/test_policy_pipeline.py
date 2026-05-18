"""Unit tests for the intent-first, policy-card-first decision flow.

These cover the ZapEats cancellation/refund matrix end-to-end at the
PolicyDecisionEngine + FastIntentRouter layer. They are pure-unit tests — no
DB, no Redis, no Qdrant, no Azure — so they can run in seconds against any
checkout.
"""

from __future__ import annotations

import asyncio
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
