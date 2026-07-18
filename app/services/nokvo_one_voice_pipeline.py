from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


import asyncio
from datetime import datetime, time, timedelta, timezone
import json
import re
import uuid
from time import perf_counter
from typing import Any, AsyncIterator
from urllib import parse as urllib_parse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.services.datetime_parse import DateTimeParseError
from app.services.agent_config_keys import (
    AGENT_POLICY_CARDS_KEY,
    AGENT_SINGLE_PROMPT_CONFIG_KEY,
    policy_version as _agent_policy_version,
)
# Document-RAG source-type tag (retrieval scope). Retained until the KB
# document-RAG path is removed from the retrieval methods.
from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    compose_outbound_system_section,
    update_outbound_memory,
)
from app.services.agent_robustness import (
    CLARIFY_ESCALATE,
    CLARIFY_NUDGE,
    CLARIFY_OFFER_OPTIONS,
    CLARIFY_RESET,
    ClarificationState,
    clarification_prompt,
    is_turn_vague,
)
from app.services.agent_runtime_bundle import RuntimeBundle, get_bundle as get_runtime_bundle
from app.services.dynamic_tool_resolver import resolve_index
from app.services.agent_session_store import AgentSessionStore
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.fast_intent_router import (
    INTENT_AUDIO_CHECK,
    INTENT_CANCELLATION_REQUEST,
    INTENT_GOODBYE,
    INTENT_GREETING,
    INTENT_REFUND_ELIGIBILITY,
    INTENT_SMALLTALK,
    INTENT_THANKS,
    INTENT_UNKNOWN_GENERAL,
    FastIntentRouter,
    IntentResult,
    detect_policy_keyword,
)
from app.services.llm_intent_classifier import (
    INTENT_CANCELLATION_REQUEST as LLM_INTENT_CANCEL,
    INTENT_COMPLAINT as LLM_INTENT_COMPLAINT,
    INTENT_ESCALATION as LLM_INTENT_ESCALATION,
    INTENT_KB_QUESTION as LLM_INTENT_KB,
    INTENT_ORDER_STATUS as LLM_INTENT_ORDER_STATUS,
    INTENT_OUT_OF_SCOPE as LLM_INTENT_OUT_OF_SCOPE,
    INTENT_REFUND_ELIGIBILITY as LLM_INTENT_REFUND,
    INTENT_SMALLTALK as LLM_INTENT_SMALLTALK,
    INTENT_UNCLEAR as LLM_INTENT_UNCLEAR,
    LLMIntentClassifier,
)
from app.services.policy_decision_engine import (
    DEC_EXACT_MATCH,
    DEC_MATRIX_RESPONSE,
    DEC_NO_MATCH,
    PolicyDecisionEngine,
    extract_live_context_from_history,
    fetch_live_order_context,
)
from app.services.nokvo_one_business_templates import custom_tabs_from_overrides
from app.services.predefined_tools_service import PredefinedToolsService
from app.services.prosody import (
    DEFAULT_TONE,
    ProsodyChunk,
    prosody_for,
    strip_tone_tags,
    stream_prosody_chunks,
)
from app.services.sarvam_voice_service import SARVAM_LANGUAGE_OPTIONS, SarvamVoiceService
from app.services.tool_flow_policy import evaluate_tool_flow_policy
from app.services.tool_flow_questions import build_tool_flow_questions, format_field_questions_prompt
from app.services.voice_turn_policy import (
    evaluate_voice_turn_policy,
    normalize_relative_datetime_text,
)

# Re-export shim: these were defined here before the extraction to
# azure_grounded_llm.py; tests and a dozen services import them from this
# module path, which stays canonical.
from app.services.azure_grounded_llm import (  # noqa: F401
    AzureGroundedLLM,
    NokvoOneAgentRateLimited,
    NokvoOneAgentRuntimeError,
    _meter_call_llm,
)

# Re-export shims for module-level names moved to pipeline/appointments and
# pipeline/retrieval - this path stays canonical for tests and callers.
from app.services.pipeline.appointments import (  # noqa: F401
    _APPOINTMENT_LOCAL_TZ,
    _AppointmentToolInputError,
    _MONTH_INDEX,
    _next_day_of_month,
    _ORDINAL_WORDS,
    _WEEKDAY_INDEX,
    _WEEKDAY_RE,
)
from app.services.pipeline.retrieval import (  # noqa: F401
    _SENSITIVE_OR_DYNAMIC_RE,
)
from app.services.pipeline.real_estate_leads import (  # noqa: F401
    _MULTILINGUAL_DISINTEREST_PHRASES,
)



# A "." only ends a sentence when NOT preceded by a digit, so a decimal amount
# ("₹2.45Cr", or a model-spaced "₹2. 45Cr") is never split mid-number. ! ? ।
# always end. The terminator is captured (group-less) so _first_sentence keeps
# slicing through match.start()+1 (the char before the whitespace).
_SENTENCE_RE = re.compile(r"(?:(?<!\d)\.|[!?।])\s+")
_SMALLTALK_RE = re.compile(
    r"^(hi|hello|hey|namaste|namaskar|thanks?|thank you|okay|ok|bye|goodbye|good morning|good evening)[\s!.?]*$",
    re.IGNORECASE,
)
_ORDER_CONTEXT_RE = re.compile(r"\b(order|delivery|shipment|ఆర్డర్|డెలివరీ)\b", re.IGNORECASE)
_LOCATION_EXPLICIT_RE = re.compile(
    "|".join([
        r"\b(location|address|directions?|area|branch|clinic|hospital|where\s+(?:are|is)\s+(?:you|it|the))\b",
        "లొకేషన్", "లోకేషన్", "చిరునామా", "అడ్రస్", "క్లినిక్", "హాస్పిటల్",
        "लोकेशन", "पता", "एड्रेस", "क्लिनिक", "अस्पताल",
        "லொகேஷன்", "முகவரி", "கிளினிக்", "மருத்துவமனை",
        "ಲೊಕೇಶನ್", "ವಿಳಾಸ", "ಕ್ಲಿನಿಕ್", "ಆಸ್ಪತ್ರೆ",
        "ലൊക്കേഷൻ", "വിലാസം", "ക്ലിനിക്ക്", "ആശുപത്രി",
        "লোকেশন", "ঠিকানা", "ক্লিনিক", "হাসপাতাল",
        "લોકેશન", "સરનામું", "ક્લિનિક", "હોસ્પિટલ",
    ]),
    re.IGNORECASE,
)
_LOCATION_WHERE_RE = re.compile(
    "|".join([
        r"\bwhere\b",
        "ఎక్కడ", "ఎక్కడుంది", "ఎక్కడున్నది", "ఎక్కడ ఉందండి",
        "कहाँ", "कहा",
        "எங்கே",
        "ಎಲ್ಲಿ",
        "എവിടെ",
        "কোথায়",
        "ક્યાં",
    ]),
    re.IGNORECASE,
)
_FACILITY_LOCATION_RE = re.compile(
    "|".join([
        r"\b(clinic|hospital)\b",
        "క్లినిక్", "హాస్పిటల్",
        "क्लिनिक", "अस्पताल",
        "கிளினிக்", "மருத்துவமனை",
        "ಕ್ಲಿನಿಕ್", "ಆಸ್ಪತ್ರೆ",
        "ക്ലിനിക്ക്", "ആശുപത്രി",
        "ক্লিনিক", "হাসপাতাল",
        "ક્લિનિક", "હોસ્પિટલ",
    ]),
    re.IGNORECASE,
)


# _normalize moved to pipeline/text_norm.py (shared with the extracted
# pipeline modules); re-exported here so existing references keep working.
from app.services.pipeline.text_norm import _normalize  # noqa: F401, E402


def _first_sentence(buffer: str) -> tuple[str, str] | None:
    for match in _SENTENCE_RE.finditer(buffer):
        sentence = buffer[: match.start() + 1].strip()
        if len(sentence) >= 8:
            return sentence, buffer[match.end():].lstrip()
    max_chars = max(40, settings.AGENT_MAX_FIRST_SENTENCE_CHARS)
    if len(buffer) >= max_chars:
        split_at = buffer.rfind(" ", 20, max_chars)
        if split_at > 0:
            return buffer[:split_at].strip(), buffer[split_at:].lstrip()
    return None



