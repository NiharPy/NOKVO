from __future__ import annotations

import asyncio
import json
import re
import uuid
from time import perf_counter
from typing import Any, AsyncIterator
from urllib import parse as urllib_parse

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.agent_knowledge_service import (
    AGENT_CHUNK_SOURCE_KIND,
    AGENT_KNOWLEDGE_SOURCE_TYPE,
    AGENT_POLICY_CARDS_KEY,
    AgentKnowledgeService,
)
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
from app.services.prosody import (
    DEFAULT_TONE,
    ProsodyChunk,
    prosody_for,
    strip_tone_tags,
    stream_prosody_chunks,
)
from app.services.qdrant_service import QdrantService
from app.services.sarvam_voice_service import SARVAM_LANGUAGE_OPTIONS, SarvamVoiceService
from app.services.text_embedding_service import TextEmbeddingService


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
        response = await AzureGroundedLLM.http().post(
            url,
            headers={"api-key": api_key, "Content-Type": "application/json"},
            json=body,
        )
        if response.status_code == 429:
            retry_after_hdr = response.headers.get("retry-after", "")
            try:
                retry_after = float(retry_after_hdr) if retry_after_hdr else 0.0
            except ValueError:
                retry_after = 0.0
            raise NokvoOneAgentRateLimited(
                f"Azure OpenAI rate-limited (429): {response.text[:300]}",
                retry_after_seconds=retry_after or None,
            )
        if response.status_code >= 400:
            raise NokvoOneAgentRuntimeError(f"Azure OpenAI request failed ({response.status_code}): {response.text[:300]}")
        return AzureGroundedLLM.extract_text(response.json())

    @staticmethod
    async def stream_prosody(
        tenant_res: TenantResources,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 180,
    ) -> AsyncIterator[ProsodyChunk]:
        """Stream prosody-tagged sentence chunks.

        The LLM is instructed (via the system prompt) to emit inline tone
        tags like ``[empathy]…[/empathy]`` around each phrase. The parser
        strips the tags and yields ``(text, tone)`` chunks aligned to
        sentence boundaries so TTS can pick matching pace/pitch/loudness.
        """
        async for chunk in stream_prosody_chunks(
            AzureGroundedLLM.stream(tenant_res, messages, max_tokens=max_tokens)
        ):
            yield chunk

    @staticmethod
    async def stream(tenant_res: TenantResources, messages: list[dict[str, str]], *, max_tokens: int = 180) -> AsyncIterator[str]:
        api_key = await AzureGroundedLLM.api_key(tenant_res)
        url, body = AzureGroundedLLM.endpoint_and_body(
            tenant_res,
            messages,
            stream=True,
            max_tokens=max_tokens,
        )
        attempts = 2  # initial + 1 retry on 429
        for attempt in range(attempts):
            async with AzureGroundedLLM.http().stream(
                "POST",
                url,
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json=body,
            ) as response:
                if response.status_code == 429:
                    # Rate-limited. Honor Retry-After but cap so voice
                    # latency stays bounded — better to fall back to a
                    # graceful "busy" reply than to keep the caller waiting.
                    retry_after_hdr = response.headers.get("retry-after", "")
                    try:
                        retry_after = float(retry_after_hdr) if retry_after_hdr else 0.0
                    except ValueError:
                        retry_after = 0.0
                    body_text = (await response.aread()).decode("utf-8", errors="replace")[:300]
                    if attempt < attempts - 1 and retry_after and retry_after <= 1.5:
                        print(f"[NOKVO-LLM] 429 — retrying after {retry_after:.2f}s")
                        await asyncio.sleep(retry_after)
                        continue  # retry the outer for-loop
                    print(f"[NOKVO-LLM] 429 — giving up after {attempt + 1} attempt(s); retry_after={retry_after_hdr!r}")
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
    ) -> dict[str, Any]:
        if not query.strip():
            return {"query": query, "chunks": [], "refusal": "Empty query."}
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
    def _messages(
        query: str,
        chunks: list[dict[str, Any]],
        *,
        language: str,
        history: list[dict[str, str]],
        company_name: str | None = None,
        campaign_goal: str | None = None,
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
        campaign_rule = (
            f"Campaign goal: {campaign_goal}. Follow this goal, but still use only the supplied context."
            if campaign_goal
            else "This is an inbound support conversation unless campaign context says otherwise."
        )
        brand = company_name or "the tenant"

        # Order matters: language directive sits at the very top AND is
        # repeated at the bottom — LLMs weight start and end of long prompts
        # most heavily, and the reply language must dominate the conversation
        # history (which may be in English).
        language_directive_top = (
            f"# REPLY LANGUAGE — NON-NEGOTIABLE\n"
            f"Reply in {language_label}, using its native script. This overrides the conversation history, "
            f"the user's most recent message, and your training defaults. Do not mix languages. "
            f"Do not apologise for not knowing this language — you do know it. Reply in it.\n\n"
        )

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
            "1. Answer only with facts stated explicitly in the retrieved tenant context. "
            "If the user's question is partially covered, state EVERY relevant fact the context contains (city, name, hours, number, etc.) and ONLY then say the remaining detail must be confirmed by support. "
            "Refuse only when nothing in the context is relevant.\n"
            "1a. CRITICAL FOR NON-ENGLISH REPLIES: When responding in Telugu, Hindi, Tamil, Bengali, Marathi, Gujarati, Kannada, Malayalam, Punjabi, or Urdu — NEVER use 'I don't have that information' if the retrieved context contains ANY relevant fact. "
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
            f"# REMINDER\nReply in {language_label}. Do not switch languages."
        )

        messages: list[dict[str, str]] = [
            {"role": "system", "content": system_content},
        ]
        for turn in history[-8:]:
            role = turn.get("role") if turn.get("role") in {"user", "assistant"} else "user"
            messages.append({"role": role, "content": str(turn.get("content") or "")[:1200]})
        messages.append(
            {
                "role": "user",
                "content": (
                    f"Retrieved tenant context:\n{chr(10).join(context_parts)}\n\n"
                    f"Current user question:\n{query}\n\n"
                    f"Reply in {language_label}."
                ),
            }
        )
        return messages

    @staticmethod
    def _messages_smalltalk(
        query: str,
        *,
        language: str,
        history: list[dict[str, str]],
        company_name: str | None = None,
        sentiment: str = "neutral",
    ) -> list[dict[str, str]]:
        """Prompt for the LLM in *casual conversation* mode.

        The caller said something conversational, not a question requiring
        KB lookup. The LLM can respond warmly using its general conversational
        abilities, BUT it has no permission to make factual claims about the
        world or about the company — those still require KB-grounded context.
        """
        language_label = SarvamVoiceService.language_label(language)
        brand = company_name or "the company"

        sentiment_guidance = {
            "frustrated": "The caller sounds frustrated. ACKNOWLEDGE the feeling first in one short phrase before asking how to help.",
            "negative": "The caller sounds unhappy. Be warm and offer to help.",
            "positive": "The caller is in a positive mood. Match that warmth briefly.",
            "curious": "The caller is curious. Be friendly and clarify what they need.",
            "neutral": "Match the caller's energy. Keep it brief.",
        }.get(sentiment, "Keep it brief and friendly.")

        system_content = (
            f"# REPLY LANGUAGE — NON-NEGOTIABLE\n"
            f"Reply in {language_label}, using its native script. Do not switch languages.\n\n"
            f"You are Nokvo One's live voice agent for {brand}. The caller just said something CONVERSATIONAL — a greeting, thank-you, acknowledgment, casual remark, or expression of feeling. Not a factual question about the company.\n\n"
            "# RESPONSE STYLE\n"
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
    def _active_policy_cards(tenant_res: TenantResources) -> list[dict[str, Any]]:
        provider_status = dict(tenant_res.provider_status or {})
        return list(provider_status.get(AGENT_POLICY_CARDS_KEY) or [])

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
                         "decision_code", "ttfb_ms", "total_ms"}
            }
            # Surface the LLM classifier's intent so we can debug "why didn't
            # RAG fire" without enabling full debug mode.
            cls = route_payload.get("classified") or {}
            if isinstance(cls, dict) and cls:
                compact["llm_intent"] = cls.get("intent")
                compact["llm_needs_kb"] = cls.get("needs_kb")
                compact["llm_fallback"] = cls.get("fallback")
            print(f"[NOKVO-AGENT-ROUTE] {compact}")
            return
        print(f"[NOKVO-AGENT-ROUTE-DEBUG] {route_payload}")

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
    async def _await_prefetched_retrieval(route: dict[str, Any]) -> dict[str, Any] | None:
        retrieval = route.get("prefetched_retrieval") if isinstance(route, dict) else None
        if isinstance(retrieval, asyncio.Task):
            try:
                return await retrieval
            except asyncio.CancelledError:
                return None
            except Exception as exc:
                print(f"[NOKVO-RETRIEVE] prefetched retrieval failed: {exc!r}")
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
    ) -> dict[str, Any]:
        """Intent-first router. Returns a decision dict with one of:

        ``english_text`` is the Sarvam-translate-STT output of the same
        utterance. When the caller spoke Telugu / Hindi / etc., the native
        ``user_text`` won't match our English-language patterns (extractors,
        keyword regex) so we use ``english_text`` as the basis for those
        checks while still preserving the caller's exact words for the LLM
        and for language-switch detection.

        - ``route == 'template'``: local canned reply (greeting/thanks/goodbye/smalltalk).
        - ``route == 'answer_card'``: matched a Q/A answer card.
        - ``route == 'policy_card'``: matched a deterministic policy card.
        - ``route == 'rag'``: falls through to embeddings/Qdrant/LLM. ``intent_result`` is set so the retrieval layer can apply sensitive-topic settings.
        """
        intent_result = FastIntentRouter.classify(user_text, language=language)

        # 1) Greeting / thanks / goodbye / smalltalk — no LLM, no cache, no embeddings.
        templated = NokvoOneVoicePipeline._template_reply(intent_result.intent, language, company_name)
        if templated:
            return {
                "route": "template",
                "answer": templated,
                "intent_result": intent_result,
                "safe_to_cache": False,
                "sensitive": False,
            }

        # 2) Answer-card cache (existing Q/A card lookup).
        card = AgentKnowledgeService.find_answer_card(tenant_res, user_text, language)
        if card and card.get("answer"):
            return {
                "route": "answer_card",
                "answer": str(card["answer"]),
                "intent_result": intent_result,
                "safe_to_cache": bool(card.get("cacheable", True)) and not intent_result.sensitive,
                "sensitive": intent_result.sensitive,
                "card_id": card.get("id"),
            }

        # 3) Sensitive policy intents (cancellation/refund) → deterministic engine.
        if intent_result.intent in (INTENT_CANCELLATION_REQUEST, INTENT_REFUND_ELIGIBILITY):
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
                history = await AgentSessionStore.get_history(tenant_res, call_id)
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
            )
        )
        history = await AgentSessionStore.get_history(tenant_res, call_id)
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
    ) -> dict[str, Any]:
        started = perf_counter()
        user_text = _normalize(query)
        language = SarvamVoiceService.normalize_language(response_language)
        history = (conversation_history or []) + await AgentSessionStore.get_history(tenant_res, call_id)

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
        )
        intent_result: IntentResult = route["intent_result"]
        if route["route"] in {"template", "answer_card", "policy_card"}:
            answer = route["answer"]
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
            }

        # RAG fallback path — only cache non-sensitive queries.
        cached = None
        if not intent_result.sensitive:
            cached = await AgentSessionStore.get_cached_answer(
                tenant_res, retrieval_query, language, campaign_id=campaign_id
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
        if not chunks:
            if _SMALLTALK_RE.match(user_text):
                answer = NokvoOneVoicePipeline._smalltalk_reply(user_text, language, company_name)
                refused = False
            elif intent_result.intent == INTENT_UNKNOWN_GENERAL:
                # No retrieved context AND no specific intent — caller said
                # something we can't ground on (e.g. "can you hear me",
                # "uh I was wondering"). Ask an open question rather than
                # refusing formally.
                answer = NokvoOneVoicePipeline._open_question(language)
                refused = False
            else:
                answer = NokvoOneVoicePipeline._refusal(language)
                refused = True
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

        messages = NokvoOneVoicePipeline._messages(
            user_text,
            chunks,
            language=language,
            history=history,
            company_name=company_name,
            campaign_goal=campaign_goal,
        )
        timeout = max(0.8, (latency_budget_ms or settings.AGENT_LLM_TIMEOUT_MS) / 1000)
        llm_error = None
        try:
            answer = await asyncio.wait_for(AzureGroundedLLM.complete(tenant_res, messages), timeout=timeout)
            answer = NokvoOneVoicePipeline._sanitize_answer(answer) or NokvoOneVoicePipeline._refusal(language)
            refused = answer == NokvoOneVoicePipeline._refusal(language)
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
            )
        total_ms = int((perf_counter() - started) * 1000)
        NokvoOneVoicePipeline._log_route(
            {
                "tenant_id": tenant_res.tenant_id,
                "call_id": call_id,
                "intent": intent_result.intent,
                "topic": intent_result.topic,
                "route": "qdrant_rag" if not refused else "refusal",
                "sensitive": intent_result.sensitive,
                "cache_hit": False,
                "qdrant_called": True,
                "llm_called": True,
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
                "mode": "grounded_rag",
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
    ) -> AsyncIterator[dict[str, Any]]:
        started = perf_counter()
        user_text = _normalize(query)
        language = SarvamVoiceService.normalize_language(response_language)
        history = await AgentSessionStore.get_history(tenant_res, call_id)

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
        )
        intent_result: IntentResult = route["intent_result"]
        if route["route"] in {"template", "answer_card", "policy_card"}:
            answer = route["answer"]
            yield {"type": "sentence", "text": answer, "language": language, "cache_hit": False}
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
            }
            return

        # Smalltalk LLM mode: chat naturally, no RAG, no chunks, no grounding.
        # The smalltalk system prompt explicitly forbids inventing world or
        # company facts — so the LLM can say "yeah that's frustrating" but
        # not "the weather is sunny" or "our policy is X".
        if route["route"] == "smalltalk_llm":
            classified = route.get("classified") or {}
            sentiment = str(classified.get("sentiment") or "neutral")
            history = await AgentSessionStore.get_history(tenant_res, call_id)
            messages = NokvoOneVoicePipeline._messages_smalltalk(
                user_text,
                language=language,
                history=history,
                company_name=company_name,
                sentiment=sentiment,
            )
            answer_parts: list[str] = []
            try:
                async for chunk in AzureGroundedLLM.stream_prosody(tenant_res, messages, max_tokens=120):
                    sentence = NokvoOneVoicePipeline._sanitize_answer(chunk.text)
                    if not sentence:
                        continue
                    answer_parts.append(sentence)
                    yield {"type": "sentence", "text": sentence, "language": language, "tone": chunk.tone}
            except NokvoOneAgentRateLimited as exc:
                print(f"[NOKVO-LLM] smalltalk rate-limited: {exc}")
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

        cached = None
        if not intent_result.sensitive:
            cached = await AgentSessionStore.get_cached_answer(
                tenant_res, retrieval_query, language, campaign_id=campaign_id
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
        if not chunks:
            if _SMALLTALK_RE.match(user_text):
                answer = NokvoOneVoicePipeline._smalltalk_reply(user_text, language, company_name)
                refused = False
            elif intent_result.intent == INTENT_UNKNOWN_GENERAL:
                # No retrieved context AND no specific intent — caller said
                # something we can't ground on. Ask an open question rather
                # than dumping the formal refusal.
                answer = NokvoOneVoicePipeline._open_question(language)
                refused = False
            else:
                answer = NokvoOneVoicePipeline._refusal(language)
                refused = True
            yield {"type": "sentence", "text": answer, "language": language}
            await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
            yield {
                "type": "final",
                "answer": answer,
                "refused": refused,
                "chunks": [],
                "citations": [],
                "runtime": {"graph": "nokvo_rag_pipeline", "mode": "no_context_refusal", "latency_ms": int((perf_counter() - started) * 1000)},
            }
            return

        messages = NokvoOneVoicePipeline._messages(
            user_text,
            chunks,
            language=language,
            history=history,
            company_name=company_name,
            campaign_goal=campaign_goal,
        )
        # Prosody-aware streaming: the LLM is asked to wrap each sentence in a
        # [tone]…[/tone] tag. The parser strips the tags and emits one chunk
        # per sentence-or-tone-boundary so we can synthesize each with
        # matching pace/pitch/loudness.
        answer_parts: list[str] = []
        rate_limited = False
        try:
            async for chunk in AzureGroundedLLM.stream_prosody(tenant_res, messages):
                sentence = NokvoOneVoicePipeline._sanitize_answer(chunk.text)
                if not sentence:
                    continue
                answer_parts.append(sentence)
                yield {"type": "sentence", "text": sentence, "language": language, "tone": chunk.tone}
        except NokvoOneAgentRateLimited as exc:
            # Azure deployment is throttled. Tell the caller specifically —
            # "I'm busy, try again" sounds far better than "I do not have
            # enough information", and it's the actual truth.
            print(f"[NOKVO-LLM] stream rate-limited: {exc}")
            rate_limited = True
            fallback = NokvoOneVoicePipeline._rate_limited_reply(language)
            answer_parts = [fallback]
            yield {"type": "sentence", "text": fallback, "language": language, "tone": "warm"}

        if rate_limited:
            answer = answer_parts[0]
            refused = False
        else:
            answer = NokvoOneVoicePipeline._sanitize_answer(" ".join(answer_parts)) or NokvoOneVoicePipeline._refusal(language)
            refused = answer == NokvoOneVoicePipeline._refusal(language)
        await AgentSessionStore.append_turn(tenant_res, call_id, user_text, answer)
        cache_eligible = not intent_result.sensitive and NokvoOneVoicePipeline._cacheable(retrieval_query, answer, chunks)
        if cache_eligible:
            await AgentSessionStore.set_cached_answer(
                tenant_res,
                retrieval_query,
                language,
                {"answer": answer, "citations": citations, "chunks": chunks[:2]},
                campaign_id=campaign_id,
            )
        total_ms = int((perf_counter() - started) * 1000)
        NokvoOneVoicePipeline._log_route(
            {
                "tenant_id": tenant_res.tenant_id,
                "call_id": call_id,
                "intent": intent_result.intent,
                "topic": intent_result.topic,
                "route": "qdrant_rag" if not refused else "refusal",
                "sensitive": intent_result.sensitive,
                "cache_hit": False,
                "qdrant_called": True,
                "llm_called": True,
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
                "mode": "grounded_rag_streamed",
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
        return {
            "runtime": "nokvo_one_voice_agent",
            "graph": "nokvo_rag_pipeline",
            "knowledge_scope": "pre_indexed_tenant_qdrant_collection",
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
