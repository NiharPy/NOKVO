"""Tests for the Agent Intent Classification Service.

Verifies the dual-brain architecture:
    1. Conversation Brain — greetings, small talk → NO retrieval
    2. Knowledge Brain — policy/support questions → retrieval required
    3. Ambiguous messages → clarification, NO retrieval
"""

import pytest
import uuid

from app.models.tenant_resources import TenantResources
from app.services.agent_intent_service import (
    INTENT_GREETING,
    INTENT_SUPPORT,
    INTENT_ORDER_ACTION,
    INTENT_AMBIGUOUS,
    classify_intent_fast,
    detect_language,
    normalize_message,
    generate_conversation_reply,
    validate_chunk_relevance,
)
from app.services.agent_runtime_service import AgentRuntimeService


# ---------------------------------------------------------------------------
# Test: normalize_message
# ---------------------------------------------------------------------------

class TestNormalizeMessage:
    def test_strips_whitespace(self):
        assert normalize_message("  hello  ") == "hello"

    def test_collapses_spaces(self):
        assert normalize_message("hello   world") == "hello world"

    def test_strips_stt_control_tokens(self):
        assert normalize_message("Hello, <end>") == "Hello,"
        assert normalize_message("hi [speech_final]") == "hi"

    def test_handles_none(self):
        assert normalize_message(None) == ""

    def test_handles_empty(self):
        assert normalize_message("") == ""


# ---------------------------------------------------------------------------
# Test: No retrieval — Greetings and small talk
# ---------------------------------------------------------------------------

class TestGreetingClassification:
    """User says greeting → Conversation Brain activates, NO retrieval."""

    @pytest.mark.parametrize("message", [
        "hello",
        "Hello!",
        "hi",
        "Hi!",
        "hey",
        "Hey there",
        "good morning",
        "Good Morning!",
        "good afternoon",
        "good evening",
        "namaste",
        "Namaste!",
        "namaskar",
        "howdy",
    ])
    def test_greetings_no_retrieval(self, message):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}'"
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    @pytest.mark.parametrize("message", [
        "thanks",
        "thank you",
        "Thanks!",
        "Thank you!",
        "ty",
        "thx",
    ])
    def test_thanks_no_retrieval(self, message):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}'"
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    @pytest.mark.parametrize("message", [
        "okay",
        "ok",
        "OK",
        "yes",
        "yeah",
        "yep",
        "no",
        "nope",
    ])
    def test_acknowledgements_no_retrieval(self, message):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}'"
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    @pytest.mark.parametrize("message", [
        "bye",
        "goodbye",
        "see ya",
        "take care",
        "later",
    ])
    def test_farewell_no_retrieval(self, message):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}'"
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    @pytest.mark.parametrize("message", [
        "who are you",
        "what are you",
        "how are you",
        "what's up",
    ])
    def test_identity_questions_no_retrieval(self, message):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}'"
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    @pytest.mark.parametrize("message", [
        "hmm",
        "huh",
        "what?",
        "cool",
        "nice",
        "great",
        "awesome",
        "sure",
        "got it",
        "alright",
        "fine",
    ])
    def test_fillers_no_retrieval(self, message):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}'"
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False


# ---------------------------------------------------------------------------
# Test: Retrieval required — Support policy questions
# ---------------------------------------------------------------------------

class TestSupportPolicyClassification:
    """User asks policy/support question → Knowledge Brain activates, retrieval needed."""

    @pytest.mark.parametrize("message", [
        "What is the refund policy for missing items?",
        "Can I cancel after the restaurant starts preparing?",
        "What happens if food poisoning is reported?",
        "Can agents share customer data with delivery partners?",
        "What should happen if the app says delivered but I didn't receive the order?",
        "My order is 50 minutes late, what happens?",
        "Do I get compensation if restaurant cancels?",
        "Can ZapEats share the delivery partner phone number?",
        "What is the cancellation policy for pending orders?",
        "How do I get a refund for a wrong delivery?",
        "My food was stale.",
        "My food is spoiled.",
        "I want my money back.",
        "I did not receive items.",
        "I want to speak to supervisor.",
        "Food had hair in it.",
    ])
    def test_policy_questions_need_retrieval(self, message):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}'"
        assert result["type"] == INTENT_SUPPORT
        assert result["shouldRetrieve"] is True


