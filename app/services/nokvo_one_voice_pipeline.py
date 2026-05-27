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
from app.services.agent_knowledge_service import (
    AGENT_CHUNK_SOURCE_KIND,
    AGENT_KNOWLEDGE_SOURCE_TYPE,
    AGENT_POLICY_CARDS_KEY,
    AGENT_SINGLE_PROMPT_CONFIG_KEY,
    AgentKnowledgeService,
)
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
from app.services.qdrant_service import QdrantService
from app.services.language_style import (
    outbound_fewshot as language_outbound_fewshot,
    style_guidance as language_style_guidance,
)
from app.services.sarvam_voice_service import SARVAM_LANGUAGE_OPTIONS, SarvamVoiceService
from app.services.text_embedding_service import TextEmbeddingService
from app.services.tool_flow_policy import evaluate_tool_flow_policy
from app.services.tool_flow_questions import build_tool_flow_questions, format_field_questions_prompt
from app.services.voice_turn_policy import evaluate_voice_turn_policy


_SENTENCE_RE = re.compile(r"(?<=[.!?।])\s+")
_SMALLTALK_RE = re.compile(
    r"^(hi|hello|hey|namaste|namaskar|thanks?|thank you|okay|ok|bye|goodbye|good morning|good evening)[\s!.?]*$",
    re.IGNORECASE,
)
_SENSITIVE_OR_DYNAMIC_RE = re.compile(
    r"\b(order|ticket|payment|paid|refund status|account|phone|email|address|otp|password|card|upi|bank|delete|cancel my)\b",
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
_APPOINTMENT_LOCAL_TZ = ZoneInfo("Asia/Kolkata")
_MONTH_INDEX = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}
_WEEKDAY_INDEX = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


class _AppointmentToolInputError(ValueError):
    def __init__(self, slot: str, answer: str, *, clear_time: bool = False, clear_date: bool = False):
        super().__init__(answer)
        self.slot = slot
        self.answer = answer
        self.clear_time = clear_time
        self.clear_date = clear_date


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip())


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


class NokvoOneAgentRuntimeError(RuntimeError):
    pass