class NokvoOneVoicePipeline:
    @staticmethod
    def normalize_language(language: str | None) -> str:
        return SarvamVoiceService.normalize_language(language)

    @staticmethod
    def language_label(language: str | None) -> str:
        return SarvamVoiceService.language_label(language)

    @staticmethod
    def _refusal(language: str) -> str:
        return {
            "hi": "मेरे पास इस सवाल का जवाब देने के लिए पर्याप्त जानकारी नहीं है। मैं इसे सपोर्ट टीम को आगे भेज सकता हूँ।",
            "ta": "இந்த கேள்விக்கு பதில் சொல்ல போதுமான தகவல் இல்லை. இதை ஆதரவு குழுவிற்கு அனுப்பலாம்.",
            "te": "ఈ ప్రశ్నకు సమాధానం ఇవ్వడానికి తగిన సమాచారం లేదు. దీన్ని సపోర్ట్ టీమ్‌కు పంపగలను.",
            "bn": "এই প্রশ্নের উত্তর দেওয়ার মতো যথেষ্ট তথ্য নেই। আমি এটি সাপোর্ট টিমে পাঠাতে পারি।",
        }.get(language, "I do not have enough information to answer that. I can escalate this to support.")

    @staticmethod
    def _is_refusal(answer: str, language: str) -> bool:
        """Single source of truth for the LLM-refused check. Previously
        inlined as ``answer == _refusal(language)`` in both
        ``answer_text`` and ``stream_answer_sentences`` — identical
        logic in two places. Centralising it means a change to the
        refusal phrase flows through one comparator."""
        if not answer:
            return True
        return str(answer).strip() == NokvoOneVoicePipeline._refusal(language).strip()

    @staticmethod
    def _no_context_answer(
        user_text: str,
        *,
        intent: str | None,
        language: str,
        company_name: str | None,
    ) -> tuple[str, bool]:
        """Pick the caller-facing reply for the no-retrieved-chunks path.

        Returns ``(answer, refused)``. The branches were previously
        duplicated between ``answer_text`` and ``stream_answer_sentences``;
        drift between the two copies would surface as a chat reply that
        differs from the voice reply for the same input. One helper
        keeps them honest.
        """
        if _SMALLTALK_RE.match(user_text or ""):
            return (
                NokvoOneVoicePipeline._smalltalk_reply(user_text, language, company_name),
                False,
            )
        if intent == INTENT_UNKNOWN_GENERAL:
            return NokvoOneVoicePipeline._open_question(language), False
        return NokvoOneVoicePipeline._refusal(language), True

    @staticmethod
    def _rate_limited_reply(language: str) -> str:
        """Specific fallback for Azure OpenAI 429s. The generic refusal sounds
        like 'I can't help' — this sounds like 'try again', which is the
        actual truth: the LLM is rate-limited, not refusing."""
        return {
            "hi": "एक सेकंड दीजिए, मैं थोड़ा व्यस्त हूँ। एक पल में फिर से पूछिए।",
            "ta": "ஒரு நிமிடம், கொஞ்சம் நெரிசலாக இருக்கிறது. மீண்டும் முயற்சி செய்யுங்கள்.",
            "te": "ఒక్క క్షణం, కొంచెం బిజీగా ఉంది. మళ్లీ ప్రయత్నించండి.",
            "bn": "এক সেকেন্ড, একটু ব্যস্ত আছি। আবার চেষ্টা করুন।",
        }.get(language, "Give me a second — I'm a bit busy right now. Could you try that again in a moment?")

    @staticmethod
    def _open_question(language: str) -> str:
        """Used when retrieval is empty AND no specific intent was matched —
        ask the user what they need rather than dumping the formal refusal,
        which sounds robotic for casual utterances like 'can you hear me?'."""
        return {
            "hi": "माफ़ कीजिए, मैं समझ नहीं पाया। आप क्या मदद चाहते हैं?",
            "ta": "மன்னிக்கவும், சரியாகப் புரியவில்லை. என்ன உதவி வேண்டும்?",
            "te": "క్షమించండి, అర్థం కాలేదు. ఏమి సహాయం కావాలి?",
            "bn": "মাফ করবেন, বুঝতে পারিনি। কীভাবে সাহায্য করতে পারি?",
        }.get(language, "Sorry, I missed that — what can I help you with?")

    @staticmethod
    def _smalltalk_reply(query: str, language: str, company_name: str | None = None) -> str:
        name = company_name or "Nokvo"
        if re.search(r"\b(thanks?|thank you)\b", query, re.IGNORECASE):
            return {
                "hi": "आपका स्वागत है। क्या मैं और कुछ मदद कर सकता हूँ?",
                "ta": "நன்றி. வேறு ஏதாவது உதவி வேண்டுமா?",
                "te": "ధన్యవాదాలు. ఇంకేమైనా సహాయం కావాలా?",
            }.get(language, "You're welcome. Anything else I can help with?")
        return {
            "hi": f"नमस्ते, {name} सपोर्ट में आपका स्वागत है। मैं कैसे मदद कर सकता हूँ?",
            "ta": f"வணக்கம், {name} ஆதரவிற்கு வரவேற்கிறோம். எப்படி உதவலாம்?",
            "te": f"నమస్కారం, {name} సపోర్ట్‌కు స్వాగతం. ఎలా సహాయం చేయగలను?",
            "bn": f"নমস্কার, {name} সাপোর্টে স্বাগতম। কীভাবে সাহায্য করতে পারি?",
        }.get(language, f"Hi, thanks for calling {name}. How can I help?")

    @staticmethod
    def _is_short_permission_reply(user_text: str) -> bool:
        cleaned = re.sub(r"[^\w\s]", " ", (user_text or "").lower())
        cleaned = re.sub(r"\s+", " ", cleaned).strip()
        if not cleaned or len(cleaned.split()) > 3:
            return False
        return cleaned in {
            "yes", "yeah", "ya", "yep", "sure", "ok", "okay", "go on",
            "go ahead", "tell me", "continue", "fine", "alright",
        }

    @staticmethod
    def _last_assistant_text(history: list[dict[str, str]]) -> str:
        for turn in reversed(history or []):
            if turn.get("role") == "assistant":
                return str(turn.get("content") or "")
        return ""

    @staticmethod
    def _assistant_asked_for_user_decision(text: str) -> bool:
        cleaned = re.sub(r"\s+", " ", text or "").strip().lower()
        if not cleaned:
            return False
        if "?" in cleaned:
            return True
        return bool(
            re.search(
                r"\b("
                r"would\s+you\s+like|do\s+you\s+want|should\s+i|shall\s+i|"
                r"can\s+i|may\s+i|want\s+me\s+to|go\s+ahead"
                r")\b",
                cleaned,
            )
        )

    @staticmethod
    def _outbound_post_opener_permission_reply(
        user_text: str,
        *,
        language: str,
        history: list[dict[str, str]],
        outbound_context: OutboundCampaignContext | None,
        covered_objectives: list[str] | None,
    ) -> str | None:
        if language != "en" or outbound_context is None:
            return None
        if not NokvoOneVoicePipeline._is_short_permission_reply(user_text):
            return None
        last_assistant = NokvoOneVoicePipeline._last_assistant_text(history).lower()
        if "good time" not in last_assistant and "talk for a minute" not in last_assistant:
            return None

        remaining = outbound_context.remaining_objectives(covered_objectives or [])
        objective_text = " ".join(remaining or outbound_context.objectives or [])
        haystack = f"{objective_text} {outbound_context.goal} {outbound_context.pitch_summary}".lower()
        if "self" in haystack and "investment" in haystack:
            return "Great, is this for self-use or investment?"
        if "bhk" in haystack or "bedroom" in haystack or "size" in haystack:
            return "Great, which BHK size are you considering?"
        if "site visit" in haystack or "visit" in haystack:
            return "Great, would you prefer a weekday or weekend site visit?"
        if "demo" in haystack:
            return "Great, what would you like to evaluate in the demo?"
        if "appointment" in haystack:
            return "Great, what time would work for an appointment?"
        return "Great, what are you looking for right now?"

    @staticmethod
    def _business_location_retrieval_rewrite(text: str) -> str | None:
        haystack = text or ""
        if not haystack.strip():
            return None
        explicit_location = bool(_LOCATION_EXPLICIT_RE.search(haystack))
        whereish = bool(_LOCATION_WHERE_RE.search(haystack))
        if not explicit_location:
            return None
        if _ORDER_CONTEXT_RE.search(haystack) and not whereish:
            return None
        if _FACILITY_LOCATION_RE.search(haystack):
            return "clinic location address area directions where is the clinic contact"
        return "business location address area directions where is the business contact"

    @staticmethod
    def retrieval_query_for(user_text: str, retrieval_text: str | None = None) -> str:
        user_text = _normalize(user_text)
        translated = _normalize(retrieval_text or "")
        if translated and translated != user_text:
            return translated
        rewritten = NokvoOneVoicePipeline._business_location_retrieval_rewrite(user_text)
        if rewritten:
            return rewritten
        if detect_policy_keyword(user_text):
            return "cancellation refund policy order cancellation refund eligibility"
        return user_text

    @staticmethod
    def should_skip_translate_for_native_query(user_text: str) -> bool:
        return bool(NokvoOneVoicePipeline._business_location_retrieval_rewrite(user_text))

    @staticmethod
    def _sanitize_answer(answer: str) -> str:
        text = re.sub(r"\s+", " ", answer or "").strip()
        text = re.sub(r"\[(?:context|source|chunk)\s*\d+\]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\bQdrant|Redis|prompt|retrieved context\b", "", text, flags=re.IGNORECASE)
        # Strip any leaked prosody tags before any non-streaming path sends the
        # answer to the caller. The streaming path already strips them via the
        # prosody parser, but cached / fallback returns bypass that.
        text = strip_tone_tags(text)
        return text.strip()

    @staticmethod
    def _cacheable(query: str, answer: str, chunks: list[dict[str, Any]]) -> bool:
        # Body extracted to app.services.pipeline.retrieval._cacheable
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.retrieval import _cacheable

        return _cacheable(query, answer, chunks)

    @staticmethod
    def _map_point(point: Any) -> dict[str, Any]:
        # Body extracted to app.services.pipeline.retrieval._map_point
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.retrieval import _map_point

        return _map_point(point)

    @staticmethod
    def _chunks_from_outbound_doc(
        outbound_context: OutboundCampaignContext | None,
    ) -> list[dict[str, Any]]:
        # Body extracted to app.services.pipeline.retrieval._chunks_from_outbound_doc
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.retrieval import _chunks_from_outbound_doc

        return _chunks_from_outbound_doc(outbound_context)

    @staticmethod
    def _expand_parent_section(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        # Body extracted to app.services.pipeline.retrieval._expand_parent_section
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.retrieval import _expand_parent_section

        return _expand_parent_section(chunks)

    @staticmethod
    async def retrieve(
        tenant_res: TenantResources,
        query: str,
        *,
        db: AsyncSession | None = None,
        top_k: int | None = None,
        campaign_id: str | None = None,
        intent_result: IntentResult | None = None,
        english_text: str | None = None,
        dual_retrieval: bool = False,
    ) -> dict[str, Any]:
        # Knowledge-Base document retrieval is retired: the agent answers from
        # its vertical system prompt, not Qdrant/embedding retrieval. Returns an
        # empty result; callers handle ``chunks == []`` by not grounding.
        # Body extracted to app.services.pipeline.retrieval.retrieve
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.retrieval import retrieve

        return await retrieve(NokvoOneVoicePipeline, tenant_res, query, db=db, top_k=top_k, campaign_id=campaign_id, intent_result=intent_result, english_text=english_text, dual_retrieval=dual_retrieval)

    @staticmethod
    async def _retrieve_dual(
        tenant_res: TenantResources,
        *,
        primary: str,
        secondary: str,
        db: AsyncSession | None,
        top_k: int | None,
        campaign_id: str | None,
        intent_result: IntentResult | None,
    ) -> dict[str, Any]:
        # Body extracted to app.services.pipeline.retrieval._retrieve_dual
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.retrieval import _retrieve_dual

        return await _retrieve_dual(NokvoOneVoicePipeline, tenant_res, primary=primary, secondary=secondary, db=db, top_k=top_k, campaign_id=campaign_id, intent_result=intent_result)

    @staticmethod
    def _policy_card_chunks(tenant_res: TenantResources, policy_version: str) -> list[dict[str, Any]]:
        # Body extracted to app.services.pipeline.prompt_blocks._policy_card_chunks
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.prompt_blocks import _policy_card_chunks

        return _policy_card_chunks(tenant_res, policy_version)

    @staticmethod
    def _single_prompt_guidance(tenant_res: TenantResources) -> str:
        # Explicit-admin-override probe only. This gates whether to SUPPRESS
        # the built-in FSMs (clinic appointments, etc.) — NOT whether the agent
        # has a persona. The curated per-vertical persona is always present and
        # is composed separately on the async bundle path
        # (``agent_runtime_bundle._single_prompt_guidance``). Returning "" when
        # no legacy override is configured (the normal case now) lets the
        # built-in FSMs run.
        # Body extracted to app.services.pipeline.prompt_blocks._single_prompt_guidance
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.prompt_blocks import _single_prompt_guidance

        return _single_prompt_guidance(tenant_res)

    @staticmethod
    def _single_prompt_enabled(tenant_res: TenantResources) -> bool:
        # Body extracted to app.services.pipeline.prompt_blocks._single_prompt_enabled
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.prompt_blocks import _single_prompt_enabled

        return _single_prompt_enabled(NokvoOneVoicePipeline, tenant_res)

    @staticmethod
    async def _projects_block_for_bundle(
        db: AsyncSession | None,
        bundle: "RuntimeBundle",
    ) -> tuple[str, list]:
        # Body extracted to app.services.pipeline.prompt_blocks._projects_block_for_bundle
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.prompt_blocks import _projects_block_for_bundle

        return await _projects_block_for_bundle(db, bundle)

    @staticmethod
    async def _services_block_for_bundle(
        db: AsyncSession | None,
        bundle: "RuntimeBundle",
    ) -> str:
        # Body extracted to app.services.pipeline.prompt_blocks._services_block_for_bundle
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.prompt_blocks import _services_block_for_bundle

        return await _services_block_for_bundle(db, bundle)

    @staticmethod
    def _focus_project_summary(
        projects: list,
        conversational_memory: Any,
    ) -> str | None:
        # Body extracted to app.services.pipeline.prompt_blocks._focus_project_summary
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.prompt_blocks import _focus_project_summary

        return _focus_project_summary(projects, conversational_memory)

    @staticmethod
    async def _voice_business_context(
        db: AsyncSession | None,
        tenant_res: TenantResources,
    ) -> tuple[Organization, dict[str, Any], list[dict[str, Any]]] | None:
        # Body extracted to app.services.pipeline.prompt_blocks._voice_business_context
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.prompt_blocks import _voice_business_context

        return await _voice_business_context(db, tenant_res)

    @staticmethod
    def _parse_appointment_date(value: Any, *, now: datetime | None = None) -> datetime.date:
        # Body extracted to app.services.pipeline.appointments._parse_appointment_date
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.appointments import _parse_appointment_date

        return _parse_appointment_date(value, now=now)

    @staticmethod
    def _parse_appointment_time(value: Any) -> time:
        # Body extracted to app.services.pipeline.appointments._parse_appointment_time
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.appointments import _parse_appointment_time

        return _parse_appointment_time(value)

    @staticmethod
    def _appointment_datetime_iso(appointment: dict[str, Any]) -> str:
        # Fast path: caller already accepted a proposed slot, which left a
        # canonical UTC ISO on the appointment. Trust it and skip re-parsing.
        # Body extracted to app.services.pipeline.appointments._appointment_datetime_iso
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.appointments import _appointment_datetime_iso

        return _appointment_datetime_iso(NokvoOneVoicePipeline, appointment)

    @staticmethod
    def _should_offer_sms_confirmation(tenant_res: TenantResources | None) -> bool:
        # Body extracted to app.services.pipeline.appointments._should_offer_sms_confirmation
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.appointments import _should_offer_sms_confirmation

        return _should_offer_sms_confirmation(tenant_res)

    @staticmethod
    def _appointment_tool_answer(
        result: dict[str, Any],
        args: dict[str, Any],
        *,
        language: str | None = None,
        offer_sms: bool = False,
    ) -> str:
        # Body extracted to app.services.pipeline.appointments._appointment_tool_answer
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.appointments import _appointment_tool_answer

        return _appointment_tool_answer(result, args, language=language, offer_sms=offer_sms)

    @staticmethod
    async def _handle_availability_check(
        tenant_res: TenantResources,
        db: AsyncSession | None,
        turn_policy: dict[str, Any],
    ) -> dict[str, Any] | None:
        # Body extracted to app.services.pipeline.appointments._handle_availability_check
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.appointments import _handle_availability_check

        return await _handle_availability_check(NokvoOneVoicePipeline, tenant_res, db, turn_policy)

    @staticmethod
    async def _maybe_execute_turn_policy_action(
        tenant_res: TenantResources,
        call_id: str | None,
        db: AsyncSession | None,
        turn_policy: dict[str, Any],
    ) -> dict[str, Any] | None:
        # Body extracted to app.services.pipeline.appointments._maybe_execute_turn_policy_action
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.appointments import _maybe_execute_turn_policy_action

        return await _maybe_execute_turn_policy_action(NokvoOneVoicePipeline, tenant_res, call_id, db, turn_policy)

    @staticmethod
    def _map_lead_data_to_ticket_shape(data: dict[str, Any], industry: str | None) -> dict[str, Any]:
        # Body extracted to app.services.pipeline.real_estate_leads._map_lead_data_to_ticket_shape
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _map_lead_data_to_ticket_shape

        return _map_lead_data_to_ticket_shape(data, industry)

    @staticmethod
    async def _route_record_by_surface(
        db: AsyncSession,
        record_ids: list[Any],
        *,
        call_surface: str | None,
        industry: str | None = None,
        force_ticket: bool = False,
    ) -> None:
        # Body extracted to app.services.pipeline.real_estate_leads._route_record_by_surface
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _route_record_by_surface

        return await _route_record_by_surface(NokvoOneVoicePipeline, db, record_ids, call_surface=call_surface, industry=industry, force_ticket=force_ticket)

    @staticmethod
    def _campaign_contact(campaign_context: dict[str, Any] | None) -> dict[str, Any]:
        # Body extracted to app.services.pipeline.real_estate_leads._campaign_contact
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _campaign_contact

        return _campaign_contact(campaign_context)

    @staticmethod
    def _phone_from_call_context(
        memory: dict[str, Any],
        campaign_context: dict[str, Any] | None,
    ) -> str:
        # Body extracted to app.services.pipeline.real_estate_leads._phone_from_call_context
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _phone_from_call_context

        return _phone_from_call_context(NokvoOneVoicePipeline, memory, campaign_context)

    @staticmethod
    def _budget_number(value: Any) -> float | None:
        # Body extracted to app.services.pipeline.real_estate_leads._budget_number
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _budget_number

        return _budget_number(value)

    @staticmethod
    @staticmethod
    def _real_estate_opt_out(
        *,
        memory: dict[str, Any],
        history: list[dict[str, str]],
    ) -> bool:
        # Body extracted to app.services.pipeline.real_estate_leads._real_estate_opt_out
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _real_estate_opt_out

        return _real_estate_opt_out(memory=memory, history=history)

    def _real_estate_interest_signal(
        *,
        memory: dict[str, Any],
        history: list[dict[str, str]],
        call_surface: str | None,
        outbound_context: OutboundCampaignContext | None,
    ) -> bool:
        # Body extracted to app.services.pipeline.real_estate_leads._real_estate_interest_signal
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _real_estate_interest_signal

        return _real_estate_interest_signal(memory=memory, history=history, call_surface=call_surface, outbound_context=outbound_context)

    @staticmethod
    def _real_estate_memory_from_history(
        memory: dict[str, Any],
        history: list[dict[str, str]],
    ) -> dict[str, Any]:
        # Body extracted to app.services.pipeline.real_estate_leads._real_estate_memory_from_history
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _real_estate_memory_from_history

        return _real_estate_memory_from_history(memory, history)

    @staticmethod
    def _lead_args_from_call_memory(
        *,
        memory: dict[str, Any],
        campaign_context: dict[str, Any] | None,
        outbound_context: OutboundCampaignContext | None,
    ) -> dict[str, Any]:
        # Body extracted to app.services.pipeline.real_estate_leads._lead_args_from_call_memory
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _lead_args_from_call_memory

        return _lead_args_from_call_memory(NokvoOneVoicePipeline, memory=memory, campaign_context=campaign_context, outbound_context=outbound_context)

    @staticmethod
    def _site_visit_args_from_call_state(
        *,
        state: dict[str, Any],
        organization: Any,
        overrides: dict[str, Any],
        custom_tabs: list[dict[str, Any]],
        memory: dict[str, Any],
        campaign_context: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        # Body extracted to app.services.pipeline.real_estate_leads._site_visit_args_from_call_state
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _site_visit_args_from_call_state

        return _site_visit_args_from_call_state(NokvoOneVoicePipeline, state=state, organization=organization, overrides=overrides, custom_tabs=custom_tabs, memory=memory, campaign_context=campaign_context)

    @staticmethod
    async def _send_brochure_and_location_sms(
        db: AsyncSession,
        org_id: Any,
        tenant_res: TenantResources,
        call_id: str,
        state: dict[str, Any],
    ) -> None:
        # Body extracted to app.services.pipeline.real_estate_leads._send_brochure_and_location_sms
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _send_brochure_and_location_sms

        return await _send_brochure_and_location_sms(db, org_id, tenant_res, call_id, state)

    @staticmethod
    def _captured_project(state: dict[str, Any] | None, memory: dict[str, Any] | None) -> str | None:
        # Body extracted to app.services.pipeline.real_estate_leads._captured_project
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _captured_project

        return _captured_project(state, memory)

    @staticmethod
    async def _resolve_inbound_project(
        db: AsyncSession,
        org_id: Any,
        *,
        candidate: str | None,
        history: list[dict[str, str]] | None,
    ) -> tuple[str | None, str | None]:
        # Body extracted to app.services.pipeline.real_estate_leads._resolve_inbound_project
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _resolve_inbound_project

        return await _resolve_inbound_project(db, org_id, candidate=candidate, history=history)

    @staticmethod
    def _deterministic_call_note(
        *,
        kind: str,
        name: str | None,
        ani: str | None,
        memory: dict[str, Any],
        history: list[dict[str, str]],
        project: str | None = None,
    ) -> str:
        # Body extracted to app.services.pipeline.real_estate_leads._deterministic_call_note
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _deterministic_call_note

        return _deterministic_call_note(kind=kind, name=name, ani=ani, memory=memory, history=history, project=project)

    @staticmethod
    async def _create_inbound_site_visit(
        db: AsyncSession,
        org_id: Any,
        tenant_res: TenantResources,
        call_id: str,
        *,
        state: dict[str, Any],
        memory: dict[str, Any],
        history: list[dict[str, str]] | None = None,
    ) -> dict[str, Any] | None:
        # Body extracted to app.services.pipeline.real_estate_leads._create_inbound_site_visit
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _create_inbound_site_visit

        return await _create_inbound_site_visit(NokvoOneVoicePipeline, db, org_id, tenant_res, call_id, state=state, memory=memory, history=history)

    @staticmethod
    async def maybe_create_real_estate_lead_from_call(
        tenant_res: TenantResources,
        db: AsyncSession | None,
        call_id: str | None,
        *,
        campaign_context: dict[str, Any] | None = None,
        outbound_context: OutboundCampaignContext | None = None,
    ) -> dict[str, Any] | None:
        # Body extracted to app.services.pipeline.real_estate_leads.maybe_create_real_estate_lead_from_call
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import maybe_create_real_estate_lead_from_call

        return await maybe_create_real_estate_lead_from_call(NokvoOneVoicePipeline, tenant_res, db, call_id, campaign_context=campaign_context, outbound_context=outbound_context)

    @staticmethod
    async def _patch_record_metadata(
        db: AsyncSession,
        record_id: Any,
        metadata: dict[str, Any],
    ) -> None:
        # Body extracted to app.services.pipeline.real_estate_leads._patch_record_metadata
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.real_estate_leads import _patch_record_metadata

        return await _patch_record_metadata(db, record_id, metadata)

    @staticmethod
    def _tool_flow_success_answer(result: dict[str, Any], args: dict[str, Any], *, flow_key: str, language: str | None, offer_sms: bool = False) -> str:
        # Body extracted to app.services.pipeline.tool_flows._tool_flow_success_answer
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.tool_flows import _tool_flow_success_answer

        return _tool_flow_success_answer(result, args, flow_key=flow_key, language=language, offer_sms=offer_sms)

    @staticmethod
    def _site_visit_hours_reprompt(
        *,
        requested_dt: datetime,
        suggestion_dt: datetime | None,
        defaults: Any,
        language: str | None,
    ) -> str:
        # Body extracted to app.services.pipeline.tool_flows._site_visit_hours_reprompt
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.tool_flows import _site_visit_hours_reprompt

        return _site_visit_hours_reprompt(requested_dt=requested_dt, suggestion_dt=suggestion_dt, defaults=defaults, language=language)

    @staticmethod
    async def _maybe_execute_tool_flow_action(
        tenant_res: TenantResources,
        call_id: str | None,
        db: AsyncSession | None,
        tool_flow: dict[str, Any],
        *,
        business_context: tuple[Organization, dict[str, Any], list[dict[str, Any]]] | None = None,
        language: str | None = None,
    ) -> dict[str, Any] | None:
        # Snapshot the FK primitive (see _maybe_execute_turn_policy_action) so the
        # fresh-session retry below can't force a sync ORM reload → MissingGreenlet.
        # Body extracted to app.services.pipeline.tool_flows._maybe_execute_tool_flow_action
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.tool_flows import _maybe_execute_tool_flow_action

        return await _maybe_execute_tool_flow_action(NokvoOneVoicePipeline, tenant_res, call_id, db, tool_flow, business_context=business_context, language=language)

    @staticmethod
    async def _apply_route_state(
        tenant_res: TenantResources,
        call_id: str | None,
        route: dict[str, Any],
    ) -> None:
        patch = route.get("state_patch") if isinstance(route, dict) else None
        if isinstance(patch, dict) and patch:
            # Real-estate inbound FSM: when the route's state_patch carries a
            # tool_flow update, re-derive agent_mode from the resulting state
            # and bake it into the patch so observability tools see the
            # transition without needing to recompute. Phase 1 — real-estate
            # only; non-real-estate orgs skip the augmentation.
            if "tool_flow" in patch:
                try:
                    from app.services.real_estate_agent_fsm import current_mode as _fsm_current_mode

                    # Approximate the post-patch state by overlaying the patch
                    # onto an empty state — current_mode only consults
                    # ``tool_flow`` fields, so a partial overlay is sufficient.
                    patch.setdefault(
                        "agent_mode",
                        _fsm_current_mode({"tool_flow": patch.get("tool_flow") or {}}),
                    )
                except Exception:
                    pass
            await AgentSessionStore.merge_state(tenant_res, call_id, patch)

    @staticmethod
    def _messages(
        query: str,
        chunks: list[dict[str, Any]],
        *,
        language: str,
        history: list[dict[str, str]],
        company_name: str | None = None,
        campaign_goal: str | None = None,
        single_prompt_guidance: str | None = None,
        outbound_context: OutboundCampaignContext | None = None,
        covered_objectives: list[str] | None = None,
        outbound_memory: dict[str, Any] | None = None,
        conversational_memory_block: str | None = None,
        conversation_strategy_block: str | None = None,
        field_questions_prompt: str | None = None,
        projects_block: str | None = None,
        services_block: str | None = None,
        tool_flow_state: dict[str, Any] | None = None,
        tool_flow_bundle: dict[str, Any] | None = None,
        turn_index: int | None = None,
        agent_mode_block: str | None = None,
        conversational_memory: Any = None,
        business_type: str | None = None,
    ) -> list[dict[str, str]]:
        # Body extracted to app.services.pipeline.message_composer.compose_rag_messages.
        # This wrapper preserves the class-method API surface so legacy callers
        # don't need updating. New code should import compose_rag_messages directly.
        from app.services.pipeline.message_composer import compose_rag_messages
        return compose_rag_messages(
            query,
            chunks,
            language=language,
            history=history,
            company_name=company_name,
            campaign_goal=campaign_goal,
            single_prompt_guidance=single_prompt_guidance,
            outbound_context=outbound_context,
            covered_objectives=covered_objectives,
            outbound_memory=outbound_memory,
            conversational_memory_block=conversational_memory_block,
            conversation_strategy_block=conversation_strategy_block,
            field_questions_prompt=field_questions_prompt,
            projects_block=projects_block,
            services_block=services_block,
            tool_flow_state=tool_flow_state,
            tool_flow_bundle=tool_flow_bundle,
            turn_index=turn_index,
            agent_mode_block=agent_mode_block,
            conversational_memory=conversational_memory,
            business_type=business_type,
        )


    @staticmethod
    def _messages_smalltalk(
        query: str,
        *,
        language: str,
        history: list[dict[str, str]],
        company_name: str | None = None,
        sentiment: str = "neutral",
        single_prompt_guidance: str | None = None,
    ) -> list[dict[str, str]]:
        # Body extracted to app.services.pipeline.message_composer.compose_smalltalk_messages.
        # Wrapper preserves the legacy class-method API surface.
        from app.services.pipeline.message_composer import compose_smalltalk_messages
        return compose_smalltalk_messages(
            query,
            language=language,
            history=history,
            company_name=company_name,
            sentiment=sentiment,
            single_prompt_guidance=single_prompt_guidance,
        )

    @staticmethod
    def _template_reply(intent: str, language: str, company_name: str | None) -> str | None:
        """Local, no-LLM canned replies for greeting/thanks/goodbye/smalltalk."""
        name = company_name or "Nokvo"
        if intent == INTENT_GREETING:
            return {
                "hi": f"नमस्ते, {name} सपोर्ट में आपका स्वागत है। मैं कैसे मदद कर सकता हूँ?",
                "ta": f"வணக்கம், {name} ஆதரவிற்கு வரவேற்கிறோம். எப்படி உதவலாம்?",
                "te": f"నమస్కారం, {name} సపోర్ట్‌కు స్వాగతం. ఎలా సహాయం చేయగలను?",
                "bn": f"নমস্কার, {name} সাপোর্টে স্বাগতম। কীভাবে সাহায্য করতে পারি?",
            }.get(language, f"Hi, thanks for calling {name}. How can I help?")
        if intent == INTENT_THANKS:
            return {
                "hi": "आपका स्वागत है। क्या मैं और कुछ मदद कर सकता हूँ?",
                "ta": "நன்றி. வேறு ஏதாவது உதவி வேண்டுமா?",
                "te": "ధన్యవాదాలు. ఇంకేమైనా సహాయం కావాలా?",
            }.get(language, "You're welcome. Anything else I can help with?")
        if intent == INTENT_GOODBYE:
            return {
                "hi": "धन्यवाद, अच्छा दिन हो।",
                "ta": "நன்றி, நல்ல நாள் ஆகட்டும்.",
                "te": "ధన్యవాదాలు, మంచి రోజు.",
            }.get(language, "Thanks for calling. Have a good day.")
        if intent == INTENT_SMALLTALK:
            return {
                "hi": "ठीक है, बताइए।",
                "ta": "சரி, சொல்லுங்கள்.",
                "te": "సరే, చెప్పండి.",
            }.get(language, "Sure, go ahead.")
        if intent == INTENT_AUDIO_CHECK:
            return {
                "hi": "जी हाँ, मैं आपको सुन सकता हूँ। बताइए, मैं कैसे मदद कर सकता हूँ?",
                "ta": "ஆம், என்னால் கேட்க முடிகிறது. எப்படி உதவலாம்?",
                "te": "అవును, వినగలుగుతున్నాను. ఎలా సహాయం చేయగలను?",
                "bn": "হ্যাঁ, আমি শুনতে পাচ্ছি। কীভাবে সাহায্য করতে পারি?",
            }.get(language, "Yeah, I can hear you. What can I help you with?")
        return None

    @staticmethod
    async def _caller_is_verified(
        tenant_res: TenantResources,
        db: AsyncSession | None,
        call_id: str | None,
        user_text: str,
    ) -> dict[str, Any]:
        """Return {"verified": bool, "challenged": bool, "challenge": str?} —
        true when the conversation history (or this turn) contains a phone
        that matches the contact_phone on an existing record for the org.
        ``challenged`` is True once we've already asked the caller to verify
        in this session, so we don't loop on the same challenge."""
        # Snapshot the FK primitive (see _maybe_execute_turn_policy_action) so the
        # query filter below can't force a sync ORM reload → MissingGreenlet.
        org_id = getattr(tenant_res, "organization_id")
        if db is None or call_id is None:
            # Without DB/session we can't gate; default to permitting (the
            # legacy behaviour) so we don't break the existing flow.
            return {"verified": True, "challenged": False}

        state = await AgentSessionStore.get_state(tenant_res, call_id) or {}
        if state.get("identity_verified"):
            return {"verified": True, "challenged": True}

        history = await AgentSessionStore.get_history(tenant_res, call_id) or []
        # Pull every phone-looking token from the conversation + this turn.
        from app.services.voice_turn_policy import normalize_phone_number

        phones: list[str] = []
        for turn in history[-12:]:
            if turn.get("role") != "user":
                continue
            phone = normalize_phone_number(str(turn.get("content") or ""), expected=True)
            if phone:
                phones.append(phone)
        this_phone = normalize_phone_number(user_text or "", expected=True)
        if this_phone:
            phones.append(this_phone)

        if not phones:
            return {
                "verified": False,
                "challenged": bool(state.get("identity_verification_pending")),
                "challenge": (
                    "Before I can change a booking, I need to verify you — "
                    "could you share the phone number the booking is under?"
                ),
            }

        from app.models.nokvo_one_tool_record import NokvoOneToolRecord
        from sqlalchemy import select

        stmt = (
            select(NokvoOneToolRecord)
            .where(NokvoOneToolRecord.organization_id == org_id)
            .order_by(NokvoOneToolRecord.created_at.desc())
            .limit(50)
        )
        try:
            res = await db.execute(stmt)
        except Exception:
            return {"verified": False, "challenged": True}

        suffixes = {p[-10:] for p in phones if len(p) >= 10}
        for rec in res.scalars().all():
            phone_value = "".join(c for c in str(rec.contact_phone or "") if c.isdigit())
            if not phone_value:
                data = rec.data or {}
                phone_value = "".join(c for c in str(data.get("phone") or data.get("contact_phone") or "") if c.isdigit())
            if phone_value and phone_value[-10:] in suffixes:
                await AgentSessionStore.set_state(
                    tenant_res, call_id, {"identity_verified": True}
                )
                return {"verified": True, "challenged": True}
        return {
            "verified": False,
            "challenged": True,
            "challenge": (
                "I couldn't match that number to a booking on file. "
                "Could you share the phone number used at booking?"
            ),
        }

    @staticmethod
    def _active_policy_cards(tenant_res: TenantResources) -> list[dict[str, Any]]:
        provider_status = dict(tenant_res.provider_status or {})
        return list(provider_status.get(AGENT_POLICY_CARDS_KEY) or [])

    @staticmethod
    async def _apply_clarification(
        tenant_res: TenantResources,
        call_id: str | None,
        *,
        turn_cache: dict[str, Any],
        user_text: str,
        route: str,
        intent: str | None,
        refused: bool,
        chunks: list[dict[str, Any]] | None,
        state_slot: str | None,
        language: str,
        original_answer: str,
    ) -> tuple[str, str | None, ClarificationState]:
        """Apply the clarification FSM to a freshly-completed turn.

        Returns a tuple ``(answer, action, state)`` where:

        * ``answer`` is the final caller-facing text. When the FSM has
          escalated, the original ``original_answer`` is replaced with
          the matching multilingual prompt (options menu or handoff).
        * ``action`` is the FSM verdict (``CLARIFY_RESET`` /
          ``CLARIFY_NUDGE`` / ``CLARIFY_OFFER_OPTIONS`` /
          ``CLARIFY_ESCALATE``) for logging.
        * ``state`` is the updated :class:`ClarificationState` to be
          persisted back to the session blob.

        The FSM is hydrated from ``turn_cache["state"]`` (already loaded
        by ``_prime_turn_cache``) so this method never adds an extra
        Redis round-trip.
        """
        session_state = dict(turn_cache.get("state") or {})
        state = ClarificationState.from_dict(session_state.get("clarification"))
        vague = is_turn_vague(
            route=route,
            intent=intent,
            refused=refused,
            chunks=chunks,
            user_text=user_text,
            state_slot=state_slot,
        )
        if not vague:
            if state.consecutive_vague_turns:
                state.reset()
                if call_id:
                    await AgentSessionStore.merge_state(
                        tenant_res, call_id, {"clarification": state.to_dict()}
                    )
            return original_answer, CLARIFY_RESET, state

        # The vagueness heuristic fired, but if the agent still produced a
        # SUBSTANTIVE reply this turn (e.g. the LLM actually answered / listed
        # projects), the turn wasn't a failure — keep that answer and reset the
        # counter. We must never throw a real answer away and replace it with a
        # generic options menu; that's the agent "going dumb" mid-answer.
        produced_non_answer = refused or len((original_answer or "").split()) < 4
        if not produced_non_answer:
            if state.consecutive_vague_turns:
                state.reset()
                if call_id:
                    await AgentSessionStore.merge_state(
                        tenant_res, call_id, {"clarification": state.to_dict()}
                    )
            return original_answer, CLARIFY_RESET, state

        state.bump(user_text)
        action = state.action()
        # NUDGE: leave the existing ``original_answer`` (open-question /
        # refusal). OFFER_OPTIONS + ESCALATE override the answer so the caller
        # hears something concrete instead of the third "sorry, I missed that".
        answer = original_answer
        if action in (CLARIFY_OFFER_OPTIONS, CLARIFY_ESCALATE):
            answer = clarification_prompt(action, language)
        if call_id:
            await AgentSessionStore.merge_state(
                tenant_res, call_id, {"clarification": state.to_dict()}
            )
        return answer, action, state

    @staticmethod
    def _log_route(route_payload: dict[str, Any]) -> None:
        """Single structured log line per turn. Customer responses are never
        included here; only routing metadata for observability."""
        if not settings.AGENT_RAG_DEBUG:
            # In non-debug mode, still emit a compact log so we can see route
            # decisions in production logs without leaking retrieved text.
            compact = {
                k: v
                for k, v in route_payload.items()
                if k in {"tenant_id", "call_id", "intent", "topic", "route", "sensitive",
                         "cache_hit", "qdrant_called", "llm_called", "policy_card_id",
                         "decision_code", "ttfb_ms", "total_ms", "single_prompt_enabled",
                         "detected_entities", "state_slot", "route_reason"}
            }
            # Surface the LLM classifier's intent so we can debug "why didn't
            # RAG fire" without enabling full debug mode.
            cls = route_payload.get("classified") or {}
            if isinstance(cls, dict) and cls:
                compact["llm_intent"] = cls.get("intent")
                compact["llm_needs_kb"] = cls.get("needs_kb")
                compact["llm_fallback"] = cls.get("fallback")
            logger.warning(f"NOKVO-AGENT-ROUTE: {compact}")
            return
        logger.warning(f"NOKVO-AGENT-ROUTE-DEBUG: {route_payload}")

    @staticmethod
    def _cancel_retrieval_task(task: asyncio.Task | None) -> None:
        if not task:
            return
        if task.done():
            try:
                task.exception()
            except BaseException:
                pass
            return
        task.cancel()

    @staticmethod
    async def _mark_appointment_deferred(
        tenant_res: TenantResources,
        call_id: str | None,
        prior_appointment: dict[str, Any],
    ) -> None:
        """Mark an in-progress booking as deferred for a knowledge-base digression.

        The slot-fill FSM consumes this flag on its next turn to prefix the
        resumed slot question with a "Coming back to your booking — " phrase.
        Re-yielding before the FSM resumes is idempotent: the flag stays True.
        """
        if not call_id or not prior_appointment.get("active"):
            return
        patch = {"appointment": {**prior_appointment, "deferred_for_kb": True}}
        try:
            await AgentSessionStore.merge_state(tenant_res, call_id, patch)
        except Exception as exc:
            # Don't let a state-store hiccup poison the route; the user just
            # won't get the "Coming back" prefix on the next turn.
            logger.warning(f"NOKVO-ROUTE: failed to mark appointment deferred: {exc!r}")

    @staticmethod
    async def _mark_tool_flow_deferred(
        tenant_res: TenantResources,
        call_id: str | None,
        prior_tool_flow: dict[str, Any],
    ) -> None:
        """Same deferred-for-kb mechanism as appointments, for tool_flow.

        When the caller pivots to a KB question mid tool_flow (leads_create,
        real_estate_site_visit, etc.), the policy returns None and the next
        slot question is prefixed with "Coming back to your booking — ".
        """
        if not call_id or not prior_tool_flow.get("active"):
            return
        patch = {"tool_flow": {**prior_tool_flow, "deferred_for_kb": True}}
        try:
            await AgentSessionStore.merge_state(tenant_res, call_id, patch)
        except Exception as exc:
            logger.warning(f"NOKVO-ROUTE: failed to mark tool_flow deferred: {exc!r}")

    @staticmethod
    async def _llm_check_booking_digression(
        tenant_res: TenantResources,
        user_text: str,
        history: list[dict[str, str]],
    ) -> Any | None:
        """Final-guard digression check via the small LLM classifier.

        Called only when the FSM is about to re-ask the same slot — which is
        a strong signal that the caller's input wasn't a slot answer. Returns
        the ClassifiedIntent when the caller clearly pivoted (kb_question,
        complaint, escalation, cancel/refund, out_of_scope); returns ``None``
        when the classifier timed out, errored, or said "this still looks
        like booking input" (smalltalk / order_status / unclear).
        """
        try:
            result = await LLMIntentClassifier.classify(
                user_text,
                tenant_res=tenant_res,
                history=history,
                # The classifier's measured latency is 1.5-2.4s (non-streaming;
                # on a reasoning-model fallback it can NEVER finish in 500ms —
                # the hardcoded 500 here timed out on EVERY turn in prod).
                timeout_ms=int(settings.NOKVO_INTENT_CLASSIFIER_TIMEOUT_MS or 2500),
            )
        except Exception as exc:
            logger.warning(f"NOKVO-DIGRESSION: classifier failed: {exc!r}")
            return None
        if result.fallback:
            return None
        if result.intent in {
            LLM_INTENT_KB,
            LLM_INTENT_COMPLAINT,
            LLM_INTENT_ESCALATION,
            LLM_INTENT_CANCEL,
            LLM_INTENT_REFUND,
            LLM_INTENT_OUT_OF_SCOPE,
        }:
            return result
        return None

    @staticmethod
    async def _turn_history(
        tenant_res: TenantResources,
        call_id: str | None,
        turn_cache: dict[str, Any],
    ) -> list[dict[str, str]]:
        """Cached ``get_history``. The first call within a turn fetches from
        Redis; later calls within the same turn return the in-memory copy.
        This collapses the previous 2-3 Redis GETs per turn into one."""
        cached = turn_cache.get("history")
        if cached is not None:
            return list(cached)
        history = await AgentSessionStore.get_history(tenant_res, call_id)
        turn_cache["history"] = list(history)
        return list(history)

    @staticmethod
    async def _turn_state(
        tenant_res: TenantResources,
        call_id: str | None,
        turn_cache: dict[str, Any],
    ) -> dict[str, Any]:
        """Cached ``get_state``. See :meth:`_turn_history`."""
        cached = turn_cache.get("state")
        if cached is not None:
            return dict(cached)
        state = await AgentSessionStore.get_state(tenant_res, call_id)
        turn_cache["state"] = dict(state or {})
        return dict(state or {})

    @staticmethod
    async def _turn_bundle(
        db: AsyncSession | None,
        tenant_res: TenantResources,
        turn_cache: dict[str, Any],
    ) -> RuntimeBundle | None:
        """Cached runtime-bundle resolver. Shared by the inline ``industry``
        lookup, ``_voice_business_context``, and any other helper that
        needs tenant-stable state within the same turn.

        Returns ``None`` if the bundle can't be loaded (e.g., partial
        ``tenant_res`` test stub without ``organization_id``, or a db that
        can't execute). Callers must default safely on ``None``.
        """
        cached = turn_cache.get("bundle")
        if cached is not None:
            return cached
        if db is None or getattr(tenant_res, "organization_id", None) is None:
            return None
        try:
            bundle = await get_runtime_bundle(db, tenant_res)
        except Exception:
            return None
        turn_cache["bundle"] = bundle
        return bundle

    @staticmethod
    async def prime_prefix_cache(
        tenant_res: TenantResources,
        *,
        language: str,
        call_id: str | None,
        company_name: str | None = None,
        outbound_context: "OutboundCampaignContext | None" = None,
    ) -> None:
        """Warm the provider prompt cache for this call's STATIC PREFIX (WS2).

        The inbound system prompt is split into a byte-identical static prefix
        (tenant + per-language style/few-shots) and a per-turn dynamic suffix;
        the provider caches the prefix, cutting first-token latency on every
        turn AFTER the first. The sticky LLM-pool routing (set_call_id) keeps a
        call pinned to one deployment, so once warm it stays warm — but the
        FIRST turn in each language still pays full TTFT on the +500-1000 prompt
        tokens Hindi/Telugu carry. This fires a tiny (max_tokens=1) fire-and-
        forget completion at call start with exactly that prefix so turn 1 also
        hits cache.

        Fully best-effort: opens its OWN short-lived DB session (never the
        call's shared session — that isn't concurrency-safe) and swallows every
        error. Runs under the caller's set_call_id context so it reserves the
        same sticky home box the real turns will use.
        """
        try:
            from app.db.session import AsyncSessionLocal
            from app.services.llm_pool import LLMPoolClient

            language = SarvamVoiceService.normalize_language(language)
            outbound_mode = bool(outbound_context) and getattr(outbound_context, "is_proactive", False)
            async with AsyncSessionLocal() as db:
                turn_cache = await NokvoOneVoicePipeline._prime_turn_cache(db, tenant_res, call_id)
                bundle = turn_cache.get("bundle")
                single_prompt_guidance = bundle.single_prompt_guidance if bundle is not None else None
                business_type = bundle.organization_industry if bundle is not None else None
                projects_block = ""
                services_block = ""
                if bundle is not None and not outbound_mode:
                    projects_block, _ = await NokvoOneVoicePipeline._projects_block_for_bundle(db, bundle)
                    services_block = await NokvoOneVoicePipeline._services_block_for_bundle(db, bundle)
                # Build the messages exactly as the first real turn would, then
                # warm ONLY the static prefix (messages[0]) — the provider cache
                # matches on the longest common token prefix, which is precisely
                # that system block. The dynamic suffix/history differ per turn
                # and aren't worth priming.
                messages = NokvoOneVoicePipeline._messages(
                    ".",
                    [],
                    language=language,
                    history=[],
                    company_name=company_name,
                    single_prompt_guidance=single_prompt_guidance,
                    outbound_context=outbound_context,
                    projects_block=projects_block,
                    services_block=services_block,
                    business_type=business_type,
                )
            static_prefix = (messages[0] or {}).get("content") if messages else None
            if not static_prefix:
                return
            await LLMPoolClient.chat(
                [
                    {"role": "system", "content": static_prefix},
                    {"role": "user", "content": "."},
                ],
                max_tokens=1,
                temperature=0.0,
            )
        except Exception:
            logger.debug("prime_prefix_cache: best-effort warm failed", exc_info=True)

    @staticmethod
    async def _prime_turn_cache(
        db: AsyncSession | None,
        tenant_res: TenantResources,
        call_id: str | None,
    ) -> dict[str, Any]:
        """Run the three independent turn-startup fetches concurrently and
        return a ``turn_cache`` dict pre-populated with all three results.

        Previously the pipeline called ``get_history``, ``get_state`` and
        ``_voice_business_context`` serially at the top of the turn, which
        added together roughly = (history latency) + (state latency) +
        (organization DB roundtrip). With this primer, total turn-startup
        latency drops to ``max(...)`` of the three — usually the bundle
        load on a cold tenant, ~5 ms on a warm one.
        """
        history_task = asyncio.create_task(AgentSessionStore.get_history(tenant_res, call_id))
        state_task = asyncio.create_task(AgentSessionStore.get_state(tenant_res, call_id))
        bundle_task = asyncio.create_task(get_runtime_bundle(db, tenant_res))
        history_res, state_res, bundle_res = await asyncio.gather(
            history_task, state_task, bundle_task, return_exceptions=True
        )
        # Degrade gracefully: a transient Redis/DB hiccup on the primer must
        # not crash the turn. Fall back to empty history/state and let bundle
        # load lazily later if it failed here.
        history = history_res if not isinstance(history_res, BaseException) else []
        state = state_res if not isinstance(state_res, BaseException) else {}
        if isinstance(bundle_res, BaseException):
            bundle = None
        else:
            bundle = bundle_res
        return {
            "history": list(history or []),
            "state": dict(state or {}),
            "bundle": bundle,
        }

    @staticmethod
    async def _await_prefetched_retrieval(route: dict[str, Any]) -> dict[str, Any] | None:
        retrieval = route.get("prefetched_retrieval") if isinstance(route, dict) else None
        if isinstance(retrieval, asyncio.Task):
            try:
                return await retrieval
            except asyncio.CancelledError:
                return None
            except Exception:
                import logging
                logging.getLogger(__name__).warning(
                    "prefetched retrieval failed", exc_info=True
                )
                return None
        if isinstance(retrieval, dict):
            return retrieval
        return None

    @staticmethod
    async def _route_turn(
        tenant_res: TenantResources,
        user_text: str,
        *,
        language: str,
        company_name: str | None,
        call_id: str | None,
        english_text: str | None = None,
        db: AsyncSession | None = None,
        top_k: int | None = None,
        campaign_id: str | None = None,
        turn_cache: dict[str, Any] | None = None,
        code_switching: bool = False,
        outbound_context: OutboundCampaignContext | None = None,
    ) -> dict[str, Any]:
        # Body extracted to app.services.pipeline.turn_router.route_turn.
        # The wrapper preserves the @staticmethod API so the 5 call sites
        # in this file + any external monkeypatches in tests keep working.
        # The router calls back into the orchestrator class for the 21
        # static helpers it depends on (_template_reply, _turn_state, etc.)
        # by accepting helpers as its first positional argument.
        from app.services.pipeline.turn_router import route_turn

        return await route_turn(
            NokvoOneVoicePipeline,
            tenant_res,
            user_text,
            language=language,
            company_name=company_name,
            call_id=call_id,
            english_text=english_text,
            db=db,
            top_k=top_k,
            campaign_id=campaign_id,
            turn_cache=turn_cache,
            code_switching=code_switching,
            outbound_context=outbound_context,
        )
    @staticmethod
    async def answer_text(
        tenant_res: TenantResources,
        query: str,
        *,
        db: AsyncSession | None = None,
        top_k: int | None = None,
        latency_budget_ms: int | None = None,
        response_language: str | None = None,
        conversation_history: list[dict[str, str]] | None = None,
        call_id: str | None = None,
        retrieval_text: str | None = None,
        campaign_id: str | None = None,
        campaign_goal: str | None = None,
        company_name: str | None = None,
        outbound_context: OutboundCampaignContext | None = None,
    ) -> dict[str, Any]:
        # Body extracted to app.services.pipeline.answer_flow.answer_text
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.answer_flow import answer_text

        return await answer_text(NokvoOneVoicePipeline, tenant_res, query, db=db, top_k=top_k, latency_budget_ms=latency_budget_ms, response_language=response_language, conversation_history=conversation_history, call_id=call_id, retrieval_text=retrieval_text, campaign_id=campaign_id, campaign_goal=campaign_goal, company_name=company_name, outbound_context=outbound_context)

    @staticmethod
    def _field_questions_prompt_for_bundle(
        bundle: "RuntimeBundle",
        *,
        language: str,
        project_names: list[str] | None = None,
    ) -> str:
        # Body extracted to app.services.pipeline.prompt_blocks._field_questions_prompt_for_bundle
        # (turn_router helpers pattern). The wrapper preserves the
        # @staticmethod API for call sites and class-attribute monkeypatches.
        from app.services.pipeline.prompt_blocks import _field_questions_prompt_for_bundle

        return _field_questions_prompt_for_bundle(bundle, language=language, project_names=project_names)

    @staticmethod
    async def stream_answer_sentences(
        tenant_res: TenantResources,
        query: str,
        *,
        db: AsyncSession | None = None,
        top_k: int | None = None,
        response_language: str | None = None,
        call_id: str | None = None,
        retrieval_text: str | None = None,
        campaign_id: str | None = None,
        campaign_goal: str | None = None,
        company_name: str | None = None,
        code_switching: bool = False,
        outbound_context: OutboundCampaignContext | None = None,
        covered_objectives: list[str] | None = None,
        outbound_memory: dict[str, Any] | None = None,
        conversational_memory: Any = None,
    ) -> AsyncIterator[dict[str, Any]]:
        # Body extracted to app.services.pipeline.answer_flow.stream_answer_sentences
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.pipeline.answer_flow import stream_answer_sentences

        return await stream_answer_sentences(NokvoOneVoicePipeline, tenant_res, query, db=db, top_k=top_k, response_language=response_language, call_id=call_id, retrieval_text=retrieval_text, campaign_id=campaign_id, campaign_goal=campaign_goal, company_name=company_name, code_switching=code_switching, outbound_context=outbound_context, covered_objectives=covered_objectives, outbound_memory=outbound_memory, conversational_memory=conversational_memory)

    @staticmethod
    async def latency_test(
        tenant_res: TenantResources,
        query: str,
        *,
        db: AsyncSession | None = None,
        target_ms: int = 800,
        response_language: str | None = None,
    ) -> dict[str, Any]:
        started = perf_counter()
        result = await NokvoOneVoicePipeline.answer_text(
            tenant_res,
            query,
            db=db,
            top_k=settings.AGENT_RETRIEVAL_TOP_K,
            latency_budget_ms=min(settings.AGENT_LLM_TIMEOUT_MS, max(200, target_ms - 120)),
            response_language=response_language,
        )
        answer_ms = int((perf_counter() - started) * 1000)
        tts_started = perf_counter()
        try:
            tts = await SarvamVoiceService.synthesize(
                tenant_res,
                result.get("answer") or "",
                language=response_language,
            )
            first_audio_ms = int((perf_counter() - tts_started) * 1000) if tts.get("audios") else None
            tts_probe = {"status": "passed" if first_audio_ms and first_audio_ms <= target_ms else "slow", "first_audio_ms": first_audio_ms}
        except Exception as exc:
            first_audio_ms = None
            tts_probe = {"status": "failed", "error_message": str(exc)[:200]}
        return {
            **result,
            "latency": {
                "target_ms": target_ms,
                "final_transcript_to_answer_ms": answer_ms,
                "answer_text_to_first_tts_audio_ms": first_audio_ms,
                "final_transcript_to_first_tts_audio_ms": answer_ms + first_audio_ms if first_audio_ms is not None else None,
                "tts_first_audio_passed": bool(first_audio_ms is not None and first_audio_ms <= target_ms),
                "passed": bool(first_audio_ms is not None and first_audio_ms <= target_ms),
                "measurement": "server-side Nokvo RAG answer text produced to first Sarvam TTS audio chunk",
                "tts_probe": tts_probe,
            },
        }

    @staticmethod
    def runtime_status(tenant_res: TenantResources) -> dict[str, Any]:
        provider_status = dict(tenant_res.provider_status or {})
        single_prompt_config = provider_status.get(AGENT_SINGLE_PROMPT_CONFIG_KEY) or {}
        single_prompt_enabled = bool(
            isinstance(single_prompt_config, dict)
            and single_prompt_config.get("enabled")
            and single_prompt_config.get("prompt")
        )
        return {
            "runtime": "nokvo_one_voice_agent",
            "graph": "nokvo_rag_pipeline",
            "knowledge_scope": "pre_indexed_tenant_qdrant_collection",
            "setup_mode": "document_based_with_single_prompt" if single_prompt_enabled else "document_based",
            "single_prompt_voice_agent": {
                "enabled": single_prompt_enabled,
                "updated_at": single_prompt_config.get("updated_at")
                if isinstance(single_prompt_config, dict)
                else None,
            },
            "intent_gating": False,
            "brains": ["retrieval_grounded_pipeline", "semantic_cache", "session_memory"],
            "optimization": {
                "ingestion_off_hot_path": True,
                "semantic_cache_enabled": settings.AGENT_ANSWER_CACHE_ENABLED,
                "sentence_level_tts": True,
                "streamed_llm": True,
                "qdrant_top_k": settings.AGENT_RETRIEVAL_TOP_K,
                "policy_version": _agent_policy_version(tenant_res),
            },
            "supported_indian_languages": SARVAM_LANGUAGE_OPTIONS,
            "stt": {
                "provider": "sarvam",
                "endpoint": provider_status.get("sarvam_stt_ws_url") or settings.SARVAM_STT_WEBSOCKET_URL,
                "model": provider_status.get("sarvam_stt_model") or settings.SARVAM_STT_MODEL,
                "mode": provider_status.get("sarvam_stt_mode") or settings.SARVAM_STT_MODE,
                "sample_rate": provider_status.get("stt_sample_rate") or settings.SARVAM_STT_SAMPLE_RATE,
                "status": "configured" if settings.SARVAM_API_KEY or provider_status.get("sarvam_api_key_ref") else "missing_api_key",
            },
            "llm": {
                "provider": "azure_openai",
                "model": settings.AZURE_OPENAI_AGENT_MODEL,
                "deployment": provider_status.get("llm_deployment") or settings.AZURE_OPENAI_AGENT_DEPLOYMENT,
                "latency_budget_ms": settings.AGENT_LLM_TIMEOUT_MS,
            },
            "tts": {
                "provider": "sarvam",
                "endpoint": provider_status.get("sarvam_tts_rest_url") or settings.SARVAM_TTS_REST_URL,
                "stream_endpoint": provider_status.get("sarvam_tts_ws_url") or settings.SARVAM_TTS_WEBSOCKET_URL,
                "model": provider_status.get("sarvam_tts_model") or settings.SARVAM_TTS_MODEL,
                "voice": provider_status.get("sarvam_tts_speaker") or settings.SARVAM_TTS_SPEAKER,
                "sample_rate": provider_status.get("tts_sample_rate") or settings.SARVAM_TTS_SAMPLE_RATE,
                "audio_format": settings.SARVAM_TTS_AUDIO_CODEC,
                "status": "configured" if settings.SARVAM_API_KEY or provider_status.get("sarvam_api_key_ref") else "missing_api_key",
            },
        }


# Backward-compatible service name for existing imports while the active logic is
# now the Nokvo One RAG/Sarvam architecture.
AgentRuntimeService = NokvoOneVoicePipeline