# ---------------------------------------------------------------------------
# Test: Ambiguous — needs clarification
# ---------------------------------------------------------------------------

class TestAmbiguousClassification:
    """User sends vague message → ask clarification, NO retrieval."""

    @pytest.mark.parametrize("message", [
        "problem",
        "issue",
        "help",
        "refund",
        "late",
        "app issue",
        "order",
    ])
    def test_single_word_ambiguous(self, message):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}'"
        # Single vague words without policy context → AMBIGUOUS
        assert result["shouldRetrieve"] is False

    @pytest.mark.parametrize("message", [
        "refund chahiye",
        "payment failed twice",
        "delivery partner asked for extra money",
    ])
    def test_concrete_policy_keyword_triggers_support(self, message):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}'"
        assert result["type"] == INTENT_SUPPORT
        assert result["shouldRetrieve"] is True

    @pytest.mark.parametrize("message", [
        "Cancel my order",
        "Refund my payment",
        "Track my order",
        "Change my address",
        "Check my ZapEats Coins",
    ])
    def test_order_actions_skip_retrieval_and_ask_identifiers(self, message):
        result = classify_intent_fast(message)
        assert result is not None
        assert result["type"] == INTENT_ORDER_ACTION
        assert result["shouldRetrieve"] is False

    def test_empty_message_is_ambiguous(self):
        result = classify_intent_fast("")
        assert result is not None
        assert result["type"] == INTENT_AMBIGUOUS
        assert result["shouldRetrieve"] is False


# ---------------------------------------------------------------------------
# Test: Conversation Brain replies
# ---------------------------------------------------------------------------

class TestConversationBrainReplies:
    """Conversation Brain should produce warm, human replies without policy facts."""

    def test_hello_reply(self):
        reply = generate_conversation_reply("hello", INTENT_GREETING)
        assert "ZapEats" in reply
        assert "hello" not in reply.lower() or "Hello!" in reply

    def test_namaste_reply(self):
        reply = generate_conversation_reply("namaste", INTENT_GREETING)
        assert "ZapEats" in reply or "swagat" in reply.lower()

    def test_bye_reply(self):
        reply = generate_conversation_reply("bye", INTENT_GREETING)
        assert "ZapEats" in reply or "Thank" in reply

    def test_ambiguous_reply(self):
        reply = generate_conversation_reply("refund", INTENT_AMBIGUOUS)
        assert "refund" in reply.lower() or "help" in reply.lower() or "more" in reply.lower()

    def test_no_policy_facts_in_greeting(self):
        """Conversation Brain MUST NOT invent policy facts."""
        reply = generate_conversation_reply("hello", INTENT_GREETING)
        # Should not contain policy-specific details
        assert "45 minutes" not in reply
        assert "full refund" not in reply
        assert "escalation" not in reply.lower()


# ---------------------------------------------------------------------------
# Test: Chunk relevance validation
# ---------------------------------------------------------------------------

class TestChunkRelevanceValidation:
    """Low-relevance chunks must be filtered out to prevent hallucination."""

    def test_filters_low_score_chunks(self):
        chunks = [
            {"text": "relevant policy", "score": 0.85},
            {"text": "irrelevant career info", "score": 0.15},
            {"text": "marginal match", "score": 0.30},
        ]
        result = validate_chunk_relevance(chunks, min_score=0.35)
        assert len(result) == 1
        assert result[0]["text"] == "relevant policy"

    def test_keeps_all_high_score_chunks(self):
        chunks = [
            {"text": "policy A", "score": 0.90},
            {"text": "policy B", "score": 0.75},
            {"text": "policy C", "score": 0.60},
        ]
        result = validate_chunk_relevance(chunks, min_score=0.35)
        assert len(result) == 3

    def test_returns_empty_when_all_low(self):
        chunks = [
            {"text": "career progression path", "score": 0.10},
            {"text": "team building guide", "score": 0.08},
        ]
        result = validate_chunk_relevance(chunks, min_score=0.35)
        assert len(result) == 0

    def test_empty_chunks_returns_empty(self):
        result = validate_chunk_relevance([], min_score=0.35)
        assert len(result) == 0