class NokvoOneAgentRateLimited(NokvoOneAgentRuntimeError):
    """Raised when Azure OpenAI returns 429. Caller should emit a graceful
    'busy, try again' response rather than the generic refusal."""

    def __init__(self, message: str, retry_after_seconds: float | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class AzureGroundedLLM:
    _client: httpx.AsyncClient | None = None

    @classmethod
    def http(cls) -> httpx.AsyncClient:
        if cls._client is None or cls._client.is_closed:
            cls._client = httpx.AsyncClient(
                timeout=httpx.Timeout(max(8.0, settings.AGENT_LLM_STREAM_TOTAL_MS / 1000)),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return cls._client

    @staticmethod
    async def api_key(tenant_res: TenantResources) -> str:
        provider_status = dict(tenant_res.provider_status or {})
        for key in ("llm_api_key_ref", "llm_api_key_secret_ref"):
            ref = provider_status.get(key)
            if not ref:
                continue
            try:
                secret = await AzureKeyVaultService.get_secret_value(ref)
            except Exception:
                secret = None
            if secret:
                return secret
        secret_ref = ((tenant_res.secret_refs or {}).get("llm_api_key") or {}).get("secret_name")
        if secret_ref:
            try:
                secret = await AzureKeyVaultService.get_secret_value(secret_ref)
            except Exception:
                secret = None
            if secret:
                return secret
        if settings.AZURE_OPENAI_GLOBAL_API_KEY:
            return settings.AZURE_OPENAI_GLOBAL_API_KEY
        raise NokvoOneAgentRuntimeError("Azure OpenAI API key is not configured for Nokvo One agent runtime.")

    @staticmethod
    def endpoint_and_body(
        tenant_res: TenantResources,
        messages: list[dict[str, str]],
        *,
        stream: bool = False,
        max_tokens: int = 180,
    ) -> tuple[str, dict[str, Any]]:
        provider_status = dict(tenant_res.provider_status or {})
        endpoint = str(provider_status.get("llm_endpoint") or settings.AZURE_OPENAI_GLOBAL_ENDPOINT or "").rstrip("/")
        if not endpoint:
            raise NokvoOneAgentRuntimeError("Azure OpenAI endpoint is not configured for Nokvo One agent runtime.")
        deployment = str(
            provider_status.get("llm_deployment")
            or provider_status.get("deployment_name")
            or settings.AZURE_OPENAI_AGENT_DEPLOYMENT
            or "gpt-4-1-mini"
        ).strip()
        if endpoint.endswith("/responses"):
            body: dict[str, Any] = {
                "model": settings.AZURE_OPENAI_AGENT_MODEL or deployment,
                "input": messages,
                "temperature": 0.2,
                "max_output_tokens": max_tokens,
            }
            if stream:
                body["stream"] = True
            return endpoint, body

        api_version = urllib_parse.quote(settings.AZURE_OPENAI_AGENT_API_VERSION.strip())
        deployment_path = urllib_parse.quote(deployment)
        if "/openai/deployments/" in endpoint:
            url = f"{endpoint}?api-version={api_version}" if "api-version=" not in endpoint else endpoint
        else:
            url = f"{endpoint}/openai/deployments/{deployment_path}/chat/completions?api-version={api_version}"
        body = {
            "messages": messages,
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        if stream:
            body["stream"] = True
        return url, body

    @staticmethod
    def extract_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str):
            return payload["output_text"].strip()
        choices = payload.get("choices") or []
        if choices:
            content = ((choices[0] or {}).get("message") or {}).get("content")
            if isinstance(content, str):
                return content.strip()
        parts: list[str] = []
        for item in payload.get("output") or []:
            for content in item.get("content") or []:
                text = content.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts).strip()

    @staticmethod
    async def complete(tenant_res: TenantResources, messages: list[dict[str, str]], *, max_tokens: int = 180) -> str:
        api_key = await AzureGroundedLLM.api_key(tenant_res)
        url, body = AzureGroundedLLM.endpoint_and_body(tenant_res, messages, max_tokens=max_tokens)
        attempts = 4
        last_response = None
        for attempt in range(attempts):
            response = await AzureGroundedLLM.http().post(
                url,
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json=body,
            )
            last_response = response
            if response.status_code != 429:
                break
            retry_after_hdr = response.headers.get("retry-after", "")
            try:
                retry_after = float(retry_after_hdr) if retry_after_hdr else 0.0
            except ValueError:
                retry_after = 0.0
            if attempt < attempts - 1:
                wait_for = retry_after if retry_after > 0 else 0.6 * (2 ** attempt)
                wait_for = min(wait_for, 3.5)
                print(
                    f"[NOKVO-LLM] 429 (complete) attempt {attempt + 1}/{attempts} — sleeping {wait_for:.2f}s "
                    f"(retry_after={retry_after_hdr!r})"
                )
                await asyncio.sleep(wait_for)
                continue
            logger.warning(f"NOKVO-LLM: 429 (complete) — giving up after {attempt + 1} attempt(s); retry_after={retry_after_hdr!r}")
            raise NokvoOneAgentRateLimited(
                f"Azure OpenAI rate-limited (429): {response.text[:300]}",
                retry_after_seconds=retry_after or None,
            )
        response = last_response
        if response.status_code >= 400:
            raise NokvoOneAgentRuntimeError(f"Azure OpenAI request failed ({response.status_code}): {response.text[:300]}")
        return AzureGroundedLLM.extract_text(response.json())

    @staticmethod
    async def stream_prosody(
        tenant_res: TenantResources,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 180,
        retry_attempts: int | None = None,
        max_retry_wait_s: float | None = None,
    ) -> AsyncIterator[ProsodyChunk]:
        """Stream prosody-tagged sentence chunks.

        The LLM is instructed (via the system prompt) to emit inline tone
        tags like ``[empathy]…[/empathy]`` around each phrase. The parser
        strips the tags and yields ``(text, tone)`` chunks aligned to
        sentence boundaries so TTS can pick matching pace/pitch/loudness.
        """
        async for chunk in stream_prosody_chunks(
            AzureGroundedLLM.stream(
                tenant_res,
                messages,
                max_tokens=max_tokens,
                retry_attempts=retry_attempts,
                max_retry_wait_s=max_retry_wait_s,
            )
        ):
            yield chunk

    @staticmethod
    async def stream(
        tenant_res: TenantResources,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 180,
        retry_attempts: int | None = None,
        max_retry_wait_s: float | None = None,
    ) -> AsyncIterator[str]:
        api_key = await AzureGroundedLLM.api_key(tenant_res)
        url, body = AzureGroundedLLM.endpoint_and_body(
            tenant_res,
            messages,
            stream=True,
            max_tokens=max_tokens,
        )
        # Azure OpenAI per-tenant deployments often have low TPM/RPM. In
        # interactive testing the user fires several turns in quick succession
        # and trips the quota — the agent then says "Give me a second, I'm a
        # bit busy" which sounds like Sarvam crashed but is actually Azure LLM.
        # Be more patient: up to 4 attempts, honor Retry-After up to 3.5s,
        # and fall back to an exponential 0.6 / 1.2 / 2.4s wait when Azure
        # doesn't tell us how long.
        attempts = max(1, int(retry_attempts or 4))
        retry_wait_cap = 3.5 if max_retry_wait_s is None else max(0.0, float(max_retry_wait_s))
        for attempt in range(attempts):
            async with AzureGroundedLLM.http().stream(
                "POST",
                url,
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json=body,
            ) as response:
                if response.status_code == 429:
                    retry_after_hdr = response.headers.get("retry-after", "")
                    try:
                        retry_after = float(retry_after_hdr) if retry_after_hdr else 0.0
                    except ValueError:
                        retry_after = 0.0
                    body_text = (await response.aread()).decode("utf-8", errors="replace")[:300]
                    if attempt < attempts - 1:
                        wait_for = retry_after if retry_after > 0 else 0.6 * (2 ** attempt)
                        wait_for = min(wait_for, retry_wait_cap)
                        print(
                            f"[NOKVO-LLM] 429 attempt {attempt + 1}/{attempts} — sleeping {wait_for:.2f}s "
                            f"(retry_after={retry_after_hdr!r})"
                        )
                        await asyncio.sleep(wait_for)
                        continue
                    logger.warning(f"NOKVO-LLM: 429 — giving up after {attempt + 1} attempt(s); retry_after={retry_after_hdr!r}")
                    raise NokvoOneAgentRateLimited(
                        f"Azure OpenAI rate-limited (429): {body_text}",
                        retry_after_seconds=retry_after or None,
                    )
                if response.status_code >= 400:
                    text = await response.aread()
                    raise NokvoOneAgentRuntimeError(f"Azure OpenAI stream failed ({response.status_code}): {text[:300]!r}")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    raw = line[6:]
                    if raw.strip() == "[DONE]":
                        return
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if event.get("type") == "response.output_text.delta":
                        token = event.get("delta") or ""
                        if token:
                            yield token
                        continue
                    choices = event.get("choices") or []
                    if choices:
                        token = ((choices[0].get("delta") or {}).get("content")) or ""
                        if token:
                            yield token
                return  # successful stream — exit the retry loop


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
        if _SENSITIVE_OR_DYNAMIC_RE.search(query or ""):
            return False
        for chunk in chunks:
            metadata = dict(chunk.get("metadata") or {})
            if metadata.get("sensitivity") == "sensitive":
                return False
        return bool(answer.strip())

    @staticmethod
    def _map_point(point: Any) -> dict[str, Any]:
        payload = dict(getattr(point, "payload", {}) or {})
        return {
            "document_id": str(payload.get("document_id") or ""),
            "document_name": str(payload.get("document_name") or payload.get("source_title") or "Document"),
            "chunk_id": str(payload.get("chunk_id") or getattr(point, "id", "")),
            "text": str(payload.get("text") or ""),
            "score": float(getattr(point, "score", 0.0) or 0.0),
            "metadata": {
                "source_type": payload.get("source_type"),
                "source_kind": payload.get("source_kind"),
                "document_type": payload.get("document_type"),
                "status": payload.get("status"),
                "document_status": payload.get("document_status"),
                "language": payload.get("language"),
                "campaign_id": payload.get("campaign_id"),
                "topic": payload.get("topic"),
                "sensitivity": payload.get("sensitivity"),
                "source_title": payload.get("source_title"),
                "section_id": payload.get("section_id"),
                "section_title": payload.get("section_title"),
                "parent_section_text": payload.get("parent_section_text"),
            },
        }

    @staticmethod
    def _chunks_from_outbound_doc(
        outbound_context: OutboundCampaignContext | None,
    ) -> list[dict[str, Any]]:
        """Materialize the campaign-supplied brief as retrieval chunks.

        Outbound is a different agent from inbound — its only data source
        is whatever the operator pinned into the campaign config (the
        ``doc_text`` field on :class:`OutboundCampaignContext`). We
        return Qdrant-shaped chunks so the existing prompt builder
        composes them the same way it does inbound retrievals. The
        agent_prompt rides through the separate outbound system fragment
        and does not need to be a chunk.
        """
        if outbound_context is None:
            return []
        text = (getattr(outbound_context, "doc_text", "") or "").strip()
        if not text:
            return []
        # Split into ~350-word chunks so a long campaign brief doesn't
        # blow the context window on a single LLM call. The reader sees
        # them as ordered excerpts from "Campaign Brief".
        words_per_chunk = 350
        words = text.split()
        out: list[dict[str, Any]] = []
        for i in range(0, len(words), words_per_chunk):
            slice_text = " ".join(words[i : i + words_per_chunk]).strip()
            if not slice_text:
                continue
            out.append(
                {
                    "text": slice_text,
                    "score": 1.0,
                    "chunk_id": f"outbound_doc_chunk_{i // words_per_chunk}",
                    "document_id": "outbound_campaign_brief",
                    "document_name": "Campaign Brief",
                    "metadata": {"source": "outbound_campaign", "approved": True},
                }
            )
            if len(out) >= 6:
                # Cap at 6 chunks so a very long brief doesn't dominate
                # the prompt; the system fragment already carries the
                # persona + objectives.
                break
        return out

    @staticmethod
    def _expand_parent_section(chunks: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Replace a chunk's text with its parent section when the chunk came
        from a likely table/list section (cancellation/refund/policy). Sliced
        rows lose the conditional structure; the parent section restores it
        for the LLM."""
        expanded: list[dict[str, Any]] = []
        seen_sections: set[str] = set()
        for chunk in chunks:
            section_id = (chunk.get("metadata") or {}).get("section_id") if isinstance(chunk.get("metadata"), dict) else None
            section_title = ((chunk.get("metadata") or {}).get("section_title") or "") if isinstance(chunk.get("metadata"), dict) else ""
            parent_text = ((chunk.get("metadata") or {}).get("parent_section_text") or "") if isinstance(chunk.get("metadata"), dict) else ""
            policy_section = bool(
                re.search(r"cancel|refund|policy|table", section_title or "", re.IGNORECASE)
            )
            if policy_section and parent_text and section_id and section_id not in seen_sections:
                seen_sections.add(section_id)
                copied = dict(chunk)
                copied["text"] = parent_text
                copied["expanded_from_parent_section"] = True
                expanded.append(copied)
            else:
                expanded.append(chunk)
        return expanded

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
        if not query.strip():
            return {"query": query, "chunks": [], "refusal": "Empty query."}
        # Dual retrieval (code-switching path): when the call is actively
        # code-switching between two languages, embedding only the
        # "best" form of the query misses chunks indexed under the other
        # form. We embed BOTH the primary and the secondary form, search
        # in parallel, and union the chunks by chunk_id. Cost: one extra
        # Qdrant search + one extra embedding (almost always a cache hit).
        if (
            dual_retrieval
            and english_text
            and _normalize(english_text).lower() != _normalize(query).lower()
        ):
            return await NokvoOneVoicePipeline._retrieve_dual(
                tenant_res,
                primary=query,
                secondary=english_text,
                db=db,
                top_k=top_k,
                campaign_id=campaign_id,
                intent_result=intent_result,
            )
        provider_status = dict(tenant_res.provider_status or {})
        policy_version = str(provider_status.get("agent_policy_version") or "")

        sensitive = bool(intent_result and intent_result.sensitive)
        effective_top_k = top_k or (
            settings.AGENT_RETRIEVAL_TOP_K_SENSITIVE if sensitive else settings.AGENT_RETRIEVAL_TOP_K
        )
        min_score = (
            settings.AGENT_MIN_RELEVANCE_SCORE_SENSITIVE if sensitive else settings.AGENT_MIN_RELEVANCE_SCORE
        )

        # MINIMAL mandatory filter — match agent_lab's pattern. tenant_id is
        # already enforced by QdrantService._payload_filter; we only need
        # source_type to scope to KB chunks (the same collection holds
        # integration tool data, embedding for other sources, etc.).
        #
        # active / approval_status / policy_version / topic were previously
        # in the must-match list. Any chunk whose payload was missing one of
        # those fields (legacy uploads, reconciled-from-Qdrant entries,
        # custom integrations) silently disappeared. Now they're only
        # consulted as soft signals AFTER retrieval — see _filter_unapproved
        # below.
        filters: dict[str, Any] = {
            "source_type": AGENT_KNOWLEDGE_SOURCE_TYPE,
        }
        if campaign_id:
            filters["campaign_id"] = campaign_id
        if sensitive and intent_result and intent_result.topic and intent_result.topic != "general":
            filters["topic"] = intent_result.topic

        vector = await TextEmbeddingService.embed_text_for_tenant(tenant_res, query)
        limit = max(1, min(effective_top_k, 12))

        # Score floor + soft approval check. We DON'T reject a chunk just
        # because it lacks an approval_status payload — older chunks or
        # reconciled ones may not have one and we still want to surface them
        # for the LLM. We only reject when approval_status is explicitly set
        # to a rejecting value.
        def _approved(point) -> bool:
            payload = getattr(point, "payload", {}) or {}
            approval = payload.get("approval_status")
            if approval is None:
                return True  # missing → trust it
            return str(approval).lower() in {"approved", "active", "ok", ""}

        def _chunks_from(points, floor: float, *, approval_check: bool) -> list[dict[str, Any]]:
            return [
                NokvoOneVoicePipeline._map_point(point)
                for point in points
                if float(getattr(point, "score", 0.0) or 0.0) >= floor
                and (not approval_check or _approved(point))
            ]

        async def _search(label: str, payload_filters: dict[str, Any]) -> list[Any]:
            started = perf_counter()
            points = await QdrantService.search_points(
                tenant_res,
                vector,
                limit=limit,
                payload_filters=payload_filters,
                db=db,
            )
            print(
                f"[NOKVO-RETRIEVE] tenant={tenant_res.tenant_id} label={label} "
                f"query={query[:60]!r} filters={payload_filters} "
                f"min_score={min_score} top_k={effective_top_k} "
                f"raw_results={len(points)} scores="
                f"{[round(float(getattr(p, 'score', 0.0) or 0.0), 3) for p in points[:5]]} "
                f"qdrant_ms={int((perf_counter() - started) * 1000)}"
            )
            return points

        primary_task = asyncio.create_task(_search("primary", filters))
        relaxed_task: asyncio.Task[list[Any]] | None = None
        minimal_task: asyncio.Task[list[Any]] | None = None
        relaxed_filters: dict[str, Any] | None = None
        minimal_filters = {"source_type": AGENT_KNOWLEDGE_SOURCE_TYPE}

        if sensitive and "topic" in filters:
            relaxed_filters = dict(filters)
            relaxed_filters.pop("topic", None)
            relaxed_task = asyncio.create_task(_search("relaxed_topic", relaxed_filters))
        if minimal_filters != filters and minimal_filters != relaxed_filters:
            minimal_task = asyncio.create_task(_search("minimal", minimal_filters))

        try:
            primary_results = await primary_task
            chunks = _chunks_from(primary_results, min_score, approval_check=True)
            if chunks:
                for task in (relaxed_task, minimal_task):
                    if task and not task.done():
                        task.cancel()
            elif relaxed_task is not None:
                relaxed_results = await relaxed_task
                chunks = _chunks_from(
                    relaxed_results,
                    settings.AGENT_MIN_RELEVANCE_SCORE,
                    approval_check=False,
                )
                if chunks and minimal_task and not minimal_task.done():
                    minimal_task.cancel()
            else:
                chunks = []

            if not chunks:
                if minimal_task is not None:
                    minimal_results = await minimal_task
                else:
                    minimal_results = primary_results
                chunks = _chunks_from(minimal_results, 0.20, approval_check=False)
        finally:
            for task in (relaxed_task, minimal_task):
                if not task:
                    continue
                if task.done():
                    try:
                        task.exception()
                    except BaseException:
                        pass
                else:
                    task.cancel()

        # For sensitive topics, broaden context by pulling the whole parent
        # section when a chunk likely came from a policy table or list row.
        if sensitive and chunks:
            chunks = NokvoOneVoicePipeline._expand_parent_section(chunks)

        # Grounding insurance for policy intents.
        #
        # If the utterance is about cancellation/refund — either because the
        # intent_result said so, OR because we detected a multi-script policy
        # keyword in the user's actual words — we ALWAYS prepend the active
        # policy_card source_text as synthetic chunks. Even when Qdrant
        # returned its own chunks: those may be unrelated FAQ content, and
        # the policy text is the authoritative answer.
        #
        # Without this, cross-lingual queries ("నాకు రీఫండ్ దొరుకుతదా?")
        # whose translate-STT timed out get classified as `unclear` →
        # retrieval returns nothing or noise → LLM refuses. With it, the
        # LLM always sees the policy matrix and can answer in the caller's
        # language.
        policy_keyword_hit = (
            detect_policy_keyword(query) is not None
            or (english_text and detect_policy_keyword(english_text) is not None)
            or (intent_result and intent_result.topic in ("cancellation", "refund"))
        )
        if policy_keyword_hit:
            policy_chunks = NokvoOneVoicePipeline._policy_card_chunks(tenant_res, policy_version)
            if policy_chunks:
                # Deduplicate: don't prepend a policy chunk whose text is
                # already present in a Qdrant result.
                existing_text = {(c.get("text") or "").strip()[:200] for c in chunks}
                new_policy = [
                    pc for pc in policy_chunks
                    if (pc.get("text") or "").strip()[:200] not in existing_text
                ]
                # Policy text goes FIRST so the LLM sees it before any
                # marginally-relevant Qdrant chunks.
                chunks = new_policy + chunks

        return {
            "query": query,
            "chunks": chunks,
            "refusal": None if chunks else "No indexed tenant context matched this question.",
            "sensitive": sensitive,
            "min_score": min_score,
            "top_k": effective_top_k,
        }

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
        """Code-switch retrieval helper.

        Runs the primary and secondary queries against Qdrant in parallel
        and unions the chunks by ``chunk_id``, keeping the higher score
        for any duplicates. Limits the merged set to a reasonable
        ``top_k`` so the LLM prompt stays bounded.
        """
        # We deliberately recurse into ``retrieve`` with dual_retrieval=
        # False so each side does its own single-query search.
        primary_task = asyncio.create_task(
            NokvoOneVoicePipeline.retrieve(
                tenant_res,
                primary,
                db=db,
                top_k=top_k,
                campaign_id=campaign_id,
                intent_result=intent_result,
                english_text=None,
                dual_retrieval=False,
            )
        )
        secondary_task = asyncio.create_task(
            NokvoOneVoicePipeline.retrieve(
                tenant_res,
                secondary,
                db=db,
                top_k=top_k,
                campaign_id=campaign_id,
                intent_result=intent_result,
                english_text=None,
                dual_retrieval=False,
            )
        )
        primary_raw, secondary_raw = await asyncio.gather(
            primary_task, secondary_task, return_exceptions=True
        )
        # When one side fails (e.g., embedding service blip on the code-switch
        # arm), keep whichever results did come back rather than losing the turn.
        primary_res = primary_raw if not isinstance(primary_raw, BaseException) else {}
        secondary_res = secondary_raw if not isinstance(secondary_raw, BaseException) else {}

        merged: dict[str, dict[str, Any]] = {}
        for source_label, res in (("primary", primary_res), ("secondary", secondary_res)):
            for chunk in res.get("chunks") or []:
                key = str(chunk.get("chunk_id") or chunk.get("document_id") or "")
                if not key:
                    continue
                if key not in merged or float(chunk.get("score") or 0.0) > float(
                    merged[key].get("score") or 0.0
                ):
                    merged[key] = chunk
        chunks = sorted(
            merged.values(),
            key=lambda c: float(c.get("score") or 0.0),
            reverse=True,
        )
        # Bound the merged list to a sensible cap — code-switch retrieval
        # naturally inflates the chunk count and we don't want to pay
        # the prompt-size cost.
        effective_top_k = top_k or settings.AGENT_RETRIEVAL_TOP_K
        chunks = chunks[: max(effective_top_k, 4)]
        sensitive = bool(intent_result and intent_result.sensitive)
        return {
            "query": primary,
            "secondary_query": secondary,
            "chunks": chunks,
            "refusal": None if chunks else "No indexed tenant context matched this question.",
            "sensitive": sensitive,
            "min_score": primary_res.get("min_score") or secondary_res.get("min_score"),
            "top_k": effective_top_k,
            "dual_retrieval": True,
        }

    @staticmethod
    def _policy_card_chunks(tenant_res: TenantResources, policy_version: str) -> list[dict[str, Any]]:
        """Synthesize retrieval chunks from active policy cards.

        These aren't real Qdrant results — they're the policy's own
        ``source_text``, formatted to look like a chunk so the existing
        ``_messages`` builder treats them as grounding context. Used as a
        last-resort when Qdrant retrieval came up empty on a sensitive
        cancellation/refund intent.
        """
        provider_status = dict(tenant_res.provider_status or {})
        cards = provider_status.get(AGENT_POLICY_CARDS_KEY) or []
        out: list[dict[str, Any]] = []
        for card in cards:
            if card.get("approval_status") not in (None, "approved"):
                continue
            if card.get("status") not in (None, "active", "ok"):
                continue
            if policy_version and card.get("policy_version") and card.get("policy_version") != policy_version:
                continue
            text = (card.get("source_text") or "").strip()
            if not text:
                # Build text from the structured conditions when source_text
                # isn't preserved.
                conds = card.get("conditions") or []
                lines = [str(cond.get("customer_message") or "").strip() for cond in conds]
                text = "\n".join(line for line in lines if line)
            if not text:
                continue
            out.append(
                {
                    "document_id": str(card.get("document_id") or ""),
                    "document_name": str(card.get("source_section_title") or "Policy"),
                    "chunk_id": str(card.get("id") or ""),
                    "text": text[:4000],
                    "score": 1.0,
                    "metadata": {
                        "source_type": "agent_policy_card",
                        "topic": card.get("topic"),
                        "policy_version": card.get("policy_version"),
                        "sensitivity": "sensitive",
                        "source_title": card.get("source_section_title") or "Policy",
                    },
                }
            )
        return out

    @staticmethod
    def _single_prompt_guidance(tenant_res: TenantResources) -> str:
        # Synchronous read straight off ``provider_status`` — used by
        # branches that can't await (template router decisions). The async
        # ``_voice_business_context`` path uses the cached bundle which
        # contains the same string.
        provider_status = dict(tenant_res.provider_status or {})
        config = provider_status.get(AGENT_SINGLE_PROMPT_CONFIG_KEY) or {}
        if not isinstance(config, dict) or not config.get("enabled"):
            return ""
        prompt = str(config.get("prompt") or "").strip()
        return prompt[:8000]

    @staticmethod
    def _single_prompt_enabled(tenant_res: TenantResources) -> bool:
        return bool(NokvoOneVoicePipeline._single_prompt_guidance(tenant_res))

    @staticmethod
    async def _voice_business_context(
        db: AsyncSession | None,
        tenant_res: TenantResources,
    ) -> tuple[Organization, dict[str, Any], list[dict[str, Any]]] | None:
        """Resolve the ``(organization, overrides, custom_tabs)`` tuple via
        the per-tenant :class:`RuntimeBundle` cache so repeat turns avoid a
        DB round-trip and a custom_tabs rebuild."""
        bundle = await get_runtime_bundle(db, tenant_res)
        return bundle.as_business_context_tuple()

    @staticmethod
    def _parse_appointment_date(value: Any, *, now: datetime | None = None) -> datetime.date:
        raw = re.sub(r"\s+", " ", str(value or "").strip().lower())
        local_now = (now or datetime.now(timezone.utc)).astimezone(_APPOINTMENT_LOCAL_TZ)
        today = local_now.date()
        if not raw:
            raise _AppointmentToolInputError("preferred_date", "Which date should I note for the appointment?")
        # ISO `YYYY-MM-DD` (or `YYYY-MM-DDTHH:MM:SS…`) — emitted by the
        # slot-acceptance path and by any external integration. The legacy
        # numeric regex below treats this as DD/MM and produces month=20
        # nonsense, hence the explicit branch.
        iso_match = re.match(r"^\s*(\d{4})-(\d{1,2})-(\d{1,2})", raw)
        if iso_match:
            try:
                year = int(iso_match.group(1))
                month = int(iso_match.group(2))
                day = int(iso_match.group(3))
                return datetime(year, month, day, tzinfo=_APPOINTMENT_LOCAL_TZ).date()
            except ValueError:
                pass
        if "day after tomorrow" in raw:
            return today + timedelta(days=2)
        if "tomorrow" in raw:
            return today + timedelta(days=1)
        if "today" in raw:
            return today
        for name, weekday in _WEEKDAY_INDEX.items():
            if name in raw:
                delta = (weekday - today.weekday()) % 7
                return today + timedelta(days=delta)

        numeric = re.search(r"\b(\d{1,2})[/-](\d{1,2})(?:[/-](\d{2,4}))?\b", raw)
        if numeric:
            day = int(numeric.group(1))
            month = int(numeric.group(2))
            year = int(numeric.group(3) or today.year)
            if year < 100:
                year += 2000
            try:
                parsed = datetime(year, month, day, tzinfo=_APPOINTMENT_LOCAL_TZ).date()
            except ValueError as exc:
                raise _AppointmentToolInputError(
                    "preferred_date",
                    "That date does not look valid. Which date should I note?",
                    clear_date=True,
                ) from exc
            return parsed if parsed >= today or numeric.group(3) else parsed.replace(year=parsed.year + 1)

        named = re.search(r"\b(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]+)(?:\s+(\d{2,4}))?\b", raw)
        if not named:
            named = re.search(r"\b([a-z]+)\s+(\d{1,2})(?:st|nd|rd|th)?(?:\s+(\d{2,4}))?\b", raw)
            if named:
                month_token, day_token, year_token = named.group(1), named.group(2), named.group(3)
            else:
                month_token = day_token = year_token = None
        else:
            day_token, month_token, year_token = named.group(1), named.group(2), named.group(3)
        if day_token and month_token:
            month = _MONTH_INDEX.get(month_token[:3], _MONTH_INDEX.get(month_token))
            if month:
                year = int(year_token or today.year)
                if year < 100:
                    year += 2000
                try:
                    parsed = datetime(year, month, int(day_token), tzinfo=_APPOINTMENT_LOCAL_TZ).date()
                except ValueError as exc:
                    raise _AppointmentToolInputError(
                        "preferred_date",
                        "That date does not look valid. Which date should I note?",
                        clear_date=True,
                    ) from exc
                return parsed if parsed >= today or year_token else parsed.replace(year=parsed.year + 1)

        raise _AppointmentToolInputError(
            "preferred_date",
            "I need the appointment date clearly. Which date should I note?",
            clear_date=True,
        )

    @staticmethod
    def _parse_appointment_time(value: Any) -> time:
        raw = re.sub(r"\s+", " ", str(value or "").strip().lower())
        if not raw:
            raise _AppointmentToolInputError("preferred_time", "What time should I note for the appointment?")
        named_times = {
            "morning": time(9, 0),
            "afternoon": time(14, 0),
            "evening": time(17, 0),
            "night": time(19, 0),
            "noon": time(12, 0),
        }
        for label, parsed in named_times.items():
            if label in raw:
                return parsed
        ampm = re.search(r"\b(\d{1,2})(?::([0-5]\d))?\s*(am|pm)\b", raw)
        if ampm:
            hour = int(ampm.group(1))
            minute = int(ampm.group(2) or 0)
            suffix = ampm.group(3)
            if hour < 1 or hour > 12:
                raise _AppointmentToolInputError(
                    "preferred_time",
                    "That time does not look valid. What time should I note?",
                    clear_time=True,
                )
            if suffix == "pm" and hour != 12:
                hour += 12
            if suffix == "am" and hour == 12:
                hour = 0
            return time(hour, minute)
        twenty_four = re.search(r"\b([01]?\d|2[0-3]):([0-5]\d)\b", raw)
        if twenty_four:
            return time(int(twenty_four.group(1)), int(twenty_four.group(2)))
        bare = re.fullmatch(r"(?:at\s+)?(\d{1,2})", raw)
        if bare:
            hour = int(bare.group(1))
            if 0 <= hour <= 23 and hour > 12:
                return time(hour, 0)
            raise _AppointmentToolInputError(
                "preferred_time",
                f"Just to confirm, is that {hour} AM or {hour} PM?",
                clear_time=True,
            )
        raise _AppointmentToolInputError(
            "preferred_time",
            "I need the appointment time clearly. What time should I note?",
            clear_time=True,
        )

    @staticmethod
    def _appointment_datetime_iso(appointment: dict[str, Any]) -> str:
        # Fast path: caller already accepted a proposed slot, which left a
        # canonical UTC ISO on the appointment. Trust it and skip re-parsing.
        proposed = appointment.get("appointment_time")
        if isinstance(proposed, str) and proposed:
            try:
                parsed = datetime.fromisoformat(proposed.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                if parsed.astimezone(_APPOINTMENT_LOCAL_TZ) > datetime.now(_APPOINTMENT_LOCAL_TZ):
                    return parsed.astimezone(timezone.utc).isoformat()
            except ValueError:
                pass
        local_date = NokvoOneVoicePipeline._parse_appointment_date(appointment.get("preferred_date"))
        local_time = NokvoOneVoicePipeline._parse_appointment_time(appointment.get("preferred_time"))
        local_dt = datetime.combine(local_date, local_time, tzinfo=_APPOINTMENT_LOCAL_TZ)
        if local_dt <= datetime.now(_APPOINTMENT_LOCAL_TZ):
            raise _AppointmentToolInputError(
                "preferred_date",
                "That appointment time is already past. Which future date and time should I note?",
                clear_date=True,
                clear_time=True,
            )
        return local_dt.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _should_offer_sms_confirmation(tenant_res: TenantResources | None) -> bool:
        """Return True only when the tenant has explicitly opted into the
        end-of-booking SMS confirmation offer. The platform default is False
        because SMS dispatch isn't wired in yet — offering a confirmation
        that never arrives is a worse caller experience than offering
        nothing. Tenants enable it via
        ``provider_status['agent_offer_sms_confirmation'] = True`` once
        their SMS gateway is connected."""
        if tenant_res is None:
            return bool(settings.NOKVO_AGENT_OFFER_SMS_CONFIRMATION)
        override = (tenant_res.provider_status or {}).get("agent_offer_sms_confirmation")
        if override is None:
            return bool(settings.NOKVO_AGENT_OFFER_SMS_CONFIRMATION)
        return bool(override)

    @staticmethod
    def _appointment_tool_answer(
        result: dict[str, Any],
        args: dict[str, Any],
        *,
        language: str | None = None,
        offer_sms: bool = False,
    ) -> str:
        patient = str(args.get("patient_name") or "the patient")
        when = str(args.get("appointment_time") or "the requested time")
        try:
            parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
            local_when = parsed.astimezone(_APPOINTMENT_LOCAL_TZ).strftime("%d %b %Y at %I:%M %p")
        except Exception:
            local_when = when
        assignment_status = result.get("assignment_status")
        assigned_name = result.get("assigned_member_name")
        lang = SarvamVoiceService.normalize_language(language)
        if lang == "te":
            if assignment_status == "assigned" and assigned_name:
                return (
                    f"Appointment request create అయ్యింది for {patient} on {local_when}. "
                    f"It has been assigned to {assigned_name}."
                )
            if assignment_status == "no_available_member":
                return (
                    f"Appointment request create అయ్యింది for {patient} on {local_when}. "
                    "That slot note చేశాను, కానీ available doctor system లో కనిపించలేదు. "
                    "Clinic team availability confirm చేస్తారు."
                )
            return (
                f"Appointment request create అయ్యింది for {patient} on {local_when}. "
                "Clinic team exact availability confirm చేస్తారు."
            )
        phone = str(args.get("phone") or "").strip()
        # End-of-call SMS offer is opt-in: empty unless the tenant has
        # wired SMS dispatch and toggled ``agent_offer_sms_confirmation``.
        sms_offer = ""
        if offer_sms and phone:
            spoken_phone = " ".join(list(phone[-10:])) if phone[-10:].isdigit() else phone
            if lang == "te":
                sms_offer = f" {spoken_phone} కి confirmation SMS పంపాలా?"
            elif lang == "hi":
                sms_offer = f" क्या {spoken_phone} पर confirmation SMS भेज दूँ?"
            else:
                sms_offer = f" Want me to send a confirmation SMS to {spoken_phone}?"
        if assignment_status == "assigned" and assigned_name:
            return (
                f"I have created the appointment request for {patient} on {local_when}. "
                f"It has been assigned to {assigned_name}.{sms_offer}"
            )
        if assignment_status == "no_available_member":
            return (
                f"I have created the appointment request for {patient} on {local_when}. "
                "That time is noted, but I could not find an available doctor in the system for that slot, "
                f"so the clinic team will confirm availability.{sms_offer}"
            )
        return (
            f"I have created the appointment request for {patient} on {local_when}. "
            f"The clinic team can confirm exact availability.{sms_offer}"
        )

    @staticmethod
    async def _handle_availability_check(
        tenant_res: TenantResources,
        db: AsyncSession | None,
        turn_policy: dict[str, Any],
    ) -> dict[str, Any] | None:
        """Consult the scheduler when the caller asks "is X available?" /
        "when can you book me?". Works across business types — picks the
        right request_type from the active flow or the industry default.
        Returns ``None`` when no scheduling-shaped flow applies (e.g.,
        ecommerce ticket creation), so the pipeline can fall back to RAG."""

        def _first_truthy(mapping: dict[str, Any], keys: tuple[str, ...]) -> Any:
            for key in keys:
                value = mapping.get(key)
                if value:
                    return value
            return None

        from app.services.nokvo_one_assignment_service import (
            NokvoOneAssignmentService,
            _aware_utc,
        )

        if db is None:
            return None
        context = await NokvoOneVoicePipeline._voice_business_context(db, tenant_res)
        if context is None:
            return None
        organization, _overrides, _custom_tabs = context

        # Identify the request_type to schedule against. Priority:
        #   1) the active appointment FSM (clinics) → "appointment"
        #   2) the active generic tool_flow → derived from flow_key
        #   3) industry default
        # If no scheduling-shaped flow applies, return None.
        state_patch = turn_policy.get("state_patch") or {}
        appointment = dict(state_patch.get("appointment") or {})
        tool_flow_state = dict(state_patch.get("tool_flow") or {})
        flow_key = str(tool_flow_state.get("flow_key") or "")
        industry = (organization.industry or "").lower()
        _FLOW_TO_REQUEST_TYPE = {
            "real_estate_site_visit": "site_visit",
        }
        _INDUSTRY_DEFAULT = {
            "clinics": "appointment",
            "real_estate": "site_visit",
            "hospitality": "callback",
        }
        request_type = _FLOW_TO_REQUEST_TYPE.get(flow_key) or _INDUSTRY_DEFAULT.get(industry)
        if not request_type:
            return None

        entities = turn_policy.get("entities") or {}
        language = turn_policy.get("language")

        # Resolve the candidate datetime in priority order:
        #   1) this turn's spoken date+time
        #   2) the in-progress appointment slot values
        #   3) "now" — caller asked "when can you book?" with no time
        # Source of the requested time: this turn's entities, or the in-progress
        # appointment / tool_flow slots, depending on which flow is active.
        collected = dict(tool_flow_state.get("collected") or {})
        date_slot_value = (
            entities.get("date_text")
            or appointment.get("preferred_date")
            or _first_truthy(collected, ("visit_date", "callback_date", "preferred_date", "date"))
        )
        time_slot_value = (
            entities.get("time_text")
            or appointment.get("preferred_time")
            or _first_truthy(collected, ("visit_time", "callback_time", "preferred_time", "time"))
        )

        # Tool_flow flows often store a combined "visit_at" / "callback_at" ISO
        # string instead of split date/time — try those before falling back.
        requested_at: datetime | None = None
        if not (date_slot_value and time_slot_value):
            for combined_key in ("visit_at", "callback_at", "confirm_at", "scheduled_at"):
                combined = collected.get(combined_key)
                if combined:
                    try:
                        parsed_combined = datetime.fromisoformat(
                            str(combined).replace("Z", "+00:00")
                        )
                        requested_at = parsed_combined.astimezone(timezone.utc)
                    except Exception:
                        pass
                    break

        # Track whether the caller actually specified a time — used below to
        # decide between "X is taken — next free is Y" (specific) and a
        # cleaner "The next available slot is Y" (open-ended).
        caller_specified_time = False
        if requested_at is None and date_slot_value and time_slot_value:
            try:
                local_date = NokvoOneVoicePipeline._parse_appointment_date(date_slot_value)
                local_time = NokvoOneVoicePipeline._parse_appointment_time(time_slot_value)
                local_dt = datetime.combine(local_date, local_time, tzinfo=_APPOINTMENT_LOCAL_TZ)
                requested_at = local_dt.astimezone(timezone.utc)
                caller_specified_time = True
            except (_AppointmentToolInputError, Exception):
                requested_at = None
        # Adaptive disambiguation: caller gave a date but no time. Use
        # start-of-working-day (9 AM local) as the anchor so the scheduler
        # surfaces the first free slot on that date.
        if requested_at is None and date_slot_value and not time_slot_value:
            try:
                local_date = NokvoOneVoicePipeline._parse_appointment_date(date_slot_value)
                local_dt = datetime.combine(local_date, time(9, 0), tzinfo=_APPOINTMENT_LOCAL_TZ)
                requested_at = local_dt.astimezone(timezone.utc)
            except Exception:
                requested_at = None
        if requested_at is None:
            now_local = datetime.now(_APPOINTMENT_LOCAL_TZ)
            # Round up to the next 15-minute mark — feels less robotic than
            # "available at 14:37". Caller can refine afterwards.
            minute = (now_local.minute // 15 + 1) * 15
            if minute >= 60:
                now_local = now_local.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
            else:
                now_local = now_local.replace(minute=minute, second=0, microsecond=0)
            requested_at = now_local.astimezone(timezone.utc)

        # Load members + scheduling state.
        members = await NokvoOneAssignmentService._load_members(db, organization.id)
        settings_map = await NokvoOneAssignmentService._load_assignment_settings(db, organization.id)
        clinic_map = await NokvoOneAssignmentService._load_clinic_settings(db, organization.id)
        blocked_map = await NokvoOneAssignmentService._load_blocked_slots(db, organization.id)
        records = await NokvoOneAssignmentService._load_request_records(db, organization.id)

        _ROLE_LABEL = {
            "clinics": "doctor",
            "real_estate": "agent",
            "hospitality": "host",
        }
        member_role_label = _ROLE_LABEL.get(industry, "team member")

        # Walk every assignable member and collect their next available
        # slot. We do NOT short-circuit on the first member: we explicitly
        # want the slot CLOSEST to the caller's requested time, regardless
        # of which member it belongs to. So "Member 2 at 10am" beats
        # "Member 1 at 11am" when the caller asked for 10am — the second
        # member's same-time slot is strictly preferred over the first
        # member's next-time slot. Ties on shift_minutes are broken by
        # active_load so a less-busy member wins, then by member creation
        # order for full determinism.
        candidates: list[tuple[int, int, datetime, str]] = []
        for member in members:
            settings = settings_map.get(member.id)
            if settings is None or not settings.is_assignable:
                continue
            if request_type not in set(settings.request_types or []):
                continue
            member_blocks = list(blocked_map.get(member.id, []))
            member_blocks.extend(blocked_map.get("_org_wide", []))  # type: ignore[arg-type]
            slot = NokvoOneAssignmentService._find_next_available_slot(
                member_id=member.id,
                requested_at=_aware_utc(requested_at),
                settings=settings,
                clinic_settings=clinic_map.get(member.id) if industry == "clinics" else None,
                blocked_slots=member_blocks,
                records=records,
                exclude_record_id=None,
            )
            if slot is None:
                continue
            when_utc, shift_min = slot
            active_load = NokvoOneAssignmentService._active_load(records, member.id)
            candidates.append(
                (
                    shift_min,
                    active_load,
                    when_utc,
                    member.full_name or f"the on-call {member_role_label}",
                )
            )

        best: tuple[datetime, int, str] | None = None
        if candidates:
            # Time-first ordering. Same as the canonical sort in
            # assign_request, so the slot we propose to the caller
            # matches what the booking would actually pick.
            candidates.sort(key=lambda c: (c[0], c[1]))
            shift_min, _load, when_utc, member_name = candidates[0]
            best = (when_utc, shift_min, member_name)

        if best is None:
            answer = (
                "I checked the calendar and nothing fits within the working hours. "
                "Could you share another date or time?"
            )
            patch: dict[str, Any] = {}
            if appointment:
                patch["appointment"] = appointment
            if tool_flow_state:
                patch["tool_flow"] = tool_flow_state
            return {
                "answer": answer,
                "state_patch": patch,
                "state_slot": "availability_check_empty",
                "route_reason": "scheduler returned no slot",
                "tool_calls": [],
            }

        when_utc, shift_min, member_name = best
        when_local = when_utc.astimezone(_APPOINTMENT_LOCAL_TZ)
        when_label = when_local.strftime("%d %b at %I:%M %p").lstrip("0")
        if shift_min == 0:
            answer = (
                f"Yes, {when_label} is open with {member_name}. "
                "Want me to lock that in?"
            )
            slot_label = "availability_exact"
        elif caller_specified_time:
            # Caller named a specific time — acknowledge it's taken and
            # propose the next free slot.
            requested_local = requested_at.astimezone(_APPOINTMENT_LOCAL_TZ)
            requested_label = requested_local.strftime("%d %b at %I:%M %p").lstrip("0")
            answer = (
                f"{requested_label} is taken — the next free slot is {when_label} with {member_name}. "
                "Want me to book that?"
            )
            slot_label = "availability_next"
        else:
            # Caller asked open-endedly ("when is it available?"). The
            # "X is taken" preamble makes no sense here — just lead with
            # the proposal.
            answer = (
                f"The next available slot is {when_label} with {member_name}. "
                "Want me to book that?"
            )
            slot_label = "availability_next"

        # Stash the offered slot on whichever flow is active. The follow-up
        # turn's policy looks at awaiting_slot_confirm in both shapes.
        if appointment or industry == "clinics":
            appointment["proposed_slot_utc"] = when_utc.isoformat()
            appointment["proposed_slot_label"] = when_label
            appointment["awaiting_slot_confirm"] = True
            appointment["active"] = True
        elif tool_flow_state:
            tool_flow_state["proposed_slot_utc"] = when_utc.isoformat()
            tool_flow_state["proposed_slot_label"] = when_label
            tool_flow_state["awaiting_slot_confirm"] = True
            tool_flow_state["active"] = True
        patch: dict[str, Any] = {}
        if appointment:
            patch["appointment"] = appointment
        if tool_flow_state:
            patch["tool_flow"] = tool_flow_state
        return {
            "answer": answer,
            "state_patch": patch,
            "state_slot": slot_label,
            "route_reason": "scheduler answered availability question",
            "tool_calls": [],
        }

    @staticmethod
    async def _maybe_execute_turn_policy_action(
        tenant_res: TenantResources,
        call_id: str | None,
        db: AsyncSession | None,
        turn_policy: dict[str, Any],
    ) -> dict[str, Any] | None:
        if turn_policy.get("intent") == "availability_check":
            return await NokvoOneVoicePipeline._handle_availability_check(
                tenant_res, db, turn_policy
            )
        if turn_policy.get("intent") != "appointment_flow" or turn_policy.get("state_slot") != "complete":
            return None
        appointment = dict(((turn_policy.get("state_patch") or {}).get("appointment") or {}))
        if appointment.get("created_record_id"):
            return None

        context = await NokvoOneVoicePipeline._voice_business_context(db, tenant_res)
        if context is None:
            return None
        organization, overrides, custom_tabs = context
        if organization.industry != "clinics":
            return None
        catalog = resolve_index(organization.industry, overrides, custom_tabs)
        tool = catalog.get("appointments_create")
        if tool is None:
            return None

        try:
            appointment_time = NokvoOneVoicePipeline._appointment_datetime_iso(appointment)
        except _AppointmentToolInputError as exc:
            appointment["completed"] = False
            appointment["pending_slot"] = exc.slot
            if exc.clear_date:
                appointment["preferred_date"] = None
            if exc.clear_time:
                appointment["preferred_time"] = None
            return {
                "answer": exc.answer,
                "state_patch": {"appointment": appointment},
                "state_slot": exc.slot,
                "route_reason": "appointment needs exact scheduling detail",
                "tool_calls": [],
            }

        args = {
            "patient_name": appointment["patient_name"],
            "phone": appointment["phone"],
            "appointment_time": appointment_time,
            "reason": appointment["reason"],
        }
        # Confirmation / audit metadata is patched onto the created record
        # *after* execution (it'd be rejected by the tool's strict
        # additionalProperties:false schema if passed as args).
        record_metadata: dict[str, Any] = {}
        for key in ("confirmations", "audit_trail", "proposed_slot_accepted"):
            value = appointment.get(key)
            if value:
                record_metadata[key] = value
        # Inline retry + graceful fallback. Retry count + delay come from the
        # canonical agent spec (:class:`RetryPolicy`) — not hardcoded.
        from app.services.agent_spec import RETRY_POLICY

        result = None
        last_exc: Exception | None = None
        max_inline_attempts = 1 + RETRY_POLICY.inline_retries
        for attempt in range(max_inline_attempts):
            try:
                result = await PredefinedToolsService.execute(
                    db,
                    tenant_res.organization_id,
                    None,
                    tool,
                    args,
                    session_id=call_id,
                )
                await db.commit()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 — voice tool entry, broad catch by design
                last_exc = exc
                if db is not None:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                if attempt < max_inline_attempts - 1:
                    await asyncio.sleep(RETRY_POLICY.inline_delay_seconds)
        if result is None:
            # Inline retries exhausted — persist to the retry queue so a
            # worker / admin / cron can pick it back up once the underlying
            # issue clears. The caller's data is *not* lost.
            try:
                from app.services.tool_retry_service import ToolRetryService

                await ToolRetryService.enqueue(
                    db,
                    organization_id=tenant_res.organization_id,
                    tool_key=tool.key,
                    arguments=args,
                    context={
                        "call_id": call_id,
                        "language": turn_policy.get("language"),
                        "intent": "appointment",
                    },
                    last_error=str(last_exc) if last_exc else None,
                )
            except Exception:
                pass
            appointment["completed"] = False
            appointment["pending_slot"] = None
            appointment["needs_callback"] = True
            from app.services.flow_session import append_audit_trail
            append_audit_trail(appointment, "tool_retry_enqueued", detail=str(last_exc)[:200] if last_exc else None)
            lang = SarvamVoiceService.normalize_language(turn_policy.get("language"))
            if lang == "te":
                fallback = (
                    "I have all the details, kāni system temporarily unavailable. "
                    "Clinic team mīkū call back chestāru same number ki."
                )
            elif lang == "hi":
                fallback = (
                    "मेरे पास सारी जानकारी है, पर सिस्टम अभी temporarily unavailable है. "
                    "Clinic team आपके इसी नंबर पर call back करेगी."
                )
            else:
                fallback = (
                    "I have all your details, but I'm having trouble saving them right now. "
                    "The clinic team will call you back on this number to confirm — your booking won't be missed."
                )
            return {
                "answer": fallback,
                "state_patch": {"appointment": appointment},
                "state_slot": "tool_error",
                "route_reason": "appointment tool failed after retry",
                "tool_calls": [
                    {"tool": tool.key, "arguments": args, "ok": False, "error": str(last_exc)[:240]},
                ],
            }

        appointment.update(
            {
                "active": False,
                "completed": True,
                "pending_slot": None,
                "appointment_time": appointment_time,
                "created_record_id": result.get("id"),
                "assignment_status": result.get("assignment_status"),
                "assigned_member_name": result.get("assigned_member_name"),
            }
        )
        # Patch the persisted record with confirmation/audit metadata so
        # downstream consumers see what the caller actually confirmed.
        if record_metadata and result.get("id") and db is not None:
            await NokvoOneVoicePipeline._patch_record_metadata(
                db, result["id"], record_metadata
            )
        return {
            "answer": NokvoOneVoicePipeline._appointment_tool_answer(
                result,
                args,
                language=turn_policy.get("language"),
                offer_sms=NokvoOneVoicePipeline._should_offer_sms_confirmation(tenant_res),
            ),
            "state_patch": {"appointment": appointment},
            "state_slot": "complete",
            "route_reason": "appointment tool executed",
            "tool_calls": [{"tool": tool.key, "arguments": args, "result": result}],
        }

    @staticmethod
    def _map_lead_data_to_ticket_shape(data: dict[str, Any], industry: str | None) -> dict[str, Any]:
        """Project the lead-shaped fields onto the keys the ticket schema
        expects so the Tickets tab renders populated cells rather than blank
        ones. Per business-template:

        * real_estate tickets need ``customer``, ``issue_type``, ``priority``
          (the lead has ``name``, ``phone``, ``visit_date``).
        * clinics tickets need ``subject``, ``customer``, ``priority``.
        * ecommerce / hospitality follow the same name → customer convention.

        We only ADD; existing data is preserved so anything downstream that
        was looking for the old keys still finds them.
        """
        merged = dict(data or {})
        ind = (industry or "").lower()

        # Common "customer" alias from whatever name-like field the lead had.
        customer = (
            merged.get("customer")
            or merged.get("name")
            or merged.get("customer_name")
            or merged.get("patient_name")
            or merged.get("guest_name")
            or merged.get("contact_name")
        )
        if customer and not merged.get("customer"):
            merged["customer"] = customer

        # Default priority / issue_type / subject so the required ticket
        # columns aren't empty. We err on the safe side ("normal") and let
        # the operator re-classify in the dashboard if needed.
        merged.setdefault("priority", "normal")
        if ind == "real_estate":
            merged.setdefault("issue_type", "site_visit")
            if merged.get("location") and not merged.get("property_id"):
                merged["property_id"] = merged["location"]
        elif ind == "clinics":
            merged.setdefault("subject", merged.get("care_need") or merged.get("reason") or "Patient request")
            merged.setdefault("priority", "normal")
        elif ind == "ecommerce":
            merged.setdefault("subject", merged.get("subject") or merged.get("issue_summary") or "Customer inquiry")
            merged.setdefault("issue_type", merged.get("issue_type") or "support_request")
        elif ind == "hospitality":
            merged.setdefault("subject", merged.get("subject") or "Guest inquiry")
            merged.setdefault("reservation_id", merged.get("reservation_id") or merged.get("booking_id"))
        return merged

    @staticmethod
    async def _route_record_by_surface(
        db: AsyncSession,
        record_ids: list[Any],
        *,
        call_surface: str | None,
        industry: str | None = None,
    ) -> None:
        """Inbound calls = caller reached out for help → tickets tab.
        Outbound calls = we reached out → leads tab.

        We rewrite ``record_type`` post-creation on records the macro created
        as ``lead`` so they land in the right tab, AND project the data dict
        onto the ticket schema's expected field keys so the UI renders
        populated cells (otherwise the row looks blank and the operator
        thinks no ticket was created)."""
        if call_surface not in {"voice_inbound", "voice_outbound"} or not record_ids:
            return
        from app.models.nokvo_one_tool_record import NokvoOneToolRecord
        from app.services.nokvo_one_business_templates import STATUS_VOCABULARIES
        from sqlalchemy import select
        import uuid as _uuid

        # Only inbound triggers a rewrite (lead → ticket). Outbound already
        # creates leads and that's correct.
        if call_surface != "voice_inbound":
            return

        ticket_status = (
            (STATUS_VOCABULARIES.get((industry or "").lower(), {}).get("tickets") or {}).get("initial")
            or "open"
        )
        for rid in record_ids:
            try:
                rid_uuid = _uuid.UUID(str(rid))
            except (TypeError, ValueError):
                continue
            try:
                res = await db.execute(
                    select(NokvoOneToolRecord).where(NokvoOneToolRecord.id == rid_uuid)
                )
                rec = res.scalars().first()
                if rec is None or rec.record_type != "lead":
                    continue
                rec.record_type = "ticket"
                rec.status = ticket_status
                projected = NokvoOneVoicePipeline._map_lead_data_to_ticket_shape(rec.data or {}, industry)
                projected["routed_from"] = "lead"
                projected["call_surface"] = call_surface
                rec.data = projected
                db.add(rec)
                await db.commit()
            except Exception:
                try:
                    await db.rollback()
                except Exception:
                    pass

    @staticmethod
    def _campaign_contact(campaign_context: dict[str, Any] | None) -> dict[str, Any]:
        contact = (campaign_context or {}).get("contact")
        return contact if isinstance(contact, dict) else {}

    @staticmethod
    def _phone_from_call_context(
        memory: dict[str, Any],
        campaign_context: dict[str, Any] | None,
    ) -> str:
        contact = NokvoOneVoicePipeline._campaign_contact(campaign_context)
        raw = (
            memory.get("phone")
            or contact.get("phone")
            or contact.get("phone_e164")
            or (campaign_context or {}).get("from_phone")
            or (campaign_context or {}).get("to_phone")
            or ""
        )
        digits = re.sub(r"\D", "", str(raw))
        if len(digits) >= 10:
            return digits[-10:]
        return str(raw).strip()

    @staticmethod
    def _budget_number(value: Any) -> float | None:
        match = re.search(r"([0-9]+(?:\.[0-9]+)?)", str(value or "").replace(",", ""))
        if not match:
            return None
        try:
            return float(match.group(1))
        except ValueError:
            return None

    @staticmethod
    def _real_estate_interest_signal(
        *,
        memory: dict[str, Any],
        history: list[dict[str, str]],
        call_surface: str | None,
        outbound_context: OutboundCampaignContext | None,
    ) -> bool:
        objection = str(memory.get("objection") or "").lower()
        if re.search(r"\b(not interested|don't call|do not call|remove me|wrong number)\b", objection):
            return False
        if any(memory.get(key) for key in (
            "purpose", "bhk", "budget", "timeline", "location_preference",
            "visit_preference", "requested_info",
        )):
            return True
        user_text = " ".join(
            str(turn.get("content") or "")
            for turn in history[-12:]
            if turn.get("role") == "user"
        ).lower()
        if re.search(
            r"\b(property|flat|apartment|villa|plot|bhk|site\s+visit|brochure|"
            r"pricing|price|cost|floor\s*plan|details?|rera|interested|investment|self[-\s]?use)\b",
            user_text,
        ):
            return True
        # Outbound calls should not create a lead just because the opener ran.
        # Require at least one customer utterance beyond a tiny permission reply.
        if call_surface == "voice_outbound" and outbound_context is not None:
            substantial_user_turns = [
                str(turn.get("content") or "").strip()
                for turn in history[-12:]
                if turn.get("role") == "user" and len(str(turn.get("content") or "").split()) > 2
            ]
            return bool(substantial_user_turns)
        return False

    @staticmethod
    def _real_estate_memory_from_history(
        memory: dict[str, Any],
        history: list[dict[str, str]],
    ) -> dict[str, Any]:
        merged = dict(memory or {})
        for turn in (history or [])[-16:]:
            if turn.get("role") != "user":
                continue
            merged = update_outbound_memory(
                merged,
                caller_text=str(turn.get("content") or ""),
            )
        return merged

    @staticmethod
    def _lead_args_from_call_memory(
        *,
        memory: dict[str, Any],
        campaign_context: dict[str, Any] | None,
        outbound_context: OutboundCampaignContext | None,
    ) -> dict[str, Any]:
        contact = NokvoOneVoicePipeline._campaign_contact(campaign_context)
        name = (
            memory.get("name")
            or contact.get("name")
            or contact.get("full_name")
            or contact.get("customer_name")
            or "Property inquiry"
        )
        phone = NokvoOneVoicePipeline._phone_from_call_context(memory, campaign_context)
        args: dict[str, Any] = {
            "name": str(name).strip()[:200] or "Property inquiry",
            "phone": phone,
        }
        if memory.get("bhk"):
            args["property_type"] = str(memory["bhk"])
        elif outbound_context and outbound_context.pitch_summary:
            bhk_match = re.search(r"\b([1-6]\s*BHK)\b", outbound_context.pitch_summary, re.IGNORECASE)
            if bhk_match:
                args["property_type"] = bhk_match.group(1).upper().replace(" ", " ")
        budget = NokvoOneVoicePipeline._budget_number(memory.get("budget"))
        if budget is not None:
            args["budget"] = budget
        if memory.get("location_preference"):
            args["location"] = str(memory["location_preference"])
        return {key: value for key, value in args.items() if value not in (None, "")}

    @staticmethod
    async def maybe_create_real_estate_lead_from_call(
        tenant_res: TenantResources,
        db: AsyncSession | None,
        call_id: str | None,
        *,
        campaign_context: dict[str, Any] | None = None,
        outbound_context: OutboundCampaignContext | None = None,
    ) -> dict[str, Any] | None:
        """Create a real-estate lead at call end when interest was expressed.

        This catches short calls that never complete the slot-filling flow:
        inbound property inquiries and outbound leads who ask for details /
        pricing / brochure and then hang up. It is idempotent per call and
        deliberately requires a phone number because ``leads_create`` does.
        """
        if db is None or not call_id:
            return None
        state = await AgentSessionStore.get_state(tenant_res, call_id) or {}
        if state.get("auto_lead_created"):
            return None
        tool_flow = dict(state.get("tool_flow") or {})
        if tool_flow.get("created_record_id") or tool_flow.get("completed"):
            return None
        context = await NokvoOneVoicePipeline._voice_business_context(db, tenant_res)
        if context is None:
            return None
        organization, overrides, custom_tabs = context
        if str(getattr(organization, "industry", "") or "").lower() != "real_estate":
            return None
        history = await AgentSessionStore.get_history(tenant_res, call_id)
        memory = NokvoOneVoicePipeline._real_estate_memory_from_history(
            dict(state.get("outbound_memory") or {}),
            history,
        )
        call_surface = str(state.get("call_surface") or "")
        if not NokvoOneVoicePipeline._real_estate_interest_signal(
            memory=memory,
            history=history,
            call_surface=call_surface,
            outbound_context=outbound_context,
        ):
            return None
        args = NokvoOneVoicePipeline._lead_args_from_call_memory(
            memory=memory,
            campaign_context=campaign_context,
            outbound_context=outbound_context,
        )
        if not args.get("phone"):
            return None
        catalog = resolve_index(organization.industry, overrides, custom_tabs)
        tool = catalog.get("leads_create")
        if tool is None:
            return None
        result = await PredefinedToolsService.execute(
            db,
            tenant_res.organization_id,
            None,
            tool,
            args,
            session_id=f"{call_id}:auto_real_estate_lead",
        )
        await db.commit()
        lead_id = result.get("id") or result.get("lead_id")
        metadata = {
            "source": call_surface or "voice_call",
            "auto_created_from_call": True,
            "requested_info": memory.get("requested_info"),
            "purpose": memory.get("purpose"),
            "timeline": memory.get("timeline"),
            "visit_preference": memory.get("visit_preference"),
            "objection": memory.get("objection"),
            "budget_label": memory.get("budget"),
            "campaign_id": (campaign_context or {}).get("campaign_id"),
        }
        if lead_id:
            await NokvoOneVoicePipeline._patch_record_metadata(
                db,
                lead_id,
                {k: v for k, v in metadata.items() if v not in (None, "")},
            )
        await AgentSessionStore.merge_state(
            tenant_res,
            call_id,
            {"auto_lead_created": True, "auto_lead_id": lead_id},
        )
        return {"tool": "leads_create", "arguments": args, "result": result}

    @staticmethod
    async def _patch_record_metadata(
        db: AsyncSession,
        record_id: Any,
        metadata: dict[str, Any],
    ) -> None:
        """Merge ``metadata`` keys into a NokvoOneToolRecord.data after the
        tool has created it. Used to attach confirmation status, audit trail,
        proposed-slot acceptance — fields we can't pass in tool args because
        the tool schemas reject unknown properties."""
        from app.models.nokvo_one_tool_record import NokvoOneToolRecord
        from sqlalchemy import select
        import uuid as _uuid

        try:
            rid = _uuid.UUID(str(record_id))
        except (TypeError, ValueError):
            return
        try:
            res = await db.execute(select(NokvoOneToolRecord).where(NokvoOneToolRecord.id == rid))
            record = res.scalars().first()
            if record is None:
                return
            merged = dict(record.data or {})
            for key, value in metadata.items():
                merged[key] = value
            record.data = merged
            db.add(record)
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass

    @staticmethod
    def _tool_flow_success_answer(result: dict[str, Any], args: dict[str, Any], *, flow_key: str, language: str | None, offer_sms: bool = False) -> str:
        lang = SarvamVoiceService.normalize_language(language)
        assigned_name = result.get("assigned_member_name")
        assignment_status = result.get("assignment_status")
        name = str(args.get("name") or args.get("customer_name") or args.get("phone") or "the customer")
        phone = str(args.get("phone") or args.get("contact_phone") or "").strip()
        # End-of-call SMS offer is opt-in (mirrors clinic flow). Disabled
        # by default because SMS dispatch isn't wired in yet.
        sms_offer = ""
        if offer_sms and phone:
            spoken_phone = " ".join(list(phone[-10:])) if phone[-10:].isdigit() else phone
            if lang == "te":
                sms_offer = f" {spoken_phone} కి confirmation SMS పంపాలా?"
            elif lang == "hi":
                sms_offer = f" क्या {spoken_phone} पर confirmation SMS भेज दूँ?"
            else:
                sms_offer = f" Want me to send a confirmation SMS to {spoken_phone}?"
        if flow_key == "real_estate_site_visit":
            when = str(args.get("visit_at") or "the requested time")
            try:
                parsed = datetime.fromisoformat(when.replace("Z", "+00:00"))
                local_when = parsed.astimezone(_APPOINTMENT_LOCAL_TZ).strftime("%d %b %Y at %I:%M %p")
            except Exception:
                local_when = when
            if lang == "te":
                if assignment_status == "assigned" and assigned_name:
                    return f"Site visit request create అయ్యింది for {name} on {local_when}. It has been assigned to {assigned_name}.{sms_offer}"
                return f"Site visit request create అయ్యింది for {name} on {local_when}. Team availability confirm చేస్తారు.{sms_offer}"
            if lang == "hi":
                if assignment_status == "assigned" and assigned_name:
                    return f"Site visit request create हो गया for {name} on {local_when}. It has been assigned to {assigned_name}.{sms_offer}"
                return f"Site visit request create हो गया for {name} on {local_when}. Team availability confirm करेगी.{sms_offer}"
            if assignment_status == "assigned" and assigned_name:
                return f"I have created the site visit request for {name} on {local_when}. It has been assigned to {assigned_name}.{sms_offer}"
            return f"I have created the site visit request for {name} on {local_when}. The team will confirm availability.{sms_offer}"

        if lang == "te":
            return f"Lead create అయ్యింది for {name}. Team follow up చేస్తారు.{sms_offer}"
        if lang == "hi":
            return f"Lead create हो गया for {name}. Team follow up करेगी.{sms_offer}"
        return f"I have created the lead for {name}. The team will follow up.{sms_offer}"

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
        if tool_flow.get("intent") != "tool_flow" or tool_flow.get("state_slot") != "complete":
            return None
        action = tool_flow.get("action") if isinstance(tool_flow.get("action"), dict) else None
        if not action:
            return None
        context = business_context or await NokvoOneVoicePipeline._voice_business_context(db, tenant_res)
        if context is None:
            return None
        organization, overrides, custom_tabs = context
        catalog = resolve_index(organization.industry, overrides, custom_tabs)
        tool_key = str(action.get("tool_key") or "")
        tool = catalog.get(tool_key)
        if tool is None:
            return None
        raw_args = dict(action.get("arguments") or {})
        flow_key = str(action.get("flow_key") or tool_flow.get("flow_key") or "")
        args: dict[str, Any] = {}
        if flow_key == "real_estate_site_visit":
            try:
                visit_date = NokvoOneVoicePipeline._parse_appointment_date(raw_args.get("visit_date"))
                visit_time = NokvoOneVoicePipeline._parse_appointment_time(raw_args.get("visit_time"))
            except _AppointmentToolInputError as exc:
                flow_state = dict(((tool_flow.get("state_patch") or {}).get("tool_flow") or {}))
                flow_state["active"] = True
                flow_state["completed"] = False
                flow_state["pending_slot"] = "visit_date" if exc.slot == "preferred_date" else "visit_time"
                return {
                    "answer": exc.answer,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": flow_state["pending_slot"],
                    "route_reason": "tool flow needs exact scheduling detail",
                    "tool_calls": [],
                }
            visit_at = datetime.combine(visit_date, visit_time, tzinfo=_APPOINTMENT_LOCAL_TZ).astimezone(timezone.utc).isoformat()
            args = {
                "name": raw_args.get("name"),
                "phone": raw_args.get("phone"),
                "visit_at": visit_at,
            }
            for key in ("email", "property_type", "budget", "location", "notes"):
                if raw_args.get(key) not in (None, ""):
                    args[key] = raw_args[key]
            extra = {k: v for k, v in raw_args.items() if k not in {*args.keys(), "visit_date", "visit_time"} and v not in (None, "")}
            if extra:
                args["notes"] = "Additional details: " + json.dumps(extra, ensure_ascii=False, default=str)
        else:
            args = {k: v for k, v in raw_args.items() if v not in (None, "")}
        # Same retry shape as the clinic appointment path — reads from spec.
        from app.services.agent_spec import RETRY_POLICY

        result = None
        last_exc: Exception | None = None
        max_inline_attempts = 1 + RETRY_POLICY.inline_retries
        for attempt in range(max_inline_attempts):
            try:
                result = await PredefinedToolsService.execute(
                    db,
                    tenant_res.organization_id,
                    None,
                    tool,
                    args,
                    session_id=call_id,
                )
                await db.commit()
                last_exc = None
                break
            except Exception as exc:  # noqa: BLE001 — voice tool entry, broad catch by design
                last_exc = exc
                if db is not None:
                    try:
                        await db.rollback()
                    except Exception:
                        pass
                if attempt < max_inline_attempts - 1:
                    await asyncio.sleep(RETRY_POLICY.inline_delay_seconds)
        if result is None:
            try:
                from app.services.tool_retry_service import ToolRetryService

                await ToolRetryService.enqueue(
                    db,
                    organization_id=tenant_res.organization_id,
                    tool_key=tool.key,
                    arguments=args,
                    context={
                        "call_id": call_id,
                        "language": language,
                        "intent": "tool_flow",
                        "flow_key": flow_key,
                    },
                    last_error=str(last_exc) if last_exc else None,
                )
            except Exception:
                pass
            flow_state = dict(((tool_flow.get("state_patch") or {}).get("tool_flow") or {}))
            flow_state["active"] = False
            flow_state["tool_error"] = str(last_exc)[:180]
            flow_state["needs_callback"] = True
            from app.services.flow_session import append_audit_trail
            append_audit_trail(flow_state, "tool_retry_enqueued", detail=str(last_exc)[:200] if last_exc else None)
            lang = SarvamVoiceService.normalize_language(language)
            phone_hint = str(args.get("phone") or args.get("contact_phone") or "this number")
            spoken_phone = " ".join(list(phone_hint[-10:])) if phone_hint[-10:].isdigit() else phone_hint
            if lang == "te":
                fallback = (
                    f"Details అన్నీ note చేశాను, kāni system temporarily unavailable. "
                    f"Team మీకు {spoken_phone} mīda call back chestāru — booking miss avadu."
                )
            elif lang == "hi":
                fallback = (
                    f"मेरे पास सारी जानकारी है, पर system अभी temporarily unavailable है. "
                    f"Team {spoken_phone} पर call back करेगी — booking miss नहीं होगी."
                )
            else:
                fallback = (
                    f"I have all your details, but I'm having trouble saving them right now. "
                    f"The team will call you back on {spoken_phone} to confirm — your request won't be missed."
                )
            return {
                "answer": fallback,
                "state_patch": {"tool_flow": flow_state},
                "state_slot": "tool_error",
                "route_reason": "tool flow tool failed after retry",
                "tool_calls": [{"tool": tool.key, "arguments": args, "ok": False, "error": str(last_exc)[:240]}],
            }

        flow_state = dict(((tool_flow.get("state_patch") or {}).get("tool_flow") or {}))
        flow_state.update(
            {
                "active": False,
                "completed": True,
                "created_record_id": result.get("id") or result.get("lead_id") or result.get("callback_id"),
                "assignment_status": result.get("assignment_status"),
                "assigned_member_name": result.get("assigned_member_name"),
            }
        )
        # Same as the clinic path: patch confirmation/audit metadata onto the
        # persisted record now that we have its id.
        record_metadata: dict[str, Any] = {}
        for key in ("confirmations", "audit_trail", "proposed_slot_accepted"):
            value = flow_state.get(key)
            if value:
                record_metadata[key] = value
        created_id = flow_state.get("created_record_id")
        if record_metadata and created_id and db is not None:
            await NokvoOneVoicePipeline._patch_record_metadata(db, created_id, record_metadata)

        # Surface-based routing: inbound calls land in tickets, outbound in
        # leads. The macro defaults to creating leads, so we rewrite to
        # tickets when the session is voice_inbound.
        if db is not None and call_id is not None:
            try:
                session_state = await AgentSessionStore.get_state(tenant_res, call_id) or {}
                surface = session_state.get("call_surface")
                ids_to_route = [
                    rid
                    for rid in (
                        result.get("lead_id"),
                        result.get("id"),
                    )
                    if rid
                ]
                if ids_to_route:
                    await NokvoOneVoicePipeline._route_record_by_surface(
                        db,
                        ids_to_route,
                        call_surface=surface,
                        industry=(business_context[0].industry if business_context else None),
                    )
            except Exception:
                pass

        return {
            "answer": NokvoOneVoicePipeline._tool_flow_success_answer(
                result,
                args,
                flow_key=flow_key,
                language=language,
                offer_sms=NokvoOneVoicePipeline._should_offer_sms_confirmation(tenant_res),
            ),
            "state_patch": {"tool_flow": flow_state},
            "state_slot": "complete",
            "route_reason": "tool flow tool executed",
            "tool_calls": [{"tool": tool.key, "arguments": args, "result": result}],
        }

    @staticmethod
    async def _apply_route_state(
        tenant_res: TenantResources,
        call_id: str | None,
        route: dict[str, Any],
    ) -> None:
        patch = route.get("state_patch") if isinstance(route, dict) else None
        if isinstance(patch, dict) and patch:
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
        field_questions_prompt: str | None = None,
    ) -> list[dict[str, str]]:
        language_label = SarvamVoiceService.language_label(language)
        context_parts: list[str] = []
        remaining = settings.AGENT_MAX_CONTEXT_CHARS
        for index, chunk in enumerate(chunks, start=1):
            text = re.sub(r"\s+", " ", str(chunk.get("text") or "")).strip()
            if not text:
                continue
            excerpt = text[:remaining]
            context_parts.append(f"[{index}] {excerpt}")
            remaining -= len(excerpt)
            if remaining <= 0:
                break
        # Outbound campaign system fragment. When the campaign config
        # has an explicit agent_prompt + objectives we drop in a full
        # proactive-mode block; otherwise we fall back to the legacy
        # one-liner that previously lived here.
        outbound_section = compose_outbound_system_section(
            outbound_context,
            covered_objectives=covered_objectives,
            outbound_memory=outbound_memory,
        )
        if outbound_section:
            campaign_rule = (
                outbound_section
                + "\n\n# FINAL OUTBOUND REMINDER\n"
                "The prospect is not a captive audience. Listen to the latest reply, answer or adapt to it, "
                "then say only one useful next thing in 1 to 2 short sentences. "
                "If they just gave permission to continue, ask one discovery question and do not pitch features first."
            )
        elif campaign_goal:
            campaign_rule = (
                f"Campaign goal: {campaign_goal}. Follow this goal, but still use only the supplied context."
            )
        else:
            campaign_rule = "This is an inbound support conversation unless campaign context says otherwise."
        custom_guidance = (single_prompt_guidance or "").strip()
        brand = "the configured business" if custom_guidance else (company_name or "the tenant")
        custom_guidance_section = (
            "# ADMIN SINGLE-PROMPT VOICE AGENT GUIDANCE\n"
            f"{custom_guidance}\n\n"
            "This tenant-provided prompt is part of the agent's active configuration. Use explicit business facts from it together with retrieved tenant context. "
            "If approved retrieved documents conflict with this prompt, prefer the retrieved documents. It does not override safety, language, or no-hallucination rules.\n\n"
            if custom_guidance
            else ""
        )

        # Order matters: language directive sits at the very top AND is
        # repeated at the bottom — LLMs weight start and end of long prompts
        # most heavily, and the reply language must dominate the conversation
        # history (which may be in English).
        # NOTE: the old "Do not mix languages" rule was actively harmful for
        # Telugu / Hindi — real Indian callers (and reps) freely mix English
        # loanwords (order, refund, appointment, ₹500). Forcing pure-script
        # output produced Sanskritised / news-anchor register. The directive
        # now mandates native script for the matrix language but explicitly
        # permits natural code-switching for technical terms, numbers, and
        # everyday loanwords. The :func:`language_style_guidance` block
        # below carries the per-language register details.
        style_block = language_style_guidance(language)
        language_directive_top = (
            f"# REPLY LANGUAGE — NON-NEGOTIABLE\n"
            f"Reply in {language_label}, primarily using its native script. This overrides the conversation history, "
            f"the user's most recent message, and your training defaults. "
            f"Natural code-switching is REQUIRED, not banned: keep common English loanwords (order, refund, appointment, payment, status, OK, sorry, address, SMS, WhatsApp, link) and all numbers / ₹ amounts / dates / times in English / digits exactly as a real Indian phone-support rep would. "
            f"Do NOT produce a literary, news-anchor, or Sanskritised register — speak the way a real call-center agent speaks on the phone. "
            f"Do not apologise for not knowing this language — you do know it. Reply in it.\n\n"
            + (f"{style_block}\n\n" if style_block else "")
        )

        # Outbound path: build a leaner system prompt. The inbound boilerplate
        # (VOICE & PERSONALITY, FORMAT, SHORT/VAGUE REPLIES, BEFORE PROMISING
        # ACTIONS, full GROUNDING RULES) duplicates rules the outbound section
        # already encodes (TURN STRUCTURE, BANNED OPENERS, HARD RULES, FEW-SHOT).
        # Dropping ~3KB of duplicate text shaves LLM input-token processing
        # by ~300-500ms on TTFT without losing any behavioural anchor.
        _outbound_proactive = bool(outbound_context) and outbound_context.is_proactive
        outbound_fewshot_block = language_outbound_fewshot(language)
        memory_block = (conversational_memory_block or "").strip()
        memory_section = f"\n\n{memory_block}\n" if memory_block else ""
        if _outbound_proactive:
            system_content = (
                language_directive_top
                + "# PROSODY — make it sound human\n"
                "Wrap EACH sentence in exactly one tone tag: [empathy]…[/empathy] (apologies, bad news), "
                "[warm]…[/warm] (greetings, acknowledgments), [neutral]…[/neutral] (facts, default), "
                "[excited]…[/excited] (good news), [question]…[/question] (direct questions). "
                "Tags are stripped before speaking — they only set the voice's tone.\n\n"
                + campaign_rule
                + memory_section
                + (f"\n\n{outbound_fewshot_block}" if outbound_fewshot_block else "")
                + f"\n\n# REMINDER\nReply in {language_label} with natural English code-switching for loanwords, numbers, and ₹ amounts. Keep it to 1-2 sentences."
            )
            messages: list[dict[str, str]] = [{"role": "system", "content": system_content}]
            # Outbound conversations are short by design — keep the last 6
            # turns instead of 8 to trim a couple hundred more input tokens.
            for turn in history[-6:]:
                role = turn.get("role") if turn.get("role") in {"user", "assistant"} else "user"
                messages.append({"role": role, "content": str(turn.get("content") or "")[:600]})
            user_content = (
                f"Latest prospect reply — respond to this first:\n{query}\n\n"
                f"Campaign brief context, if needed:\n{chr(10).join(context_parts)}\n\n"
                f"Reply in {language_label}."
            )
            messages.append({"role": "user", "content": user_content})
            return messages

        system_content = (
            language_directive_top
            + f"You are Nokvo One's live voice agent for {brand}. Talk like a real person on a phone call — "
            "not a help-center bot.\n\n"
            "# PROSODY — make it sound human\n"
            "Your reply is going to be spoken aloud. Wrap EACH sentence in exactly one of these tone tags:\n"
            "  [empathy]…[/empathy]   — apologies, bad news, 'sorry to hear that'. Slower, softer.\n"
            "  [warm]…[/warm]         — greetings, acknowledgments, 'of course', 'got it'.\n"
            "  [neutral]…[/neutral]   — facts, policies, statements. DEFAULT.\n"
            "  [excited]…[/excited]   — good news, enthusiasm.\n"
            "  [question]…[/question] — direct questions.\n"
            "Examples:\n"
            "  [empathy]Oh, that's frustrating.[/empathy] [question]What's your order number?[/question]\n"
            "  [warm]Of course.[/warm] [neutral]Refunds within 2 minutes go back to your original payment method.[/neutral]\n"
            "Tags are stripped before being spoken; they only control the voice's tone. Most replies are mostly [neutral] with one warm or empathic opener.\n\n"
            "# VOICE & PERSONALITY\n"
            f"{custom_guidance_section}"
            "- Use contractions: 'I'll', 'you're', 'let's' — same in every language (equivalent informal forms).\n"
            "- Open with quick acknowledgments — 'Sure', 'Got it', 'Of course', 'Okay', 'Right' — not 'I understand your concern' or 'Thank you for reaching out'.\n"
            "- When the caller is frustrated, hurt, or angry: ACKNOWLEDGE the feeling first in one short phrase ('Oh that's frustrating', 'Sorry to hear that'), THEN help. Don't skip to 'please provide your order number'.\n"
            "- Replace stiff phrases: 'Please provide your order number' → 'What's your order number?' · 'I will assist you' → 'Yeah, I can help' · 'Kindly hold on' → 'One sec' · 'How may I help you today?' → 'What can I help you with?'\n"
            "- Vary openers across turns. Don't start every reply with the same word.\n\n"
            "# FORMAT\n"
            "- Keep replies SHORT — 1 to 3 sentences. The first must be immediately useful.\n"
            "- Be specific: name the policy, the threshold, the ₹ amount, the time limit — whatever's in the context.\n"
            "- No markdown, bullets, lists, filenames, or citations.\n\n"
            "# USE THE CONVERSATION\n"
            "- If the caller mentioned an order number, name, or issue earlier, USE IT — don't ask again.\n"
            "- If they correct you, briefly acknowledge ('Ah, my mistake') and adjust. Don't repeat the same wrong assumption.\n"
            "- React to what they just said before launching into your answer.\n\n"
            "# BEFORE PROMISING ACTIONS\n"
            "You cannot actually cancel orders, issue refunds, or escalate from this call. Before saying 'I'll cancel' or 'I'll refund':\n"
            "- Ask for the order number / customer details if you don't have them.\n"
            "- Say the next step — 'I'll pass this to our cancellation team' — not 'I've cancelled it'.\n\n"
            "# SHORT OR VAGUE REPLIES ('yes', 'ok', 'hmm', 'hi')\n"
            "- Don't assume what they want. Don't pull a cancellation/refund topic out of thin air.\n"
            "- If you asked a question last turn, treat their short reply as the answer to that.\n"
            "- If you didn't ask anything specific, respond openly: 'What can I help you with?'\n\n"
            "# GROUNDING RULES — non-negotiable for company-specific facts\n"
            "1. Answer only with facts stated explicitly in the retrieved tenant context or the active admin single-prompt guidance. "
            "If the user's question is partially covered, state EVERY relevant fact the context contains (city, name, hours, number, etc.) and ONLY then say the remaining detail must be confirmed by support. "
            "Refuse only when nothing in the context is relevant.\n"
            "1a. CRITICAL FOR NON-ENGLISH REPLIES: When responding in Telugu, Hindi, Tamil, Bengali, Marathi, Gujarati, Kannada, Malayalam, Punjabi, or Urdu — NEVER use 'I don't have that information' if the retrieved context or admin guidance contains ANY relevant fact. "
            "Translate the facts you DO have into the target language and share them. Only after sharing should you note what's missing. "
            "Example: caller asks 'where is the clinic?' in Telugu, context says 'clinic is in KPHB, Hyderabad'. CORRECT response: share KPHB+Hyderabad in Telugu and offer to follow up for the exact street. INCORRECT: 'I don't have the exact address'.\n"
            "1b. AMBIGUOUS PRONOUNS — when the user says 'where is it', 'how much is this', 'when do they open', or similar pronoun-style questions without explicit subject, "
            "assume they're asking about the business (the clinic / restaurant / store this agent represents). Apply the context to THAT subject.\n"
            "2. Never invent, infer, generalize, or guess. Do not stitch unrelated context fragments into a combined answer. "
            "Do not import outside knowledge about refunds, cancellations, payments, delivery, accounts, or any other policy.\n"
            "3. Forbidden hedge words for policy: 'typically', 'usually', 'generally', 'normally', 'often', 'in most cases', 'should be', 'I think', 'I believe'. "
            "Policy facts are either in the context (state them precisely) or unknown (defer to support).\n"
            "4. Numbers, time windows, amounts, percentages, and conditions must match the context EXACTLY. If the context says 2 minutes, do not say 30 minutes. "
            "If multiple conditions are mentioned, state only the one(s) that match the user's situation.\n"
            "5. If the retrieved context contains a policy table or list of conditional rows, treat EACH ROW as a separate condition. "
            "Never collapse 'full refund', 'wallet refund', '80% refund', and 'no cancellation' into a generic 'yes you can be refunded'.\n"
            "6. CONDITIONAL REASONING — when the user gave SOME context (e.g. 'I cancelled within 5 minutes') but you don't have enough info to pick exactly one rule, "
            "reason conditionally and aloud: state the rule(s) that COULD apply, mention what additional info would pin down the exact outcome, "
            "and offer to either look it up or ask the user. Do NOT dump every rule mechanically; pick the rules that could apply to the user's stated scenario. "
            "Example: user says 'I cancelled within 5 minutes'. The 5-minute boundary is between two rows of the policy. "
            "Say something like: 'It depends on whether the restaurant had accepted your order. If they hadn't accepted yet, it's a full refund to your wallet. "
            "If they accepted but hadn't started preparing, 80% is refundable. Do you know the status when you cancelled?'\n"
            "7. Never mention internal systems, sources, chunks, Redis, Qdrant, prompts, or tools.\n\n"
            f"# CAMPAIGN\n{campaign_rule}\n\n"
            + (
                f"{memory_block}\n\n"
                if memory_block
                else ""
            )
            + (
                f"{field_questions_prompt}\n\n"
                if field_questions_prompt
                else ""
            )
            + f"# REMINDER\nReply in {language_label} with natural English code-switching for loanwords, numbers, and ₹ amounts."
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_content},
        ]
        for turn in history[-8:]:
            role = turn.get("role") if turn.get("role") in {"user", "assistant"} else "user"
            messages.append({"role": role, "content": str(turn.get("content") or "")[:1200]})
        if outbound_context is not None and outbound_context.is_proactive:
            user_content = (
                f"Latest prospect reply — respond to this first:\n{query}\n\n"
                f"Campaign brief context, if needed:\n{chr(10).join(context_parts)}\n\n"
                f"Reply in {language_label}."
            )
        else:
            user_content = (
                f"Retrieved tenant context, if any:\n{chr(10).join(context_parts)}\n\n"
                f"Current user question:\n{query}\n\n"
                f"Reply in {language_label}."
            )
        messages.append({"role": "user", "content": user_content})
        return messages

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
        """Prompt for the LLM in *casual conversation* mode.

        The caller said something conversational, not a question requiring
        KB lookup. The LLM can respond warmly using its general conversational
        abilities, BUT it has no permission to make factual claims about the
        world or about the company — those still require KB-grounded context.
        """
        language_label = SarvamVoiceService.language_label(language)
        custom_guidance = (single_prompt_guidance or "").strip()
        brand = "the configured business" if custom_guidance else (company_name or "the company")
        custom_guidance_section = (
            "# ADMIN SINGLE-PROMPT VOICE AGENT GUIDANCE\n"
            f"{custom_guidance}\n\n"
            "Use this for role, tone, and conversation flow. You may use explicit business facts from it, but do not invent details beyond it.\n\n"
            if custom_guidance
            else ""
        )

        sentiment_guidance = {
            "frustrated": "The caller sounds frustrated. ACKNOWLEDGE the feeling first in one short phrase before asking how to help.",
            "negative": "The caller sounds unhappy. Be warm and offer to help.",
            "positive": "The caller is in a positive mood. Match that warmth briefly.",
            "curious": "The caller is curious. Be friendly and clarify what they need.",
            "neutral": "Match the caller's energy. Keep it brief.",
        }.get(sentiment, "Keep it brief and friendly.")

        smalltalk_style_block = language_style_guidance(language)
        system_content = (
            f"# REPLY LANGUAGE — NON-NEGOTIABLE\n"
            f"Reply in {language_label}, primarily using its native script. "
            f"Natural code-switching is REQUIRED — keep common English loanwords (order, refund, appointment, payment, status, OK, sorry, link, SMS) and all numbers / dates / times in English exactly as a real Indian rep would. "
            f"Do NOT produce a literary or news-anchor register.\n\n"
            + (f"{smalltalk_style_block}\n\n" if smalltalk_style_block else "")
            + f"You are Nokvo One's live voice agent for {brand}. The caller just said something CONVERSATIONAL — a greeting, thank-you, acknowledgment, casual remark, or expression of feeling. Not a factual question about the company.\n\n"
            "# RESPONSE STYLE\n"
            f"{custom_guidance_section}"
            "- One or two short sentences. Voice-first — keep it crisp.\n"
            "- Use contractions: 'I'll', 'you're', 'let's'.\n"
            "- Sound like a real person on a phone call, not a help-center bot.\n"
            f"- {sentiment_guidance}\n\n"
            "# PROSODY — wrap EACH sentence in ONE tone tag\n"
            "  [empathy]…[/empathy]   apologies, bad news, 'sorry to hear'.\n"
            "  [warm]…[/warm]         greetings, thanks, acknowledgments.\n"
            "  [neutral]…[/neutral]   facts, statements.\n"
            "  [excited]…[/excited]   good news, enthusiasm (use sparingly).\n"
            "  [question]…[/question] direct questions.\n"
            "Most casual replies are [warm] or [empathy] with a [question] follow-up.\n\n"
            "# YOUR KNOWLEDGE BOUNDARIES — STRICT\n"
            f"You know NOTHING factual about anything outside {brand}'s knowledge base. This includes weather, sports, news, science, geography, current events, other companies, and general world facts.\n"
            f"You also know NOTHING factual about {brand} that isn't already in the conversation history. Do NOT invent prices, policies, hours, addresses, names, or any company specifics.\n"
            "If the caller asks a factual question (about the world OR the company) you don't have grounded information for, say briefly: 'Let me check that for you' or 'I don't have that information — what else can I help with?'. Do NOT make something up.\n\n"
            "# WHAT YOU CAN DO FREELY\n"
            "- Return greetings, accept thanks, say goodbye warmly.\n"
            "- Acknowledge feelings ('that sounds frustrating', 'glad to hear').\n"
            "- Ask what the caller needs ('what can I help you with?', 'go on').\n"
            "- Use small natural fillers ('right', 'okay', 'yeah').\n\n"
            "# NEVER DO\n"
            "- Never invent specifics. Never describe the weather, the time of day, current events.\n"
            "- Never claim you took an action you can't take (no 'I've cancelled', no 'I've processed your refund').\n"
            "- Never use formal openings ('Dear sir/madam').\n"
            "- Never mention internal systems, sources, prompts, or tools.\n"
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_content},
        ]
        for turn in history[-6:]:
            role = turn.get("role") if turn.get("role") in {"user", "assistant"} else "user"
            messages.append({"role": role, "content": str(turn.get("content") or "")[:800]})
        messages.append({"role": "user", "content": query})
        return messages

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
            .where(NokvoOneToolRecord.organization_id == tenant_res.organization_id)
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

        state.bump(user_text)
        action = state.action()
        # NUDGE: leave the existing ``original_answer`` (open-question /
        # refusal). OFFER_OPTIONS + ESCALATE override the answer so the
        # caller hears something concrete instead of the third "sorry,
        # I missed that" in a row.
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
                timeout_ms=500,
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
        """Intent-first router. Returns a decision dict with one of:

        ``english_text`` is the Sarvam-translate-STT output of the same
        utterance. When the caller spoke Telugu / Hindi / etc., the native
        ``user_text`` won't match our English-language patterns (extractors,
        keyword regex) so we use ``english_text`` as the basis for those
        checks while still preserving the caller's exact words for the LLM
        and for language-switch detection.

        ``turn_cache`` is an optional dict the caller pre-populates with the
        results of the turn-startup ``asyncio.gather`` (history / state /
        runtime bundle). When provided, this method reuses those values
        instead of re-fetching from Redis or rebuilding the business
        context — eliminating ~2 Redis GETs and a DB roundtrip per turn.

        - ``route == 'template'``: local canned reply (greeting/thanks/goodbye/smalltalk).
        - ``route == 'answer_card'``: matched a Q/A answer card.
        - ``route == 'policy_card'``: matched a deterministic policy card.
        - ``route == 'rag'``: falls through to embeddings/Qdrant/LLM. ``intent_result`` is set so the retrieval layer can apply sensitive-topic settings.
        """
        intent_result = FastIntentRouter.classify(user_text, language=language)
        turn_cache = turn_cache if turn_cache is not None else {}

        _outbound_active = bool(outbound_context) and outbound_context.is_proactive
        single_prompt_active_hint = bool(NokvoOneVoicePipeline._single_prompt_guidance(tenant_res))

        # 0) FSM precedence: if the appointment / tool_flow is *expecting* a
        # yes-or-no answer this turn (slot offered, name to confirm, phone to
        # confirm, etc.), the SMALLTALK fast-path must NOT short-circuit with
        # "Sure, go ahead." A bare "Yes" must reach the FSM so it can lock
        # the booking. We probe state once and skip the template branch when
        # any awaiting_* flag is set.
        state_pre_check = await NokvoOneVoicePipeline._turn_state(
            tenant_res, call_id, turn_cache
        )
        suppress_template = False
        if isinstance(state_pre_check, dict):
            appt_pre = state_pre_check.get("appointment") or {}
            tool_pre = state_pre_check.get("tool_flow") or {}
            awaiting_flags = (
                "awaiting_slot_confirm",
                "awaiting_name_confirmation",
                "awaiting_phone_confirmation",
                "awaiting_id_confirmation",
                "awaiting_past_time_shift",
            )
            appointment_awaiting = False if (_outbound_active or single_prompt_active_hint) else any(
                bool(appt_pre.get(flag)) for flag in awaiting_flags
            )
            tool_awaiting = any(bool(tool_pre.get(flag)) for flag in awaiting_flags)
            suppress_template = appointment_awaiting or tool_awaiting

        # 1) Greeting / thanks / goodbye / smalltalk — no LLM, no cache, no embeddings.
        templated = NokvoOneVoicePipeline._template_reply(intent_result.intent, language, company_name)
        if templated and not suppress_template and not _outbound_active:
            if intent_result.intent == INTENT_GREETING and single_prompt_active_hint:
                return {
                    "route": "smalltalk_llm",
                    "answer": None,
                    "intent_result": intent_result,
                    "safe_to_cache": False,
                    "sensitive": False,
                    "classified": {
                        "intent": "smalltalk",
                        "needs_kb": False,
                        "sentiment": "neutral",
                        "reason": "single prompt greeting override",
                    },
                }
            if (
                single_prompt_active_hint
                and intent_result.intent == INTENT_SMALLTALK
                and NokvoOneVoicePipeline._is_short_permission_reply(user_text)
            ):
                history_for_template = await NokvoOneVoicePipeline._turn_history(
                    tenant_res, call_id, turn_cache
                )
                last_assistant = NokvoOneVoicePipeline._last_assistant_text(history_for_template)
                if NokvoOneVoicePipeline._assistant_asked_for_user_decision(last_assistant):
                    return {
                        "route": "smalltalk_llm",
                        "answer": None,
                        "intent_result": intent_result,
                        "safe_to_cache": False,
                        "sensitive": False,
                        "classified": {
                            "intent": "smalltalk",
                            "needs_kb": False,
                            "sentiment": "neutral",
                            "reason": "single prompt contextual permission reply",
                        },
                    }
            return {
                "route": "template",
                "answer": templated,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": False,
            }

        # 2) Answer-card cache (existing Q/A card lookup).
        card = None if _outbound_active else AgentKnowledgeService.find_answer_card(tenant_res, user_text, language)
        if card and card.get("answer"):
            return {
                "route": "answer_card",
                "answer": str(card["answer"]),
                "intent_result": intent_result,
                "safe_to_cache": bool(card.get("cacheable", True)) and not intent_result.sensitive,
                "sensitive": intent_result.sensitive,
                "card_id": card.get("id"),
            }

        history_for_turn = await NokvoOneVoicePipeline._turn_history(tenant_res, call_id, turn_cache)
        state_for_turn = await NokvoOneVoicePipeline._turn_state(tenant_res, call_id, turn_cache)
        prior_appointment = dict((state_for_turn or {}).get("appointment") or {})
        prior_in_booking_flow = bool(prior_appointment.get("active")) and not (
            prior_appointment.get("completed") and not prior_appointment.get("pending_slot")
        )
        prior_pending_slot = prior_appointment.get("pending_slot")

        # Clinic FSM gate. The appointment slot-fill ("patient name", "eye
        # concern", "urgent symptoms", "follow-up?") is hard-wired
        # ophthalmology language — it leaks into real-estate, hospitality,
        # ecommerce, and any single-prompt tenant the moment we let it run.
        #
        # Rules:
        #   1. Outbound calls NEVER run it (the outbound LLM owns dialogue).
        #   2. Single-prompt tenants NEVER run it ("I drive the agent
        #      myself" — adding deterministic clinic prompts on top of the
        #      operator's persona is a bug).
        #   3. Industry must be explicitly ``clinics``. An empty/unknown
        #      industry MUST default to OFF — the previous code defaulted
        #      to ON, which is how a real-estate tenant ended up being
        #      asked about "eye concerns".
        bundle = await NokvoOneVoicePipeline._turn_bundle(db, tenant_res, turn_cache)
        industry = ""
        bundle_single_prompt_enabled = False
        if bundle is not None:
            industry = str(bundle.organization_industry or "").strip()
            bundle_single_prompt_enabled = bool(getattr(bundle, "single_prompt_enabled", False))
        if not industry:
            try:
                context_for_industry = await NokvoOneVoicePipeline._voice_business_context(db, tenant_res)
            except Exception:
                context_for_industry = None
            if context_for_industry is not None:
                org_obj, _overrides, _tabs = context_for_industry
                if org_obj is not None:
                    industry = str(getattr(org_obj, "industry", "") or "").strip()
        single_prompt_active = bundle_single_prompt_enabled or single_prompt_active_hint
        is_clinic_org = (
            False
            if (_outbound_active or single_prompt_active)
            else (industry.lower() == "clinics")
        )
        if prior_in_booking_flow and not is_clinic_org:
            prior_appointment = {
                **prior_appointment,
                "active": False,
                "completed": True,
                "pending_slot": None,
                "disabled_reason": "appointment_flow_not_enabled_for_account",
            }
            if call_id:
                await AgentSessionStore.merge_state(
                    tenant_res,
                    call_id,
                    {"appointment": prior_appointment},
                )
            prior_in_booking_flow = False
            prior_pending_slot = None

        turn_policy = (
            evaluate_voice_turn_policy(
                user_text,
                history=history_for_turn,
                state=state_for_turn,
                language=language,
            )
            if is_clinic_org
            else None
        )

        # If the regex side-question detector inside evaluate_voice_turn_policy
        # yielded mid-booking, persist a `deferred_for_kb` marker so the next
        # FSM turn can acknowledge the digression with a "Coming back..."
        # prefix. The function itself is sync and can't touch Redis, so we do
        # the merge here.
        if turn_policy is None and prior_in_booking_flow:
            await NokvoOneVoicePipeline._mark_appointment_deferred(
                tenant_res, call_id, prior_appointment
            )

        # LLM digression fallback: if the FSM is about to re-ask the SAME slot
        # (i.e., the caller's input didn't advance the flow), the regex detector
        # missed something. Ask the small LLM classifier with a tight timeout —
        # if it says the caller pivoted (kb_question, complaint, escalation,
        # cancel/refund), bypass the FSM and let the route fall through to RAG
        # or the sensitive-policy handler.
        if (
            turn_policy
            and turn_policy.get("answer")
            and turn_policy.get("intent") == "appointment_flow"
            and prior_in_booking_flow
            and prior_pending_slot
            and turn_policy.get("state_slot") == prior_pending_slot
        ):
            digression = await NokvoOneVoicePipeline._llm_check_booking_digression(
                tenant_res, user_text, history_for_turn
            )
            if digression is not None:
                await NokvoOneVoicePipeline._mark_appointment_deferred(
                    tenant_res, call_id, prior_appointment
                )
                if digression.intent == LLM_INTENT_CANCEL:
                    intent_result = FastIntentRouter._build(
                        INTENT_CANCELLATION_REQUEST, confidence=0.9, reason="llm digression"
                    )
                elif digression.intent == LLM_INTENT_REFUND:
                    intent_result = FastIntentRouter._build(
                        INTENT_REFUND_ELIGIBILITY, confidence=0.9, reason="llm digression"
                    )
                turn_policy = None

        # Availability check is the one policy intent that doesn't carry its
        # own answer — the pipeline must consult the scheduler to fill it in.
        needs_availability_lookup = (
            turn_policy is not None
            and turn_policy.get("intent") == "availability_check"
        )
        if turn_policy and (turn_policy.get("answer") or needs_availability_lookup):
            action = await NokvoOneVoicePipeline._maybe_execute_turn_policy_action(
                tenant_res,
                call_id,
                db,
                turn_policy,
            )
            if action:
                turn_policy["answer"] = action.get("answer") or turn_policy.get("answer") or ""
                turn_policy["state_patch"] = action.get("state_patch") or turn_policy.get("state_patch") or {}
                turn_policy["state_slot"] = action.get("state_slot") or turn_policy.get("state_slot")
                turn_policy["reason"] = action.get("route_reason") or turn_policy.get("reason")
            elif needs_availability_lookup and not turn_policy.get("answer"):
                # No business context (e.g., not a clinic) — fall through to
                # the normal RAG/template path by clearing the intent.
                turn_policy = None
            metadata = {
                **(intent_result.metadata or {}),
                "turn_policy_intent": turn_policy.get("intent"),
                "turn_policy_reason": turn_policy.get("reason"),
                "entities": turn_policy.get("entities") or {},
                "state_slot": turn_policy.get("state_slot"),
                "tool_calls": (action or {}).get("tool_calls") or [],
            }
            return {
                "route": "template",
                "answer": str(turn_policy["answer"]),
                "intent_result": IntentResult(
                    intent=intent_result.intent,
                    topic=intent_result.topic,
                    confidence=max(intent_result.confidence, 0.88),
                    sensitive=intent_result.sensitive,
                    requires_live_status=intent_result.requires_live_status,
                    reason=turn_policy.get("reason") or intent_result.reason,
                    metadata=metadata,
                ),
                "safe_to_cache": False,
                "sensitive": intent_result.sensitive,
                "state_patch": turn_policy.get("state_patch") or {},
                "detected_entities": turn_policy.get("entities") or {},
                "state_slot": turn_policy.get("state_slot"),
                "route_reason": turn_policy.get("reason"),
                "tool_calls": (action or {}).get("tool_calls") or [],
            }

        # Reuse the bundle's tuple when available; fall back to the
        # ``_voice_business_context`` helper so test-stub paths (which
        # monkeypatch only that helper) still hit the tool_flow branch.
        business_context: tuple[Any, dict[str, Any], list[dict[str, Any]]] | None = None
        if bundle is not None:
            business_context = bundle.as_business_context_tuple()
        if business_context is None:
            try:
                business_context = await NokvoOneVoicePipeline._voice_business_context(db, tenant_res)
            except Exception:
                business_context = None
        if business_context is not None:
            organization, overrides, custom_tabs = business_context
            prior_tool_flow = dict((state_for_turn or {}).get("tool_flow") or {})
            prior_in_tool_flow = bool(prior_tool_flow.get("active")) and not bool(prior_tool_flow.get("completed"))
            prior_tool_flow_slot = prior_tool_flow.get("pending_slot")
            tool_flow = evaluate_tool_flow_policy(
                user_text,
                business_type=organization.industry,
                schema_overrides=overrides,
                custom_tabs=custom_tabs,
                provider_status=dict(tenant_res.provider_status or {}),
                history=history_for_turn,
                state=state_for_turn,
                language=language,
            )

            # Regex side-question detector inside evaluate_tool_flow_policy
            # returns None when the caller pivots mid-flow. Persist the
            # deferred-for-kb marker so the next turn resumes with a
            # "Coming back to your booking — " prefix on the slot question.
            if tool_flow is None and prior_in_tool_flow:
                await NokvoOneVoicePipeline._mark_tool_flow_deferred(
                    tenant_res, call_id, prior_tool_flow
                )

            # LLM digression fallback: if the FSM is about to re-ask the SAME
            # tool_flow slot (regex extractor failed to advance), check with
            # the small LLM classifier. When it says "kb_question / complaint
            # / escalation / cancel / refund", bypass the FSM so the route
            # falls through to RAG or the sensitive-policy handler.
            if (
                tool_flow
                and tool_flow.get("answer")
                and tool_flow.get("intent") == "tool_flow"
                and prior_in_tool_flow
                and prior_tool_flow_slot
                and tool_flow.get("state_slot") == prior_tool_flow_slot
            ):
                digression = await NokvoOneVoicePipeline._llm_check_booking_digression(
                    tenant_res, user_text, history_for_turn
                )
                if digression is not None:
                    await NokvoOneVoicePipeline._mark_tool_flow_deferred(
                        tenant_res, call_id, prior_tool_flow
                    )
                    if digression.intent == LLM_INTENT_CANCEL:
                        intent_result = FastIntentRouter._build(
                            INTENT_CANCELLATION_REQUEST, confidence=0.9, reason="llm digression"
                        )
                    elif digression.intent == LLM_INTENT_REFUND:
                        intent_result = FastIntentRouter._build(
                            INTENT_REFUND_ELIGIBILITY, confidence=0.9, reason="llm digression"
                        )
                    tool_flow = None

            # Mirror the clinic-flow handling: the tool_flow's availability
            # intent comes back with answer=None — the scheduler fills it in.
            # The previous code's `if tool_flow.get("answer")` guard dropped
            # the response on the floor, never dispatched the scheduler, AND
            # silently discarded the state_patch (offered_disambiguation,
            # pending_slot), so the next turn looped right back into the same
            # availability question. Use a needs_lookup flag instead.
            tool_flow_needs_lookup = (
                tool_flow is not None
                and tool_flow.get("intent") == "availability_check"
            )
            if tool_flow and (tool_flow.get("answer") or tool_flow_needs_lookup):
                if tool_flow_needs_lookup:
                    action = await NokvoOneVoicePipeline._handle_availability_check(
                        tenant_res, db, tool_flow
                    )
                else:
                    action = await NokvoOneVoicePipeline._maybe_execute_tool_flow_action(
                        tenant_res,
                        call_id,
                        db,
                        tool_flow,
                        business_context=business_context,
                        language=language,
                    )
                if action:
                    tool_flow["answer"] = action.get("answer") or tool_flow.get("answer") or ""
                    tool_flow["state_patch"] = action.get("state_patch") or tool_flow.get("state_patch") or {}
                    tool_flow["state_slot"] = action.get("state_slot") or tool_flow.get("state_slot")
                    tool_flow["reason"] = action.get("route_reason") or tool_flow.get("reason")
                elif tool_flow_needs_lookup and not tool_flow.get("answer"):
                    # Scheduler couldn't satisfy the lookup (no assignable
                    # member for this request_type). Fall back to asking the
                    # original missing slot directly — DO persist the
                    # state_patch so offered_disambiguation stays True and
                    # we don't loop.
                    flow_state = dict((tool_flow.get("state_patch") or {}).get("tool_flow") or {})
                    pending = flow_state.get("pending_slot") or "visit_time"
                    business_type_local = (business_context[0].industry if business_context else None)
                    bundle = build_tool_flow_questions(
                        business_type_local,
                        (business_context[1] if business_context else None),
                        (business_context[2] if business_context else None),
                    )
                    slot_question = None
                    for slot_def in ((bundle.get("flows") or {}).get(tool_flow.get("flow_key") or "") or {}).get("slots") or []:
                        if slot_def.get("key") == pending:
                            questions = slot_def.get("questions") or {}
                            slot_question = questions.get(language) or questions.get("en")
                            break
                    tool_flow["answer"] = slot_question or "What time would you prefer?"
                    tool_flow["state_patch"] = {"tool_flow": flow_state}
                    tool_flow["state_slot"] = pending
                metadata = {
                    **(intent_result.metadata or {}),
                    "turn_policy_intent": tool_flow.get("intent"),
                    "turn_policy_reason": tool_flow.get("reason"),
                    "flow_key": tool_flow.get("flow_key"),
                    "state_slot": tool_flow.get("state_slot"),
                    "tool_calls": (action or {}).get("tool_calls") or [],
                }
                # Outbound mode: the tool_flow's regex slot scraper is
                # useful (it still captures slots into state_patch and
                # executes the tool on completion), but its inbound-
                # style canned questions ("May I have your name?",
                # "What date would you prefer?") must NOT become the
                # caller-facing reply — the outbound LLM speaks for the
                # agent. Suppress the template short-circuit unless this
                # turn is the completion (state_slot == "complete") OR
                # a tool was actually executed this turn, in which case
                # the deterministic confirmation ("I've created the
                # site visit request…") is the right user-facing reply.
                _is_completion = (
                    tool_flow.get("state_slot") == "complete"
                    or bool((action or {}).get("tool_calls"))
                )
                # Same rule as the clinic FSM gate above: a single-prompt
                # tenant has explicitly said "I drive the agent myself",
                # so deterministic slot-question text from the tool_flow
                # (e.g., "What's your name?", "What date would you prefer?")
                # must NOT replace the LLM's persona-voiced reply. Slots
                # are still scraped into Redis below and the completion
                # path still fires the tool — only the mid-flow canned
                # question is suppressed.
                _suppress_tool_flow_template = (_outbound_active or single_prompt_active) and not _is_completion
                if _suppress_tool_flow_template:
                    # Persist the scraped slots into the state-patch
                    # path used by the rag branch so the LLM's next
                    # turn sees up-to-date slot data, then fall
                    # through to LLM-driven reply.
                    state_patch_holder: dict[str, Any] = tool_flow.get("state_patch") or {}
                    if state_patch_holder:
                        await NokvoOneVoicePipeline._apply_route_state(
                            tenant_res,
                            call_id,
                            {
                                "state_patch": state_patch_holder,
                                "state_slot": tool_flow.get("state_slot"),
                            },
                        )
                else:
                    return {
                        "route": "template",
                        "answer": str(tool_flow["answer"]),
                        "intent_result": IntentResult(
                            intent=intent_result.intent,
                            topic=intent_result.topic,
                            confidence=max(intent_result.confidence, 0.9),
                            sensitive=intent_result.sensitive,
                            requires_live_status=intent_result.requires_live_status,
                            reason=tool_flow.get("reason") or intent_result.reason,
                            metadata=metadata,
                        ),
                        "safe_to_cache": False,
                        "sensitive": intent_result.sensitive,
                        "state_patch": tool_flow.get("state_patch") or {},
                        "detected_entities": {},
                        "state_slot": tool_flow.get("state_slot"),
                        "route_reason": tool_flow.get("reason"),
                        "tool_calls": (action or {}).get("tool_calls") or [],
                    }

        # 3) Sensitive policy intents (cancellation/refund) → deterministic engine.
        # The set of "sensitive" intents lives in :mod:`agent_spec` so chat /
        # voice / outbound all read the same list.
        from app.services.agent_spec import IDENTITY_POLICY

        if intent_result.intent in IDENTITY_POLICY.sensitive_intents or intent_result.intent in (
            INTENT_CANCELLATION_REQUEST,
            INTENT_REFUND_ELIGIBILITY,
        ):
            # Caller identity gate: before answering anything actionable
            # about a cancellation or refund, require a phone number that
            # matches an existing record. Without this anyone can call in
            # and "cancel my appointment" — a hard policy hole.
            verified = await NokvoOneVoicePipeline._caller_is_verified(
                tenant_res, db, call_id, user_text
            )
            if not verified["verified"]:
                if not verified.get("challenged"):
                    challenge = verified.get("challenge") or (
                        "Before I can change a booking, I need to verify you — "
                        "could you share the phone number the booking is under?"
                    )
                    await AgentSessionStore.set_state(
                        tenant_res, call_id, {"identity_verification_pending": True}
                    )
                    return {
                        "route": "identity_verification",
                        "answer": challenge,
                        "intent_result": intent_result,
                        "safe_to_cache": False,
                        "sensitive": True,
                        "state_patch": {"identity_verification_pending": True},
                        "state_slot": "identity_verification",
                        "route_reason": "identity verification required",
                        "tool_calls": [],
                    }
            policy_cards = NokvoOneVoicePipeline._active_policy_cards(tenant_res)
            # Prefer authoritative live context (CRM/order service) when
            # available, otherwise mine the conversation history for what
            # the caller already told us in prior turns. This is what makes
            # multi-turn cancellation work: agent asks "how long ago did you
            # place it?", caller says "3 minutes", caller asks "can I cancel?"
            # → engine fires with order_age_minutes=3 instead of re-asking.
            #
            # We feed the English-translated transcript to the extractor when
            # it's available. The extractor's patterns are English-only —
            # Telugu "5 మినిట్స్కే క్యాన్సిల్" never matches, but the
            # translated form "cancelled at 5 minutes" does.
            extractor_text = english_text or user_text
            live_context = await fetch_live_order_context(
                tenant_res.tenant_id,
                call_id,
                user_text,
            )
            if not live_context:
                history = await NokvoOneVoicePipeline._turn_history(tenant_res, call_id, turn_cache)
                live_context = extract_live_context_from_history(
                    history,
                    current_user_text=extractor_text,
                )
            provider_status = dict(tenant_res.provider_status or {})
            decision = PolicyDecisionEngine.evaluate(
                intent_result.intent,
                intent_result.topic,
                user_text,
                policy_cards,
                live_context,
                current_policy_version=str(provider_status.get("agent_policy_version") or "") or None,
            )
            # Only terminate the route when we have CONFIDENT signal:
            #   - DEC_EXACT_MATCH    — single condition matched live context
            #                          (age + status pinned a specific rule).
            #   - DEC_MATRIX_RESPONSE — pure general policy question, no
            #                          context given. Returning the full
            #                          matrix is correct.
            # DEC_NO_MATCH (user gave partial context but no clean match) and
            # DEC_LIVE_STATUS_NEEDED both fall through to the RAG path. The
            # LLM reads the policy source text and can reason conditionally
            # ("depending on whether the restaurant accepted, here's what
            # happens"), which is much more human-like than the canned matrix
            # dump. The strict grounding prompt + policy_card_chunks
            # injection keeps it from hallucinating.
            confident_codes = {DEC_EXACT_MATCH, DEC_MATRIX_RESPONSE}
            if decision.answered and decision.answer and decision.decision_code in confident_codes:
                return {
                    "route": "policy_card",
                    "answer": decision.answer,
                    "intent_result": intent_result,
                    "safe_to_cache": decision.safe_to_cache,
                    "sensitive": True,
                    "policy_card_id": decision.matched_card_id,
                    "decision_code": decision.decision_code,
                    "matched_condition": decision.matched_condition,
                }
            # Partial signal — return RAG route DIRECTLY. retrieve() injects
            # policy_card source_text as synthetic chunks when Qdrant comes up
            # empty, so the LLM always has the policy matrix to reason from.
            # We deliberately do NOT fall through to the Tier-2 classifier
            # because Tier 1 already correctly identified the intent — the
            # classifier would just duplicate the engine call.
            return {
                "route": "rag",
                "answer": None,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": True,
            }

        # Outbound short-circuit: by now the tool_flow slot scraping has
        # run (so any slots in this turn are persisted) and any genuine
        # completion already returned with route="template" above. From
        # here down the route would otherwise burn ~500-800ms on inbound
        # Tier-2 LLM intent classification + Qdrant prefetch, neither of
        # which apply to a sales call. Hand control to the outbound LLM.
        if _outbound_active:
            return {
                "route": "rag",
                "answer": None,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": intent_result.sensitive,
                "prefetched_retrieval": None,
            }

        location_retrieval_query = NokvoOneVoicePipeline._business_location_retrieval_rewrite(user_text)
        if location_retrieval_query:
            prefetch_task = asyncio.create_task(
                NokvoOneVoicePipeline.retrieve(
                    tenant_res,
                    NokvoOneVoicePipeline.retrieval_query_for(user_text, english_text),
                    db=db,
                    top_k=top_k,
                    campaign_id=campaign_id,
                    intent_result=intent_result,
                    english_text=english_text or location_retrieval_query,
                    dual_retrieval=code_switching,
                )
            )
            return {
                "route": "rag",
                "answer": None,
                "intent_result": intent_result,
                "safe_to_cache": True,
                "sensitive": False,
                "classified": {
                    "intent": "kb_question",
                    "needs_kb": True,
                    "sensitive": False,
                    "reason": "deterministic business location query",
                },
                "prefetched_retrieval": prefetch_task,
            }

        # Word-count gate: a short non-greeting utterance ("yeah", "uh ok",
        # "but I mean") has no informational intent. But: many Indian
        # languages express a complete question in 2 words ("rifand vastada?"
        # = "will refund come?"), so we ALSO require: no clear question
        # punctuation (?, ।, ؟) and (if we have an English translation)
        # nothing question-shaped in English either.
        clear_question = bool(
            "?" in user_text
            or "؟" in user_text
            or "।" in user_text
            or (english_text and "?" in english_text)
        )
        if (
            intent_result.intent == INTENT_UNKNOWN_GENERAL
            and len(user_text.split()) < settings.AGENT_RAG_MIN_QUERY_WORDS
            and not clear_question
        ):
            nudge = {
                "hi": "हाँ, बताइए।",
                "ta": "சரி, சொல்லுங்கள்.",
                "te": "సరే, చెప్పండి.",
                "bn": "হ্যাঁ, বলুন।",
            }.get(language, "Mm-hm, go on.")
            return {
                "route": "template",
                "answer": nudge,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": False,
            }

        # ── Tier 2: LLM classifier ──
        # Tier 1 regex didn't recognize this utterance. Ask a small LLM to
        # classify what the caller is actually trying to do — handles
        # paraphrasing, code-switching, STT errors, and idioms the regex
        # can't possibly enumerate. Capped at 800ms with a safe default,
        # so a slow/down classifier never blocks the turn.
        #
        # We send the classifier BOTH the native + English-translated forms
        # (when available). Small LLMs handle English best; the native form
        # is the source of truth and is still presented to preserve nuance.
        prefetch_task = asyncio.create_task(
            NokvoOneVoicePipeline.retrieve(
                tenant_res,
                NokvoOneVoicePipeline.retrieval_query_for(user_text, english_text),
                db=db,
                top_k=top_k,
                campaign_id=campaign_id,
                intent_result=intent_result,
                english_text=english_text,
                dual_retrieval=code_switching,
            )
        )
        history = await NokvoOneVoicePipeline._turn_history(tenant_res, call_id, turn_cache)
        classifier_text = (
            f"{user_text}\n(English translation: {english_text})"
            if english_text and english_text.strip() and english_text.strip() != user_text.strip()
            else user_text
        )
        classified = await LLMIntentClassifier.classify(
            classifier_text,
            tenant_res=tenant_res,
            history=history,
        )

        # Promote LLM-detected sensitive intents into Tier-1-style routing so
        # downstream code paths see consistent intent constants. For each
        # case, we also REWRITE the IntentResult so logging + retrieve()
        # filters use the correct topic.
        if classified.intent == LLM_INTENT_CANCEL:
            intent_result = FastIntentRouter._build(
                INTENT_CANCELLATION_REQUEST, confidence=0.9, reason="llm classifier"
            )
        elif classified.intent == LLM_INTENT_REFUND:
            intent_result = FastIntentRouter._build(
                INTENT_REFUND_ELIGIBILITY, confidence=0.9, reason="llm classifier"
            )

        # Sensitive policy intents — re-run the policy_card path. This catches
        # cancellation/refund questions that Tier 1's regex missed (paraphrasing,
        # other languages, STT typos).
        if intent_result.intent in (INTENT_CANCELLATION_REQUEST, INTENT_REFUND_ELIGIBILITY):
            NokvoOneVoicePipeline._cancel_retrieval_task(prefetch_task)
            policy_cards = NokvoOneVoicePipeline._active_policy_cards(tenant_res)
            live_context = await fetch_live_order_context(
                tenant_res.tenant_id,
                call_id,
                user_text,
            )
            if not live_context:
                # Use the English translation for extraction; native Telugu /
                # Hindi won't match the English-only patterns.
                live_context = extract_live_context_from_history(
                    history,
                    current_user_text=english_text or user_text,
                )
            provider_status = dict(tenant_res.provider_status or {})
            decision = PolicyDecisionEngine.evaluate(
                intent_result.intent,
                intent_result.topic,
                user_text,
                policy_cards,
                live_context,
                current_policy_version=str(provider_status.get("agent_policy_version") or "") or None,
            )
            confident_codes = {DEC_EXACT_MATCH, DEC_MATRIX_RESPONSE, DEC_NO_MATCH}
            if decision.answered and decision.answer and decision.decision_code in confident_codes:
                return {
                    "route": "policy_card",
                    "answer": decision.answer,
                    "intent_result": intent_result,
                    "safe_to_cache": decision.safe_to_cache,
                    "sensitive": True,
                    "policy_card_id": decision.matched_card_id,
                    "decision_code": decision.decision_code,
                    "matched_condition": decision.matched_condition,
                    "classified": classified.to_dict(),
                }
            # Fall through to RAG (with policy_card_chunks injected by retrieve()).

        # ── Out-of-scope: verify by retrieval before deflecting ──
        # The classifier sometimes mis-labels operational questions ("can I
        # get an appointment today?", "which clinic is this?") as
        # out_of_scope. Before sending the canned deflection, run retrieval.
        # If the KB has a relevant chunk, the classifier was wrong — let RAG
        # answer it. Only deflect when the KB genuinely has nothing.
        if classified.intent == LLM_INTENT_OUT_OF_SCOPE:
            probe = await NokvoOneVoicePipeline._await_prefetched_retrieval(
                {"prefetched_retrieval": prefetch_task}
            )
            if probe is None:
                probe = await NokvoOneVoicePipeline.retrieve(
                    tenant_res,
                    NokvoOneVoicePipeline.retrieval_query_for(user_text, english_text),
                    db=db,
                    top_k=top_k,
                    campaign_id=campaign_id,
                    intent_result=intent_result,
                    english_text=english_text,
                    dual_retrieval=code_switching,
                )
            probe_chunks = probe.get("chunks") or []
            if probe_chunks:
                # KB has relevant content → fall through to the normal RAG
                # path. Stash the probe so retrieve() doesn't re-run; we'll
                # surface it via the `prefetched_retrieval` route hint.
                print(
                    f"[NOKVO-VOICE] classifier said out_of_scope but retrieval "
                    f"found {len(probe_chunks)} chunks — overriding to RAG"
                )
                return {
                    "route": "rag",
                    "answer": None,
                    "intent_result": intent_result,
                    "safe_to_cache": False,
                    "sensitive": False,
                    "classified": classified.to_dict(),
                    "prefetched_retrieval": probe,
                }
            if NokvoOneVoicePipeline._single_prompt_guidance(tenant_res):
                return {
                    "route": "rag",
                    "answer": None,
                    "intent_result": intent_result,
                    "safe_to_cache": False,
                    "sensitive": False,
                    "classified": classified.to_dict(),
                    "prefetched_retrieval": probe,
                }
            # KB really has nothing → friendly redirect template.
            brand = company_name or "us"
            msg = {
                "hi": f"मैं केवल {brand} से जुड़े सवालों में मदद कर सकता हूँ। और कुछ बताइए?",
                "ta": f"நான் {brand} தொடர்பான கேள்விகளில் மட்டுமே உதவ முடியும். வேறு என்ன உதவி வேண்டும்?",
                "te": f"నేను {brand}కి సంబంధించిన ప్రశ్నలకు మాత్రమే సహాయం చేయగలను. మరేమైనా కావాలా?",
                "bn": f"আমি শুধু {brand} সম্পর্কিত প্রশ্নে সাহায্য করতে পারি। আর কিছু?",
            }.get(language, f"I don't have that information — I'm here to help with {brand}. What else can I do for you?")
            return {
                "route": "template",
                "answer": msg,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": False,
                "classified": classified.to_dict(),
            }

        # ── Pure smalltalk → LLM in conversational mode, no RAG ──
        # The classifier said this is chitchat. Let the LLM respond like a
        # human (using its conversational ability), but the smalltalk prompt
        # forbids it from inventing world or company facts.
        if classified.intent == LLM_INTENT_SMALLTALK and not classified.needs_kb:
            NokvoOneVoicePipeline._cancel_retrieval_task(prefetch_task)
            return {
                "route": "smalltalk_llm",
                "answer": None,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": False,
                "classified": classified.to_dict(),
            }

        # ── Escalation request ──
        if classified.intent == LLM_INTENT_ESCALATION:
            NokvoOneVoicePipeline._cancel_retrieval_task(prefetch_task)
            msg = {
                "hi": "ज़रूर, मैं इसे सपोर्ट टीम को आगे भेज देता हूँ।",
                "ta": "சரி, இதை ஆதரவு குழுவிற்கு அனுப்புகிறேன்.",
                "te": "సరే, దీన్ని సపోర్ట్ టీమ్‌కు పంపుతున్నాను.",
            }.get(language, "Sure, I'll transfer this to support. One moment.")
            return {
                "route": "template",
                "answer": msg,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": False,
                "classified": classified.to_dict(),
            }

        # ── Everything else → RAG path ──
        # kb_question, complaint, order_status, unclear — all need retrieval.
        # The downstream prompt is strict about not inventing company facts.
        # classified.sensitive carries through so retrieve() applies tighter
        # thresholds + topic filters when appropriate.
        merged_sensitive = bool(intent_result.sensitive or classified.sensitive)
        if merged_sensitive:
            NokvoOneVoicePipeline._cancel_retrieval_task(prefetch_task)
        return {
            "route": "rag",
            "answer": None,
            "intent_result": IntentResult(
                intent=intent_result.intent,
                topic=intent_result.topic,
                confidence=intent_result.confidence,
                sensitive=merged_sensitive,
                requires_live_status=intent_result.requires_live_status,
                reason=intent_result.reason,
                metadata={**(intent_result.metadata or {}), "llm_classifier": classified.to_dict()},
            ),
            "safe_to_cache": not merged_sensitive,
            "sensitive": merged_sensitive,
            "classified": classified.to_dict(),
            "prefetched_retrieval": None if merged_sensitive else prefetch_task,
        }

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
        started = perf_counter()
        user_text = _normalize(query)
        language = SarvamVoiceService.normalize_language(response_language)

        # Parallel turn startup: history fetch, state fetch, and the
        # per-tenant runtime bundle all run concurrently. Without this
        # primer the pipeline would fetch each value separately as it was
        # needed, paying a full Redis round trip every time.
        turn_cache = await NokvoOneVoicePipeline._prime_turn_cache(db, tenant_res, call_id)
        history = (conversation_history or []) + list(turn_cache.get("history") or [])

        # English-translated transcript (when caller spoke a non-English
        # language). retrieval_text holds the translate-STT output from the
        # voice-stream service — use it for extractor + classifier where
        # English patterns are required, while user_text stays the source
        # of truth for prompts.
        english_text = retrieval_text if retrieval_text and _normalize(retrieval_text) != user_text else None
        retrieval_query = NokvoOneVoicePipeline.retrieval_query_for(user_text, english_text)

        # Intent-first route: greeting/thanks/goodbye/policy-card paths
        # terminate the turn before any cache/Qdrant/LLM work.
        route = await NokvoOneVoicePipeline._route_turn(
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
            outbound_context=outbound_context,
        )
        intent_result: IntentResult = route["intent_result"]
        bundle: RuntimeBundle = turn_cache["bundle"]
        single_prompt_guidance = bundle.single_prompt_guidance
        if route["route"] in {"template", "answer_card", "policy_card"}:
            answer = route["answer"]
            await NokvoOneVoicePipeline._apply_route_state(tenant_res, call_id, route)
            await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
            total_ms = int((perf_counter() - started) * 1000)
            NokvoOneVoicePipeline._log_route(
                {
                    "tenant_id": tenant_res.tenant_id,
                    "call_id": call_id,
                    "text": user_text[:120],
                    "intent": intent_result.intent,
                    "topic": intent_result.topic,
                    "route": route["route"],
                    "sensitive": route.get("sensitive"),
                    "cache_hit": False,
                    "qdrant_called": False,
                    "llm_called": False,
                    "policy_card_id": route.get("policy_card_id"),
                    "decision_code": route.get("decision_code"),
                    "single_prompt_enabled": bool(single_prompt_guidance),
                    "detected_entities": route.get("detected_entities"),
                    "state_slot": route.get("state_slot"),
                    "route_reason": route.get("route_reason"),
                    "total_ms": total_ms,
                }
            )
            return {
                "query": query,
                "answer": answer,
                "refused": False,
                "citations": [],
                "chunks": [],
                "runtime": {
                    "graph": "nokvo_rag_pipeline",
                    "mode": route["route"],
                    "model": None,
                    "response_language": language,
                    "latency_ms": total_ms,
                },
                "retrieval": {"used": False, "cache_hit": False, "relevant_count": 0},
                "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
                "tool_calls": route.get("tool_calls") or [],
            }

        # RAG fallback path — only cache non-sensitive queries.
        cached = None
        if not intent_result.sensitive:
            cached = await AgentSessionStore.get_cached_answer(
                tenant_res,
                retrieval_query,
                language,
                campaign_id=campaign_id,
                call_context=call_id,
            )
        if cached and cached.get("answer"):
            answer = str(cached["answer"])
            await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
            total_ms = int((perf_counter() - started) * 1000)
            NokvoOneVoicePipeline._log_route(
                {
                    "tenant_id": tenant_res.tenant_id,
                    "call_id": call_id,
                    "intent": intent_result.intent,
                    "topic": intent_result.topic,
                    "route": "cache",
                    "sensitive": False,
                    "cache_hit": True,
                    "qdrant_called": False,
                    "llm_called": False,
                    "single_prompt_enabled": bool(single_prompt_guidance),
                    "total_ms": total_ms,
                }
            )
            return {
                "query": query,
                "answer": answer,
                "refused": False,
                "citations": cached.get("citations") or [],
                "chunks": cached.get("chunks") or [],
                "runtime": {
                    "graph": "nokvo_rag_pipeline",
                    "mode": "semantic_cache",
                    "model": settings.AZURE_OPENAI_AGENT_MODEL,
                    "response_language": language,
                    "latency_ms": total_ms,
                },
                "retrieval": {"used": False, "cache_hit": True, "relevant_count": len(cached.get("chunks") or [])},
                "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
            }

        # Reuse the probe retrieval done by _route_turn when it overrode
        # an out_of_scope decision — avoids a duplicate embed+Qdrant call
        # on the hot path. (answer_text path — chat/non-voice surface, so
        # code_switching defaults to False.)
        retrieval = await NokvoOneVoicePipeline._await_prefetched_retrieval(route)
        if not retrieval:
            retrieval = await NokvoOneVoicePipeline.retrieve(
                tenant_res,
                retrieval_query,
                db=db,
                top_k=top_k,
                campaign_id=campaign_id,
                intent_result=intent_result,
                english_text=english_text,
            )
        chunks = retrieval.get("chunks") or []
        citations = [
            {
                "document_id": chunk.get("document_id"),
                "document_name": chunk.get("document_name"),
                "chunk_id": chunk.get("chunk_id"),
                "score": chunk.get("score"),
            }
            for chunk in chunks
        ]
        if not chunks and not single_prompt_guidance:
            answer, refused = NokvoOneVoicePipeline._no_context_answer(
                user_text,
                intent=intent_result.intent,
                language=language,
                company_name=company_name,
            )
            await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
            return {
                "query": query,
                "answer": answer,
                "refused": refused,
                "citations": [],
                "chunks": [],
                "runtime": {
                    "graph": "nokvo_rag_pipeline",
                    "mode": "no_context_refusal" if refused else "conversation",
                    "model": settings.AZURE_OPENAI_AGENT_MODEL,
                    "response_language": language,
                    "latency_ms": int((perf_counter() - started) * 1000),
                },
                "retrieval": {
                    "used": True,
                    "cache_hit": False,
                    "retrieved_count": 0,
                    "relevant_count": 0,
                    "top_score": 0.0,
                    "relevance_threshold": settings.AGENT_MIN_RELEVANCE_SCORE,
                    "skipped_reason": retrieval.get("refusal"),
                },
                "intent": {"type": "RAG_ALWAYS_ON", "should_retrieve": True, "reason": "pre-indexed tenant retrieval"},
            }

        field_questions_prompt = NokvoOneVoicePipeline._field_questions_prompt_for_bundle(
            bundle, language=language
        )
        memory_block_v2 = ""
        if conversational_memory is not None:
            try:
                memory_block_v2 = conversational_memory.compose_prompt_block(language=language)
            except Exception:
                memory_block_v2 = ""
        messages = NokvoOneVoicePipeline._messages(
            user_text,
            chunks,
            language=language,
            history=history,
            company_name=company_name,
            campaign_goal=campaign_goal,
            single_prompt_guidance=single_prompt_guidance,
            outbound_context=outbound_context,
            outbound_memory=update_outbound_memory(
                dict((turn_cache.get("state") or {}).get("outbound_memory") or {}),
                caller_text=user_text,
            ) if outbound_context is not None else None,
            conversational_memory_block=memory_block_v2,
            field_questions_prompt=field_questions_prompt,
        )
        timeout = max(0.8, (latency_budget_ms or settings.AGENT_LLM_TIMEOUT_MS) / 1000)
        llm_error = None
        try:
            answer = await asyncio.wait_for(AzureGroundedLLM.complete(tenant_res, messages), timeout=timeout)
            answer = NokvoOneVoicePipeline._sanitize_answer(answer) or NokvoOneVoicePipeline._refusal(language)
            refused = NokvoOneVoicePipeline._is_refusal(answer, language)
        except Exception as exc:
            llm_error = str(exc)[:240]
            answer = NokvoOneVoicePipeline._refusal(language)
            refused = True
        await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
        # Sensitive intents (cancellation/refund/payment/account/food_quality)
        # bypass caching even when the chunk metadata looks fine — the answer
        # may be tied to a transient policy version or user-specific phrasing.
        cache_eligible = not intent_result.sensitive and not llm_error and NokvoOneVoicePipeline._cacheable(retrieval_query, answer, chunks)
        if cache_eligible:
            await AgentSessionStore.set_cached_answer(
                tenant_res,
                retrieval_query,
                language,
                {"answer": answer, "citations": citations, "chunks": chunks[:2]},
                campaign_id=campaign_id,
                call_context=call_id,
            )
        total_ms = int((perf_counter() - started) * 1000)
        NokvoOneVoicePipeline._log_route(
            {
                "tenant_id": tenant_res.tenant_id,
                "call_id": call_id,
                "intent": intent_result.intent,
                "topic": intent_result.topic,
                "route": ("single_prompt_rag" if not chunks else "qdrant_rag") if not refused else "refusal",
                "sensitive": intent_result.sensitive,
                "cache_hit": False,
                "qdrant_called": True,
                "llm_called": True,
                "single_prompt_enabled": bool(single_prompt_guidance),
                "total_ms": total_ms,
                "top_score": max((float(c.get("score") or 0.0) for c in chunks), default=0.0),
                "chunk_count": len(chunks),
            }
        )
        return {
            "query": query,
            "answer": answer,
            "refused": refused,
            "citations": citations,
            "chunks": chunks,
            "runtime": {
                "graph": "nokvo_rag_pipeline",
                "mode": "single_prompt_grounded" if not chunks else "grounded_rag",
                "model": settings.AZURE_OPENAI_AGENT_MODEL,
                "response_language": language,
                "latency_ms": total_ms,
                "llm_error": llm_error,
            },
            "retrieval": {
                "used": True,
                "cache_hit": False,
                "retrieved_count": len(chunks),
                "relevant_count": len(chunks),
                "top_score": max((float(chunk.get("score") or 0.0) for chunk in chunks), default=0.0),
                "relevance_threshold": retrieval.get("min_score") or settings.AGENT_MIN_RELEVANCE_SCORE,
            },
            "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": True, "sensitive": intent_result.sensitive},
        }

    @staticmethod
    def _field_questions_prompt_for_bundle(
        bundle: "RuntimeBundle",
        *,
        language: str,
    ) -> str:
        """Build the "use these exact phrasings" prompt block from the
        per-tenant runtime bundle. Empty string when no record-creation
        fields are configured — keeps the prompt lean for inbound calls
        that aren't collecting structured records."""
        try:
            catalog = build_tool_flow_questions(
                bundle.organization_industry,
                bundle.overrides,
                bundle.custom_tabs,
            )
        except Exception:
            return ""
        return format_field_questions_prompt(catalog, language=language)

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
        started = perf_counter()
        user_text = _normalize(query)
        language = SarvamVoiceService.normalize_language(response_language)

        turn_cache = await NokvoOneVoicePipeline._prime_turn_cache(db, tenant_res, call_id)
        history = list(turn_cache.get("history") or [])

        english_text = retrieval_text if retrieval_text and _normalize(retrieval_text) != user_text else None
        retrieval_query = NokvoOneVoicePipeline.retrieval_query_for(user_text, english_text)

        route = await NokvoOneVoicePipeline._route_turn(
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
        intent_result: IntentResult = route["intent_result"]
        bundle: RuntimeBundle = turn_cache["bundle"]
        single_prompt_guidance = bundle.single_prompt_guidance
        # Outbound is a different agent — *no* inbound short-circuits apply.
        # Template smalltalk ("Sure, go ahead." for a "Yes") is the worst
        # offender: it derails the outbound flow because the agent should be
        # advancing the pitch on every turn, not handing the floor back.
        # answer_card and policy_card are inbound tenant data and equally
        # wrong here. Run every utterance through the LLM with the outbound
        # system fragment + campaign brief so it can drive the call.
        _outbound_active = bool(outbound_context) and outbound_context.is_proactive
        _deterministic_routes = (
            {"template"}  # outbound templates only come from deterministic tool flows here
            if _outbound_active
            else {"template", "answer_card", "policy_card"}
        )
        prompt_outbound_memory = outbound_memory
        if _outbound_active and prompt_outbound_memory is None:
            state_for_memory = dict(turn_cache.get("state") or {})
            prompt_outbound_memory = update_outbound_memory(
                dict(state_for_memory.get("outbound_memory") or {}),
                caller_text=user_text,
            )
        if route["route"] in _deterministic_routes:
            answer = route["answer"]
            yield {"type": "sentence", "text": answer, "language": language, "cache_hit": False}
            await NokvoOneVoicePipeline._apply_route_state(tenant_res, call_id, route)
            await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
            # A deterministic route delivered an answer — reset the
            # clarification escalation counter if it had been bumped.
            await NokvoOneVoicePipeline._apply_clarification(
                tenant_res,
                call_id,
                turn_cache=turn_cache,
                user_text=user_text,
                route=route["route"],
                intent=intent_result.intent,
                refused=False,
                chunks=[],
                state_slot=route.get("state_slot"),
                language=language,
                original_answer=answer,
            )
            total_ms = int((perf_counter() - started) * 1000)
            NokvoOneVoicePipeline._log_route(
                {
                    "tenant_id": tenant_res.tenant_id,
                    "call_id": call_id,
                    "text": user_text[:120],
                    "intent": intent_result.intent,
                    "topic": intent_result.topic,
                    "route": route["route"],
                    "sensitive": route.get("sensitive"),
                    "cache_hit": False,
                    "qdrant_called": False,
                    "llm_called": False,
                    "policy_card_id": route.get("policy_card_id"),
                    "decision_code": route.get("decision_code"),
                    "single_prompt_enabled": bool(single_prompt_guidance),
                    "detected_entities": route.get("detected_entities"),
                    "state_slot": route.get("state_slot"),
                    "route_reason": route.get("route_reason"),
                    "total_ms": total_ms,
                }
            )
            yield {
                "type": "final",
                "answer": answer,
                "refused": False,
                "chunks": [],
                "citations": [],
                "runtime": {
                    "graph": "nokvo_rag_pipeline",
                    "mode": route["route"],
                    "latency_ms": total_ms,
                },
                "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
                "tool_calls": route.get("tool_calls") or [],
            }
            return

        # Smalltalk LLM mode: chat naturally, no RAG, no chunks, no grounding.
        # The smalltalk system prompt explicitly forbids inventing world or
        # company facts — so the LLM can say "yeah that's frustrating" but
        # not "the weather is sunny" or "our policy is X".
        if route["route"] == "smalltalk_llm":
            classified = route.get("classified") or {}
            sentiment = str(classified.get("sentiment") or "neutral")
            history = await NokvoOneVoicePipeline._turn_history(tenant_res, call_id, turn_cache)
            messages = NokvoOneVoicePipeline._messages_smalltalk(
                user_text,
                language=language,
                history=history,
                company_name=company_name,
                sentiment=sentiment,
                single_prompt_guidance=single_prompt_guidance,
            )
            answer_parts: list[str] = []
            try:
                async for chunk in AzureGroundedLLM.stream_prosody(
                    tenant_res,
                    messages,
                    max_tokens=120,
                    retry_attempts=settings.VOICE_LLM_STREAM_RETRY_ATTEMPTS,
                    max_retry_wait_s=settings.VOICE_LLM_STREAM_MAX_RETRY_WAIT_MS / 1000,
                ):
                    sentence = NokvoOneVoicePipeline._sanitize_answer(chunk.text)
                    if not sentence:
                        continue
                    answer_parts.append(sentence)
                    yield {"type": "sentence", "text": sentence, "language": language, "tone": chunk.tone}
            except NokvoOneAgentRateLimited as exc:
                logger.warning(f"NOKVO-LLM: smalltalk rate-limited: {exc}")
                fallback = NokvoOneVoicePipeline._rate_limited_reply(language)
                answer_parts = [fallback]
                yield {"type": "sentence", "text": fallback, "language": language, "tone": "warm"}
            except Exception as exc:
                # Smalltalk LLM failed — fall back to a friendly template so
                # the caller never gets dead air.
                fallback = {
                    "hi": "ठीक है, बताइए मैं कैसे मदद कर सकता हूँ?",
                    "ta": "சரி, எப்படி உதவ முடியும்?",
                    "te": "సరే, ఎలా సహాయం చేయగలను?",
                }.get(language, "Mm-hm. What can I help with?")
                answer_parts = [fallback]
                yield {"type": "sentence", "text": fallback, "language": language, "tone": "warm"}
            answer = " ".join(answer_parts).strip() or NokvoOneVoicePipeline._refusal(language)
            await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
            total_ms = int((perf_counter() - started) * 1000)
            NokvoOneVoicePipeline._log_route(
                {
                    "tenant_id": tenant_res.tenant_id,
                    "call_id": call_id,
                    "text": user_text[:120],
                    "intent": intent_result.intent,
                    "topic": intent_result.topic,
                    "route": "smalltalk_llm",
                    "sensitive": False,
                    "cache_hit": False,
                    "qdrant_called": False,
                    "llm_called": True,
                    "single_prompt_enabled": bool(single_prompt_guidance),
                    "total_ms": total_ms,
                    "classified": classified,
                }
            )
            yield {
                "type": "final",
                "answer": answer,
                "refused": False,
                "chunks": [],
                "citations": [],
                "runtime": {"graph": "nokvo_rag_pipeline", "mode": "smalltalk_llm", "latency_ms": total_ms},
                "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
            }
            return

        # Outbound is a *different* agent: it doesn't read the inbound KB,
        # and the tenant's inbound single-prompt guidance does not apply.
        # The campaign's own brief + persona fields are the entire source.
        # We synthesize chunks from the doc text so the existing prompt-
        # assembly path keeps working without a second LLM call site.
        outbound_mode = _outbound_active
        if outbound_mode:
            permission_reply = NokvoOneVoicePipeline._outbound_post_opener_permission_reply(
                user_text,
                language=language,
                history=history,
                outbound_context=outbound_context,
                covered_objectives=covered_objectives,
            )
            if permission_reply:
                yield {"type": "sentence", "text": permission_reply, "language": language, "tone": "question"}
                await AgentSessionStore.append_turn(tenant_res, call_id, user_text, permission_reply)
                total_ms = int((perf_counter() - started) * 1000)
                NokvoOneVoicePipeline._log_route(
                    {
                        "tenant_id": tenant_res.tenant_id,
                        "call_id": call_id,
                        "text": user_text[:120],
                        "intent": intent_result.intent,
                        "topic": intent_result.topic,
                        "route": "outbound_permission_discovery",
                        "sensitive": False,
                        "cache_hit": False,
                        "qdrant_called": False,
                        "llm_called": False,
                        "single_prompt_enabled": False,
                        "total_ms": total_ms,
                    }
                )
                yield {
                    "type": "final",
                    "answer": permission_reply,
                    "refused": False,
                    "chunks": [],
                    "citations": [],
                    "runtime": {
                        "graph": "nokvo_rag_pipeline",
                        "mode": "outbound_permission_discovery",
                        "latency_ms": total_ms,
                    },
                    "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": False},
                    "tool_calls": [],
                }
                return
            single_prompt_guidance = ""
            chunks = NokvoOneVoicePipeline._chunks_from_outbound_doc(outbound_context)
            citations = [
                {
                    "document_id": chunk.get("document_id"),
                    "document_name": chunk.get("document_name"),
                    "chunk_id": chunk.get("chunk_id"),
                    "score": chunk.get("score"),
                }
                for chunk in chunks
            ]
            retrieval = {"chunks": chunks, "refusal": None}
        else:
            cached = None
            if not intent_result.sensitive:
                cached = await AgentSessionStore.get_cached_answer(
                    tenant_res,
                    retrieval_query,
                    language,
                    campaign_id=campaign_id,
                    call_context=call_id,
                )
            if cached and cached.get("answer"):
                answer = str(cached["answer"])
                yield {"type": "sentence", "text": answer, "language": language, "cache_hit": True}
                await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
                yield {
                    "type": "final",
                    "answer": answer,
                    "refused": False,
                    "chunks": cached.get("chunks") or [],
                    "citations": cached.get("citations") or [],
                    "runtime": {"graph": "nokvo_rag_pipeline", "mode": "semantic_cache", "latency_ms": int((perf_counter() - started) * 1000)},
                }
                return

            # Reuse the probe retrieval done by _route_turn when it overrode
            # an out_of_scope decision — avoids a duplicate embed+Qdrant call
            # on the hot path.
            retrieval = await NokvoOneVoicePipeline._await_prefetched_retrieval(route)
            if not retrieval:
                retrieval = await NokvoOneVoicePipeline.retrieve(
                    tenant_res,
                    retrieval_query,
                    db=db,
                    top_k=top_k,
                    campaign_id=campaign_id,
                    intent_result=intent_result,
                    english_text=english_text,
                    dual_retrieval=code_switching,
                )
            chunks = retrieval.get("chunks") or []
            citations = [
                {
                    "document_id": chunk.get("document_id"),
                    "document_name": chunk.get("document_name"),
                    "chunk_id": chunk.get("chunk_id"),
                    "score": chunk.get("score"),
                }
                for chunk in chunks
            ]
        if not chunks and not single_prompt_guidance and not outbound_mode:
            answer, refused = NokvoOneVoicePipeline._no_context_answer(
                user_text,
                intent=intent_result.intent,
                language=language,
                company_name=company_name,
            )
            # Clarification FSM: escalate once the caller has produced
            # several consecutive low-information turns. After two the
            # agent offers concrete options; after three it hands off to
            # support instead of looping the same "sorry, missed that"
            # reply.
            answer, clarify_action, _ = await NokvoOneVoicePipeline._apply_clarification(
                tenant_res,
                call_id,
                turn_cache=turn_cache,
                user_text=user_text,
                route="no_context_refusal",
                intent=intent_result.intent,
                refused=refused,
                chunks=[],
                state_slot=None,
                language=language,
                original_answer=answer,
            )
            yield {"type": "sentence", "text": answer, "language": language}
            await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
            yield {
                "type": "final",
                "answer": answer,
                "refused": refused,
                "chunks": [],
                "citations": [],
                "runtime": {
                    "graph": "nokvo_rag_pipeline",
                    "mode": "no_context_refusal",
                    "clarification": clarify_action,
                    "latency_ms": int((perf_counter() - started) * 1000),
                },
            }
            return

        field_questions_prompt = NokvoOneVoicePipeline._field_questions_prompt_for_bundle(
            bundle, language=language
        )
        memory_block = ""
        if conversational_memory is not None:
            try:
                memory_block = conversational_memory.compose_prompt_block(language=language)
            except Exception:
                memory_block = ""
        messages = NokvoOneVoicePipeline._messages(
            user_text,
            chunks,
            language=language,
            history=history,
            company_name=company_name,
            campaign_goal=campaign_goal,
            single_prompt_guidance=single_prompt_guidance,
            outbound_context=outbound_context,
            covered_objectives=covered_objectives,
            outbound_memory=prompt_outbound_memory,
            conversational_memory_block=memory_block,
            field_questions_prompt=field_questions_prompt,
        )
        # Prosody-aware streaming: the LLM is asked to wrap each sentence in a
        # [tone]…[/tone] tag. The parser strips the tags and emits one chunk
        # per sentence-or-tone-boundary so we can synthesize each with
        # matching pace/pitch/loudness.
        answer_parts: list[str] = []
        rate_limited = False
        # Outbound: hard token cap so the model physically cannot generate a
        # paragraph reply. 48 tokens keeps it near the 1-2 sentence target.
        # Inbound keeps the default 180.
        _stream_max_tokens = 48 if outbound_mode else 180
        try:
            async for chunk in AzureGroundedLLM.stream_prosody(
                tenant_res,
                messages,
                max_tokens=_stream_max_tokens,
                retry_attempts=settings.VOICE_LLM_STREAM_RETRY_ATTEMPTS,
                max_retry_wait_s=settings.VOICE_LLM_STREAM_MAX_RETRY_WAIT_MS / 1000,
            ):
                sentence = NokvoOneVoicePipeline._sanitize_answer(chunk.text)
                if not sentence:
                    continue
                answer_parts.append(sentence)
                yield {"type": "sentence", "text": sentence, "language": language, "tone": chunk.tone}
        except NokvoOneAgentRateLimited as exc:
            # Azure deployment is throttled. Tell the caller specifically —
            # "I'm busy, try again" sounds far better than "I do not have
            # enough information", and it's the actual truth.
            logger.warning(f"NOKVO-LLM: stream rate-limited: {exc}")
            rate_limited = True
            fallback = NokvoOneVoicePipeline._rate_limited_reply(language)
            answer_parts = [fallback]
            yield {"type": "sentence", "text": fallback, "language": language, "tone": "warm"}

        if rate_limited:
            answer = answer_parts[0]
            refused = False
        else:
            answer = NokvoOneVoicePipeline._sanitize_answer(" ".join(answer_parts))
            # Outbound dead-air guard. Filler turns ("Mm-hm", "I would
            # say", "uh") routinely produce an empty / refusal completion
            # from the LLM. We can't leave the caller in silence — emit a
            # short, in-persona nudge instead so the conversation
            # continues. The inbound path keeps its existing refusal so
            # the clarification FSM can escalate after several vague turns.
            if not answer or NokvoOneVoicePipeline._is_refusal(answer, language):
                if outbound_mode:
                    fallback = "[warm]No rush — take your time.[/warm]"
                    answer = NokvoOneVoicePipeline._sanitize_answer(fallback)
                    yield {"type": "sentence", "text": answer, "language": language, "tone": "warm"}
                    refused = False
                else:
                    answer = NokvoOneVoicePipeline._refusal(language)
                    refused = True
            else:
                refused = NokvoOneVoicePipeline._is_refusal(answer, language)
        # Clarification FSM is inbound support behavior. Outbound calls use
        # the campaign prompt + memory to handle filler naturally; applying
        # the inbound vague-turn FSM here can make the agent talk over the
        # prospect with generic repair prompts.
        if outbound_mode:
            clarify_action = None
        else:
            # Clarification FSM after the grounded RAG turn: if the LLM
            # ended up refusing despite retrieval finding no chunks the
            # caller is effectively still vague — bump the counter so a
            # third such turn escalates instead of looping refusals.
            answer, clarify_action, _ = await NokvoOneVoicePipeline._apply_clarification(
                tenant_res,
                call_id,
                turn_cache=turn_cache,
                user_text=user_text,
                route=("qdrant_rag" if chunks else "single_prompt_rag"),
                intent=intent_result.intent,
                refused=refused,
                chunks=chunks,
                state_slot=None,
                language=language,
                original_answer=answer,
            )
        await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
        cache_eligible = (
            not outbound_mode
            and not intent_result.sensitive
            and NokvoOneVoicePipeline._cacheable(retrieval_query, answer, chunks)
        )
        if cache_eligible:
            await AgentSessionStore.set_cached_answer(
                tenant_res,
                retrieval_query,
                language,
                {"answer": answer, "citations": citations, "chunks": chunks[:2]},
                campaign_id=campaign_id,
                call_context=call_id,
            )
        total_ms = int((perf_counter() - started) * 1000)
        NokvoOneVoicePipeline._log_route(
            {
                "tenant_id": tenant_res.tenant_id,
                "call_id": call_id,
                "intent": intent_result.intent,
                "topic": intent_result.topic,
                "route": ("single_prompt_rag" if not chunks else "qdrant_rag") if not refused else "refusal",
                "sensitive": intent_result.sensitive,
                "cache_hit": False,
                "qdrant_called": True,
                "llm_called": True,
                "single_prompt_enabled": bool(single_prompt_guidance),
                "total_ms": total_ms,
                "top_score": max((float(c.get("score") or 0.0) for c in chunks), default=0.0),
                "chunk_count": len(chunks),
            }
        )
        yield {
            "type": "final",
            "answer": answer,
            "refused": refused,
            "chunks": chunks,
            "citations": citations,
            "runtime": {
                "graph": "nokvo_rag_pipeline",
                "mode": "single_prompt_grounded_streamed" if not chunks else "grounded_rag_streamed",
                "model": settings.AZURE_OPENAI_AGENT_MODEL,
                "response_language": language,
                "latency_ms": total_ms,
            },
            "retrieval": {
                "used": True,
                "cache_hit": False,
                "relevant_count": len(chunks),
                "top_score": max((float(chunk.get("score") or 0.0) for chunk in chunks), default=0.0),
            },
            "intent": {"type": intent_result.intent, "topic": intent_result.topic, "should_retrieve": True, "sensitive": intent_result.sensitive},
        }

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
                "policy_version": AgentKnowledgeService.policy_version(tenant_res),
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