# ---------------------------------------------------------------------------
# Test: The critical bug fix — "hello" must NOT trigger retrieval
# ---------------------------------------------------------------------------

class TestCriticalBugFix:
    """The bug: User says 'hello', system retrieves Call Script Guide chunks.
    Expected: Intent=GREETING_OR_SMALLTALK, Retrieval=skipped.
    """

    def test_hello_no_retrieval(self):
        result = classify_intent_fast("hello")
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    def test_namaste_no_retrieval(self):
        result = classify_intent_fast("namaste")
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    def test_hi_no_retrieval(self):
        result = classify_intent_fast("hi")
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    def test_hello_with_stt_end_token_no_retrieval(self):
        result = classify_intent_fast("Hello, <end>")
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    @pytest.mark.parametrize("message", [
        "all right, thank you",
        "alright thank you",
        "okay thank you",
        "thanks a lot",
        "thank you so much",
        "that works thank you",
        "Hello end",
    ])
    def test_compound_smalltalk_no_retrieval(self, message):
        result = classify_intent_fast(message)
        assert result["type"] == INTENT_GREETING
        assert result["shouldRetrieve"] is False

    def test_compound_thanks_reply_is_not_clarification(self):
        reply = generate_conversation_reply("all right, thank you", INTENT_GREETING)
        assert "Could you tell me a bit more" not in reply
        assert "refund, delivery issue" not in reply
        assert "welcome" in reply.lower() or "anything else" in reply.lower()

    def test_greeting_reply_is_human(self):
        reply = generate_conversation_reply("hello", INTENT_GREETING)
        # Must NOT contain "Based on approved knowledge"
        assert "Based on approved knowledge" not in reply
        # Must NOT contain document chunk text
        assert "Call Script" not in reply
        assert "Agent Training Manual" not in reply
        # Must be a warm human greeting
        assert "Hello" in reply or "Hi" in reply
        assert "ZapEats" in reply

    @pytest.mark.asyncio
    async def test_runtime_hello_skips_retrieval(self):
        tenant_res = TenantResources(
            tenant_id="test-tenant",
            organization_id=uuid.uuid4(),
            qdrant_collection_name="test_collection",
            provider_status={},
            provisioning_status="success",
        )
        result = await AgentRuntimeService.answer_text(tenant_res, "hello")
        assert result["intent"]["type"] == INTENT_GREETING
        assert result["retrieval"]["used"] is False
        assert result["chunks"] == []
        assert "ZapEats" in result["answer"]


# ---------------------------------------------------------------------------
# Test: Multilingual policy keywords trigger SUPPORT (not AMBIGUOUS)
# ---------------------------------------------------------------------------

class TestMultilingualPolicyKeywords:
    """Non-English policy keywords must trigger SUPPORT with retrieval."""

    @pytest.mark.parametrize("message,lang_name", [
        ("రీఫండ్", "Telugu"),
        ("రీఫండ్ గురించి చెప్పండి", "Telugu"),
        ("रिफंड चाहिए", "Hindi"),
        ("ऑर्डर कैंसल करो", "Hindi"),
        ("রিফান্ড দরকার", "Bengali"),
        ("ரீஃபண்ட் வேண்டும்", "Tamil"),
        ("ರೀಫಂಡ್ ಬೇಕು", "Kannada"),
        ("റീഫണ്ട് വേണം", "Malayalam"),
        ("refund chahiye", "Hinglish"),
        ("order nahi aaya", "Hinglish"),
    ])
    def test_multilingual_policy_words_trigger_support(self, message, lang_name):
        result = classify_intent_fast(message)
        assert result is not None, f"Expected fast classification for '{message}' ({lang_name})"
        assert result["type"] == INTENT_SUPPORT, f"'{message}' ({lang_name}) should be SUPPORT, got {result['type']}"
        assert result["shouldRetrieve"] is True


# ---------------------------------------------------------------------------
# Test: Language detection
# ---------------------------------------------------------------------------

class TestLanguageDetection:
    """Zero-cost script-based language detection."""

    def test_english_default(self):
        assert detect_language("hello how are you") == "en"

    def test_hindi_devanagari(self):
        assert detect_language("नमस्ते, मुझे रिफंड चाहिए") == "hi"

    def test_bengali_script(self):
        assert detect_language("আমার অর্ডার আসেনি") == "bn"

    def test_tamil_script(self):
        assert detect_language("எனது ஆர்டர் வரவில்லை") == "ta"

    def test_telugu_script(self):
        assert detect_language("నా ఆర్డర్ రాలేదు") == "te"

    def test_kannada_script(self):
        assert detect_language("ನನ್ನ ಆರ್ಡರ್ ಬರಲಿಲ್ಲ") == "kn"

    def test_gujarati_script(self):
        assert detect_language("મારો ઓર્ડર આવ્યો નથી") == "gu"

    def test_punjabi_script(self):
        assert detect_language("ਮੇਰਾ ਆਰਡਰ ਨਹੀਂ ਆਇਆ") == "pa"

    def test_malayalam_script(self):
        assert detect_language("എന്റെ ഓർഡർ വന്നില്ല") == "ml"

    def test_urdu_arabic_script(self):
        assert detect_language("میرا آرڈر نہیں آیا") == "ur"

    def test_romanized_hindi(self):
        assert detect_language("mera order nahi aaya hai") == "hi"

    def test_hinglish(self):
        assert detect_language("kya refund milega") == "hi"

    def test_empty_string(self):
        assert detect_language("") == "en"

    def test_none(self):
        assert detect_language(None) == "en"


# ---------------------------------------------------------------------------
# Test: Multilingual conversation brain replies
# ---------------------------------------------------------------------------

class TestMultilingualReplies:
    """Agent must reply in the user's language."""

    def test_hindi_greeting(self):
        reply = generate_conversation_reply("namaste", INTENT_GREETING, language="hi")
        # Should contain Hindi text
        assert "ZapEats" in reply
        assert "स्वागत" in reply or "नमस्ते" in reply

    def test_bengali_greeting(self):
        reply = generate_conversation_reply("hello", INTENT_GREETING, language="bn")
        assert "ZapEats" in reply
        assert "স্বাগতম" in reply or "নমস্কার" in reply

    def test_tamil_greeting(self):
        reply = generate_conversation_reply("hello", INTENT_GREETING, language="ta")
        assert "ZapEats" in reply
        assert "வணக்கம்" in reply

    def test_telugu_greeting(self):
        reply = generate_conversation_reply("hello", INTENT_GREETING, language="te")
        assert "ZapEats" in reply
        assert "నమస్కారం" in reply or "స్వాగతం" in reply

    def test_english_greeting_default(self):
        reply = generate_conversation_reply("hello", INTENT_GREETING, language="en")
        assert "Hello" in reply
        assert "ZapEats" in reply

    def test_hindi_clarification(self):
        reply = generate_conversation_reply("refund", INTENT_AMBIGUOUS, language="hi")
        assert "रिफंड" in reply or "मदद" in reply

    def test_unknown_language_falls_back_to_english(self):
        reply = generate_conversation_reply("hello", INTENT_GREETING, language="xx")
        assert "Hello" in reply
        assert "ZapEats" in reply
