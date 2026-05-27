"""Agent Runtime Service — Dual-Brain LangGraph Architecture.

Production rules:
    1. Conversation Brain — handles greetings, small talk, empathy, clarification,
       identity, and normal human flow WITHOUT RAG. Must not invent ZapEats policy facts.
    2. Knowledge Brain — uses uploaded documents through RAG ONLY for ZapEats-specific
       factual/policy/process answers. Strictly grounded to retrieved documents.

Never retrieve just because the user sent a message.
Retrieve only when intent gating says the user needs approved knowledge.
"""

from __future__ import annotations

import asyncio
import json
import random
import re
from time import perf_counter
from typing import Any, AsyncIterator, TypedDict
from urllib import parse as urllib_parse

import httpx

from langgraph.graph import END, START, StateGraph
from sqlalchemy.ext.asyncio import AsyncSession
from websockets.asyncio.client import connect

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.agent_knowledge_service import AgentKnowledgeService
from app.services.agent_intent_service import (
    INTENT_GREETING,
    INTENT_SUPPORT,
    INTENT_ORDER_ACTION,
    INTENT_AMBIGUOUS,
    classify_intent,
    detect_language,
    generate_conversation_reply,
    normalize_message,
    validate_chunk_relevance,
)
from app.services.azure_keyvault_service import AzureKeyVaultService


_FILLER_PHRASES: dict[str, str] = {
    "en": "Sure, one moment.",
    "hi": "जी, एक क्षण रुकिए।",
    "ta": "சரி, ஒரு நிமிடம்.",
    "te": "సరే, ఒక్క నిమిషం.",
    "kn": "ಸರಿ, ಒಂದು ಕ್ಷಣ.",
    "ml": "ശരി, ഒരു നിമിഷം.",
    "bn": "ঠিক আছে, একটু অপেক্ষা করুন।",
    "gu": "ઠીક છે, એક ક્ષણ.",
    "mr": "ठीक आहे, एक क्षण.",
    "pa": "ਠੀਕ ਹੈ, ਇੱਕ ਪਲ.",
    "ur": "ٹھیک ہے، ایک لمحہ.",
}

_HOW_CAN_I_HELP: dict[str, str] = {
    "en": "How can I help you today?",
    "hi": "मैं आज आपकी कैसे मदद कर सकता हूँ?",
    "ta": "இன்று நான் உங்களுக்கு எப்படி உதவ முடியும்?",
    "te": "నేడు నేను మీకు ఎలా సహాయం చేయగలను?",
    "kn": "ಇಂದು ನಾನು ನಿಮಗೆ ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಹುದು?",
    "ml": "ഇന്ന് ഞാൻ നിങ്ങൾക്ക് എങ്ങനെ സഹായിക്കാം?",
    "bn": "আজ আমি আপনাকে কীভাবে সাহায্য করতে পারি?",
    "gu": "આज હું તમારી કઈ રીતે મદદ કરી શકું?",
    "mr": "आज मी तुमच्यासाठी कसे मदत करू शकतो?",
    "pa": "ਅੱਜ ਮੈਂ ਤੁਹਾਡੀ ਕਿਵੇਂ ਮਦਦ ਕਰ ਸਕਦਾ ਹਾਂ?",
    "ur": "آج میں آپ کی کیسے مدد کر سکتا ہوں؟",
}

_REFUSAL_MSGS: dict[str, str] = {
    "en": "I don't have enough information to answer that. Could you share more detail, or I can escalate this for you.",
    "hi": "मेरे पास इस सवाल का सटीक जवाब देने के लिए पर्याप्त जानकारी नहीं है। कृपया अधिक विवरण दें, या मैं इसे आगे भेज सकता हूँ।",
    "ta": "இதற்கு பதில் சொல்ல போதுமான தகவல் என்னிடம் இல்லை। மேலும் விவரங்கள் தர முடியுமா, அல்லது நான் முன்னேற்று செய்கிறேன்.",
    "te": "ఈ ప్రశ్నకు సమాధానం ఇవ్వడానికి నా వద్ద తగిన సమాచారం లేదు। మరింత వివరాలు ఇవ్వగలరా, లేదా నేను ముందుకు పంపగలను.",
    "kn": "ಈ ಪ್ರಶ್ನೆಗೆ ಉತ್ತರಿಸಲು ನನ್ನ ಬಳಿ ಸಾಕಷ್ಟು ಮಾಹಿತಿ ಇಲ್ಲ. ಹೆಚ್ಚಿನ ವಿವರ ಹೇಳಬಹುದೇ, ಅಥವಾ ನಾನು ಮೇಲಕ್ಕೆ ಕಳುಹಿಸಬಲ್ಲೆ.",
    "ml": "ഈ ചോദ്യത്തിന് ഉത്തരം നൽകാൻ ആവശ്യമായ വിവരങ്ങൾ എന്റെ കൈവശം ഇല്ല. കൂടുതൽ വിശദാംശങ്ങൾ പറയാമോ, അല്ലെങ്കിൽ ഞാൻ മുകളിലേക്ക് കൈമാറാം.",
    "bn": "এই প্রশ্নের উত্তর দিতে আমার কাছে পর্যাপ্ত তথ্য নেই। আরও বিবরণ দিতে পারবেন, অথবা আমি এটি এগিয়ে পাঠাতে পারি।",
    "gu": "આ પ્રશ્નનો જવાબ આપવા માટે મારી પાસે પૂરતી માહિતી નથી. વધુ વિગત આપી શકો, અથવા હું આ આગળ મોકલી શકું.",
    "mr": "या प्रश्नाचे उत्तर देण्यासाठी माझ्याकडे पुरेशी माहिती नाही. अधिक तपशील देऊ शकता, किंवा मी हे पुढे पाठवतो.",
    "pa": "ਇਸ ਸਵਾਲ ਦਾ ਜਵਾਬ ਦੇਣ ਲਈ ਮੇਰੇ ਕੋਲ ਕਾਫ਼ੀ ਜਾਣਕਾਰੀ ਨਹੀਂ ਹੈ। ਕੀ ਤੁਸੀਂ ਹੋਰ ਵੇਰਵਾ ਦੇ ਸਕਦੇ ਹੋ, ਜਾਂ ਮੈਂ ਇਹ ਅੱਗੇ ਭੇਜ ਸਕਦਾ ਹਾਂ।",
    "ur": "اس سوال کا جواب دینے کے لیے میرے پاس کافی معلومات نہیں ہیں۔ کیا آپ مزید تفصیل دے سکتے ہیں، یا میں یہ آگے بھیج سکتا ہوں۔",
}

_PROBLEM_FALLBACK: dict[str, str] = {
    "en": "I'm sorry about that. Let me look into this — could you share your order number?",
    "hi": "मुझे खेद है। मैं इसे देख रहा हूँ — क्या आप अपना ऑर्डर नंबर बता सकते हैं?",
    "ta": "மன்னிக்கவும். நான் பார்க்கிறேன் — உங்கள் ஆர்டர் எண்ணை சொல்ல முடியுமா?",
    "te": "క్షమించండి. నేను చూస్తాను — మీ ఆర్డర్ నంబర్ చెప్పగలరా?",
    "kn": "ಕ್ಷಮಿಸಿ. ನಾನು ನೋಡುತ್ತೇನೆ — ನಿಮ್ಮ ಆರ್ಡರ್ ನಂಬರ್ ಹೇಳಬಹುದೇ?",
    "ml": "ക്ഷമിക്കണം. ഞാൻ നോക്കുന്നു — നിങ്ങളുടെ ഓർഡർ നമ്പർ പറയാമോ?",
    "bn": "দুঃখিত। আমি দেখছি — আপনার অর্ডার নম্বর দিতে পারবেন?",
    "gu": "માફ કરો. હું જોઉં છું — શું તમે તમારો ઑર્ડર નંબર આપી શકો?",
    "mr": "माफ करा. मी पाहतो — तुमचा ऑर्डर नंबर सांगू शकता का?",
    "pa": "ਮਾਫ਼ ਕਰਨਾ। ਮੈਂ ਦੇਖਦਾ ਹਾਂ — ਕੀ ਤੁਸੀਂ ਆਪਣਾ ਆਰਡਰ ਨੰਬਰ ਦੱਸ ਸਕਦੇ ਹੋ?",
    "ur": "معاف کریں۔ میں دیکھتا ہوں — کیا آپ اپنا آرڈر نمبر بتا سکتے ہیں؟",
}

_NEUTRAL_FALLBACK: dict[str, str] = {
    "en": "Let me check on that for you. Could you share your order number so I can help?",
    "hi": "मैं आपके लिए देख रहा हूँ। क्या आप अपना ऑर्डर नंबर बता सकते हैं?",
    "ta": "நான் பார்க்கிறேன். உங்கள் ஆர்டர் எண்ணை சொல்ல முடியுமா?",
    "te": "నేను చూస్తాను. మీ ఆర్డర్ నంబర్ చెప్పగలరా?",
    "kn": "ನಾನು ನೋಡುತ್ತೇನೆ. ನಿಮ್ಮ ಆರ್ಡರ್ ನಂಬರ್ ಹೇಳಬಹುದೇ?",
    "ml": "ഞാൻ നോക്കുന്നു. നിങ്ങളുടെ ഓർഡർ നമ്പർ പറയാമോ?",
    "bn": "আমি দেখছি। আপনার অর্ডার নম্বর দিতে পারবেন?",
    "gu": "હું જોઉં છું. શું તમે તમારો ઑર્ડર નંબર આપી શકો?",
    "mr": "मी पाहतो. तुमचा ऑर्डर नंबर सांगू शकता का?",
    "pa": "ਮੈਂ ਦੇਖਦਾ ਹਾਂ. ਕੀ ਤੁਸੀਂ ਆਪਣਾ ਆਰਡਰ ਨੰਬਰ ਦੱਸ ਸਕਦੇ ਹੋ?",
    "ur": "میں دیکھتا ہوں۔ کیا آپ اپنا آرڈر نمبر بتا سکتے ہیں؟",
}
SONIOX_INDIAN_LANGUAGE_OPTIONS = [
    {"code": "en", "label": "English", "native_label": "English"},
    {"code": "hi", "label": "Hindi", "native_label": "हिन्दी"},
    {"code": "bn", "label": "Bengali", "native_label": "বাংলা"},
    {"code": "gu", "label": "Gujarati", "native_label": "ગુજરાતી"},
    {"code": "kn", "label": "Kannada", "native_label": "ಕನ್ನಡ"},
    {"code": "ml", "label": "Malayalam", "native_label": "മലയാളം"},
    {"code": "mr", "label": "Marathi", "native_label": "मराठी"},
    {"code": "pa", "label": "Punjabi", "native_label": "ਪੰਜਾਬੀ"},
    {"code": "ta", "label": "Tamil", "native_label": "தமிழ்"},
    {"code": "te", "label": "Telugu", "native_label": "తెలుగు"},
    {"code": "ur", "label": "Urdu", "native_label": "اُردُو"},
]
SONIOX_INDIAN_LANGUAGE_CODES = {item["code"] for item in SONIOX_INDIAN_LANGUAGE_OPTIONS}


# ---------------------------------------------------------------------------
# LangGraph State
# ---------------------------------------------------------------------------

class AgentGraphState(TypedDict, total=False):
    query: str
    tenant_res: TenantResources
    db: AsyncSession | None
    top_k: int
    chunks: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    answer: str
    refused: bool
    latency_budget_ms: int
    runtime_mode: str
    error: str | None
    response_language: str
    # Conversation history — maintains continuity across turns
    conversation_history: list[dict[str, str]]
    # Intent gate fields
    intent_type: str
    intent_confidence: float
    intent_reason: str
    intent_classifier: str
    should_retrieve: bool
    retrieval_skipped_reason: str | None
    relevant_chunks: list[dict[str, Any]]
    retrieved_count: int
    top_retrieval_score: float
    retrieval_refusal: str | None


# ---------------------------------------------------------------------------
# Azure OpenAI LLM Client (Knowledge Brain)
# ---------------------------------------------------------------------------

class AzureAgentLLMClient:
    # Voice-agent hard cap: never wait longer than this on a 429 — dead air on a call is worse
    # than a fallback answer. Exponential backoff still applies within this ceiling.
    _MAX_RETRY_WAIT = 1.5
    _RETRY_BASE = 0.25  # seconds — first backoff before jitter
    _client: httpx.AsyncClient | None = None

    @classmethod
    def http_client(cls) -> httpx.AsyncClient:
        if cls._client is None or cls._client.is_closed:
            cls._client = httpx.AsyncClient(
                timeout=httpx.Timeout(8.0),
                limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            )
        return cls._client

    @staticmethod
    def _retry_wait(headers: dict, attempt: int = 0) -> float:
        """Exponential backoff with full jitter, floored by Retry-After if present.

        Formula: min(cap, max(retry_after, base * 2^attempt)) + uniform(0, 0.1) jitter
        Keeps waits voice-friendly (≤1.5s) while avoiding thundering-herd on shared quota.
        """
        raw = headers.get("Retry-After") or headers.get("retry-after") or "0"
        try:
            retry_after = float(raw)
        except ValueError:
            retry_after = 0.0
        backoff = AzureAgentLLMClient._RETRY_BASE * (2 ** attempt)
        wait = max(retry_after, backoff) + random.uniform(0, 0.1)
        return min(wait, AzureAgentLLMClient._MAX_RETRY_WAIT)

    @staticmethod
    async def _api_key(tenant_res: TenantResources) -> str:
        provider_status = dict(tenant_res.provider_status or {})
        key_ref = provider_status.get("llm_api_key_ref")
        if key_ref:
            secret = await AzureKeyVaultService.get_secret_value(key_ref)
            if secret:
                return secret
        if settings.AZURE_OPENAI_GLOBAL_API_KEY:
            return settings.AZURE_OPENAI_GLOBAL_API_KEY
        raise RuntimeError("Azure OpenAI API key is not configured for Agent runtime.")

    @staticmethod
    def _endpoint_and_body(tenant_res: TenantResources, messages: list[dict[str, str]]) -> tuple[str, dict[str, Any]]:
        provider_status = dict(tenant_res.provider_status or {})
        endpoint = str(provider_status.get("llm_endpoint") or settings.AZURE_OPENAI_GLOBAL_ENDPOINT or "").rstrip("/")
        if not endpoint:
            raise RuntimeError("Azure OpenAI endpoint is not configured for Agent runtime.")

        deployment = str(
            provider_status.get("llm_deployment")
            or provider_status.get("deployment_name")
            or settings.AZURE_OPENAI_AGENT_DEPLOYMENT
            or "gpt-4-1-mini"
        ).strip()
        if endpoint.endswith("/responses"):
            return endpoint, {
                "model": settings.AZURE_OPENAI_AGENT_MODEL or deployment,
                "input": messages,
                "temperature": 0.3,
                "max_output_tokens": 120,
            }

        api_version = urllib_parse.quote(settings.AZURE_OPENAI_AGENT_API_VERSION.strip())
        deployment_path = urllib_parse.quote(deployment)
        if "/openai/deployments/" in endpoint:
            url = f"{endpoint}?api-version={api_version}" if "api-version=" not in endpoint else endpoint
        else:
            url = f"{endpoint}/openai/deployments/{deployment_path}/chat/completions?api-version={api_version}"
        return url, {
            "messages": messages,
            "temperature": 0.3,
            "max_tokens": 120,
        }

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        if isinstance(payload.get("output_text"), str):
            return payload["output_text"].strip()
        choices = payload.get("choices") or []
        if choices:
            content = ((choices[0] or {}).get("message") or {}).get("content")
            if isinstance(content, str):
                return content.strip()
        output_parts: list[str] = []
        for item in payload.get("output") or []:
            for content in item.get("content") or []:
                text = content.get("text")
                if isinstance(text, str):
                    output_parts.append(text)
        return "\n".join(output_parts).strip()

    @staticmethod
    async def generate_grounded_answer(
        tenant_res: TenantResources,
        query: str,
        chunks: list[dict[str, Any]],
        *,
        response_language: str = "en",
        conversation_history: list[dict[str, str]] | None = None,
    ) -> str:
        """Knowledge Brain: Generate answer strictly grounded to retrieved documents."""
        context_parts = []
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
        if not context_parts:
            return AgentRuntimeService.agent_refusal(response_language)

        language_label = AgentRuntimeService.language_label(response_language)
        messages = [
            {
                "role": "system",
                "content": (
                    f"You are a calm Indian customer support agent on a live phone call. Reply in {language_label}, primarily using its native script. "
                    "Natural code-switching is REQUIRED — keep common English loanwords (order, refund, appointment, payment, status, OK, sorry, address, SMS, link) and all numbers / ₹ amounts / dates / times in English / digits, exactly as a real Indian call-center rep speaks. "
                    "Do NOT produce a literary, news-anchor, or Sanskritised register — speak like a real phone-support agent. "
                    "Use only the provided knowledge. Never invent policy, refund, payment, delivery, or account facts. "
                    "Sound natural, not like a document summary. Do not copy script labels, headings, chunk text, or bullet fragments. "
                    "First sentence must be short and useful, under 12 words. Keep the whole reply to 1-3 voice-friendly sentences. "
                    "If the knowledge does not answer the question, say you do not have enough approved information and offer escalation."
                ),
            },
        ]
        # Inject conversation history for continuity (last 10 turns)
        history = (conversation_history or [])[-10:]
        for turn in history:
            messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})

        messages.append({
            "role": "user",
            "content": (
                f"Customer: {query}\n\n"
                "Knowledge:\n"
                + "\n".join(context_parts)
                + "\n\nWhat do you say?"
            ),
        })
        api_key = await AzureAgentLLMClient._api_key(tenant_res)
        url, body = AzureAgentLLMClient._endpoint_and_body(tenant_res, messages)

        headers = {"Content-Type": "application/json", "api-key": api_key}
        client = AzureAgentLLMClient.http_client()
        for attempt in range(2):
            resp = await client.post(url, json=body, headers=headers)
            if resp.status_code == 429 and attempt == 0:
                await asyncio.sleep(AzureAgentLLMClient._retry_wait(dict(resp.headers), attempt))
                continue
            if resp.status_code >= 400:
                raise RuntimeError(f"Azure OpenAI Agent request failed ({resp.status_code}): {resp.text[:500]}")
            return AzureAgentLLMClient._extract_text(resp.json())
        raise RuntimeError("Azure OpenAI Agent request failed: rate limited after retry")

    @staticmethod
    async def stream_grounded_answer(
        tenant_res: TenantResources,
        query: str,
        chunks: list[dict[str, Any]],
        *,
        response_language: str = "en",
        conversation_history: list[dict[str, str]] | None = None,
    ) -> AsyncIterator[str]:
        """Knowledge Brain: Stream answer tokens via httpx for sentence-gated TTS dispatch."""
        context_parts = []
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
        if not context_parts:
            return

        language_label = AgentRuntimeService.language_label(response_language)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"You are a calm Indian customer support agent on a live phone call. Reply in {language_label}, primarily using its native script. "
                    "Natural code-switching is REQUIRED — keep common English loanwords (order, refund, appointment, payment, status, OK, sorry, address, SMS, link) and all numbers / ₹ amounts / dates / times in English / digits, exactly as a real Indian call-center rep speaks. "
                    "Do NOT produce a literary, news-anchor, or Sanskritised register — speak like a real phone-support agent. "
                    "Use only the provided knowledge. Never invent policy, refund, payment, delivery, or account facts. "
                    "Sound natural, not like a document summary. Do not copy script labels, headings, chunk text, or bullet fragments. "
                    "First sentence must be short and useful, under 12 words. Keep the whole reply to 1-3 voice-friendly sentences. "
                    "If the knowledge does not answer the question, say you do not have enough approved information and offer escalation."
                ),
            },
        ]
        for turn in (conversation_history or [])[-10:]:
            messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
        messages.append({
            "role": "user",
            "content": (
                f"Customer: {query}\n\n"
                "Knowledge:\n"
                + "\n".join(context_parts)
                + "\n\nWhat do you say?"
            ),
        })

        api_key = await AzureAgentLLMClient._api_key(tenant_res)
        url, body = AzureAgentLLMClient._endpoint_and_body(tenant_res, messages)
        body["stream"] = True
        headers = {"Content-Type": "application/json", "api-key": api_key}

        client = AzureAgentLLMClient.http_client()
        for attempt in range(2):
            async with client.stream("POST", url, json=body, headers=headers) as resp:
                if resp.status_code == 429 and attempt == 0:
                    wait = AzureAgentLLMClient._retry_wait(dict(resp.headers), attempt)
                    await asyncio.sleep(wait)
                    continue
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() == "[DONE]":
                        return
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    # Responses API format
                    event_type = event.get("type", "")
                    if event_type == "response.output_text.delta":
                        token = event.get("delta") or ""
                        if token:
                            yield token
                        continue
                    if event_type in ("response.done", "response.completed", "response.output_text.done"):
                        return
                    # Chat completions format
                    choices = event.get("choices") or []
                    if choices:
                        token = ((choices[0].get("delta") or {}).get("content")) or ""
                        if token:
                            yield token
                return


    @staticmethod
    async def generate_conversation_answer(
        tenant_res: TenantResources,
        query: str,
        intent_type: str,
        *,
        response_language: str = "en",
        conversation_history: list[dict[str, str]] | None = None,
    ) -> str:
        """Conversation Brain: LLM reply for greetings, smalltalk, clarification (no RAG)."""
        language_label = AgentRuntimeService.language_label(response_language)
        messages: list[dict[str, str]] = [
            {
                "role": "system",
                "content": (
                    f"You are a friendly customer support agent for a food delivery service on a live phone call. "
                    f"Reply in {language_label}, primarily in its native script. "
                    "Natural code-switching with English loanwords (order, refund, OK, address, SMS) is REQUIRED — talk like a real Indian call-center rep, not like a news reader. "
                    "Keep your reply to 1–2 short sentences. Do not invent policy facts. Be warm and natural."
                ),
            },
        ]
        for turn in (conversation_history or [])[-6:]:
            messages.append({"role": turn.get("role", "user"), "content": turn.get("content", "")})
        messages.append({"role": "user", "content": query})

        api_key = await AzureAgentLLMClient._api_key(tenant_res)
        url, body = AzureAgentLLMClient._endpoint_and_body(tenant_res, messages)
        # Override token limit — conversation replies are short
        if "max_output_tokens" in body:
            body["max_output_tokens"] = 80
        else:
            body["max_tokens"] = 80

        try:
            client = AzureAgentLLMClient.http_client()
            hdrs = {"Content-Type": "application/json", "api-key": api_key}
            for attempt in range(2):
                resp = await client.post(url, json=body, headers=hdrs)
                if resp.status_code == 429 and attempt == 0:
                    await asyncio.sleep(AzureAgentLLMClient._retry_wait(dict(resp.headers), attempt))
                    continue
                resp.raise_for_status()
                return AzureAgentLLMClient._extract_text(resp.json()) or ""
        except Exception:
            return ""


# ---------------------------------------------------------------------------
# LangGraph Node Functions
# ---------------------------------------------------------------------------

class AgentRuntimeService:
    _graph = None

    _PROBLEM_TERMS_RE = re.compile(
        # English
        r"\b(wrong|missing|cold|stale|late|damaged|refund|sick|not\s+delivered)\b"
        # Romanized Hindi / Hinglish
        r"|\b(galat|nahi\s+aaya|der\s+ho\s+gayi|thanda|kharab|wapas|vapas|nuksan)\b"
        # Hindi (Devanagari)
        r"|गलत|लापता|ठंडा|बासी|देर|नुकसान|रिफंड|खराब|नहीं\s+आया"
        # Tamil
        r"|தவறு|குளிர்|கெட்டது|தாமதம்|ரீஃபண்ட்"
        # Telugu
        r"|తప్పు|చల్లగా|పాడైన|ఆలస్యం|రీఫండ్"
        # Kannada
        r"|ತಪ್ಪು|ತಣ್ಣಗೆ|ಹಾಳಾದ|ತಡ|ರೀಫಂಡ್"
        # Malayalam
        r"|തെറ്റ്|തണുത്ത|കേടായ|വൈകി|റീഫണ്ട്"
        # Bengali
        r"|ভুল|ঠান্ডা|নষ্ট|দেরি|রিফান্ড"
        # Gujarati
        r"|ખોટું|ઠંડું|બગડેલ|મોડું|રીફંડ"
        # Marathi
        r"|चुकीचे|थंड|खराब|उशीर|रिफंड"
        # Punjabi
        r"|ਗਲਤ|ਠੰਡਾ|ਖਰਾਬ|ਦੇਰੀ|ਰਿਫੰਡ"
        # Urdu
        r"|غلط|ٹھنڈا|خراب|تاخیر|ریفنڈ",
        re.IGNORECASE,
    )

    @staticmethod
    def filler_phrase(language: str | None = None) -> str:
        code = AgentRuntimeService.normalize_language(language)
        return _FILLER_PHRASES.get(code) or _FILLER_PHRASES["en"]

    @staticmethod
    def agent_refusal(language: str | None = None) -> str:
        code = AgentRuntimeService.normalize_language(language)
        return _REFUSAL_MSGS.get(code) or _REFUSAL_MSGS["en"]

    @staticmethod
    def how_can_i_help(language: str | None = None) -> str:
        code = AgentRuntimeService.normalize_language(language)
        return _HOW_CAN_I_HELP.get(code) or _HOW_CAN_I_HELP["en"]

    @staticmethod
    def _human_grounded_fallback(query: str, chunks: list[dict[str, Any]], language: str | None = None) -> str:
        has_problem = bool(AgentRuntimeService._PROBLEM_TERMS_RE.search(query or ""))
        code = AgentRuntimeService.normalize_language(language)
        if has_problem:
            return _PROBLEM_FALLBACK.get(code) or _PROBLEM_FALLBACK["en"]
        return _NEUTRAL_FALLBACK.get(code) or _NEUTRAL_FALLBACK["en"]

    @staticmethod
    def voice_sanitize_answer(answer: str) -> str:
        """Remove internal/debug/script artifacts before customer-facing TTS."""
        text = re.sub(r"\s+", " ", answer or "").strip()
        text = re.sub(
            r"^(grounded answer|based on approved knowledge|based on the approved knowledge)\s*[:\-]\s*",
            "",
            text,
            flags=re.IGNORECASE,
        )
        text = re.sub(r"\b(script|guide|manual|chunk|section)\s*\d*(\.\d+)*\s*[:\-]", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*\+\s*", " and ", text)
        text = re.sub(r"\s{2,}", " ", text).strip(" -")
        return text

    @staticmethod
    def normalize_language(language: str | None) -> str:
        code = (language or "en").strip().lower().split("-", 1)[0]
        return code if code in SONIOX_INDIAN_LANGUAGE_CODES else "en"

    @staticmethod
    def language_label(language: str | None) -> str:
        code = AgentRuntimeService.normalize_language(language)
        for option in SONIOX_INDIAN_LANGUAGE_OPTIONS:
            if option["code"] == code:
                return f"{option['label']} ({option['native_label']})"
        return "English"

    # ---- Node: classify_intent ----
    @staticmethod
    async def _classify_intent_node(state: AgentGraphState) -> AgentGraphState:
        """Intent gate: Decide whether to activate Conversation Brain or Knowledge Brain.

        Also auto-detects user language from the transcript text.
        """
        query = normalize_message(state["query"])
        intent = await classify_intent(query)

        # Auto-detect language from user text (zero-cost, script-based)
        detected_lang = detect_language(query)
        # Only override if user didn't explicitly set a language via UI,
        # or if the detected language is non-English (user is clearly speaking another language)
        current_lang = state.get("response_language") or "en"
        if detected_lang != "en":
            current_lang = detected_lang

        return {
            **state,
            "intent_type": intent["type"],
            "intent_confidence": intent.get("confidence", 0.0),
            "intent_reason": intent.get("reason", ""),
            "intent_classifier": intent.get("classifier", "unknown"),
            "should_retrieve": intent.get("shouldRetrieve", False),
            "response_language": current_lang,
        }

    # ---- Node: direct_answer (Conversation Brain) ----
    @staticmethod
    async def _direct_answer_node(state: AgentGraphState) -> AgentGraphState:
        """Conversation Brain: Handle greetings, small talk, clarification naturally."""
        intent_type = state.get("intent_type", INTENT_AMBIGUOUS)
        language = state.get("response_language") or "en"
        answer = generate_conversation_reply(state["query"], intent_type, language=language)
        if not answer:
            answer = AgentRuntimeService.how_can_i_help(language)
        return {
            **state,
            "answer": answer,
            "refused": False,
            "chunks": [],
            "citations": [],
            "relevant_chunks": [],
            "retrieval_skipped_reason": f"intent={intent_type}, no retrieval needed",
            "runtime_mode": "conversation_brain",
        }

    # ---- Node: retrieve_approved_context ----
    @staticmethod
    async def _retrieve_node(state: AgentGraphState) -> AgentGraphState:
        """Knowledge Brain step 1: Retrieve document chunks from Qdrant."""
        tenant_res = state["tenant_res"]
        result = await AgentKnowledgeService.test_retrieval(
            tenant_res,
            state["query"],
            top_k=state.get("top_k") or settings.AGENT_RETRIEVAL_TOP_K,
            db=state.get("db"),
        )
        chunks = result.get("chunks") or []
        top_score = max((float(chunk.get("score", 0.0) or 0.0) for chunk in chunks), default=0.0)
        citations = [
            {
                "document_id": chunk.get("document_id"),
                "document_name": chunk.get("document_name"),
                "chunk_id": chunk.get("chunk_id"),
                "score": chunk.get("score"),
            }
            for chunk in chunks
        ]
        return {
            **state,
            "chunks": chunks,
            "citations": citations,
            "retrieved_count": len(chunks),
            "top_retrieval_score": top_score,
            "retrieval_refusal": result.get("refusal"),
        }

    # ---- Node: validate_relevance ----
    @staticmethod
    async def _validate_relevance_node(state: AgentGraphState) -> AgentGraphState:
        """Knowledge Brain step 2: Filter out low-relevance chunks."""
        chunks = state.get("chunks") or []
        relevant = validate_chunk_relevance(chunks, state.get("query") or "")
        return {
            **state,
            "relevant_chunks": relevant,
            "chunks": relevant,  # Pass only relevant chunks to answer node
        }

    # ---- Node: answer_or_refuse (Knowledge Brain) ----
    @staticmethod
    async def _answer_node(state: AgentGraphState) -> AgentGraphState:
        """Knowledge Brain step 3: Generate grounded answer from relevant chunks."""
        chunks = state.get("chunks") or []
        lang = state.get("response_language") or "en"
        if not chunks:
            answer = state.get("retrieval_refusal") or AgentRuntimeService.agent_refusal(lang)
            return {
                **state,
                "answer": answer,
                "refused": True,
                "runtime_mode": "knowledge_brain_no_context",
            }

        timeout = max(0.5, (state.get("latency_budget_ms") or settings.AGENT_LLM_TIMEOUT_MS) / 1000)
        try:
            answer = await asyncio.wait_for(
                AzureAgentLLMClient.generate_grounded_answer(
                    state["tenant_res"],
                    state["query"],
                    chunks,
                    response_language=lang,
                    conversation_history=state.get("conversation_history"),
                ),
                timeout=timeout,
            )
            if not answer:
                answer = AgentRuntimeService._human_grounded_fallback(state["query"], chunks, lang)
            answer = AgentRuntimeService.voice_sanitize_answer(answer)
            return {
                **state,
                "answer": answer,
                "refused": False,
                "runtime_mode": "knowledge_brain",
            }
        except Exception as exc:
            # Voice path must stay responsive — fall back to extractive
            return {
                **state,
                "answer": AgentRuntimeService.voice_sanitize_answer(
                    AgentRuntimeService._human_grounded_fallback(state["query"], chunks, lang)
                ),
                "refused": False,
                "error": str(exc),
                "runtime_mode": "knowledge_brain_extractive_fallback",
            }

    # ---- Routing function ----
    @staticmethod
    def _route_after_intent(state: AgentGraphState) -> str:
        """Route to Conversation Brain or Knowledge Brain based on intent."""
        if state.get("should_retrieve"):
            return "retrieve_approved_context"
        return "direct_answer"

    # ---- Build the graph ----
    @staticmethod
    def graph():
        if AgentRuntimeService._graph is None:
            workflow = StateGraph(AgentGraphState)

            # Add nodes
            workflow.add_node("classify_intent", AgentRuntimeService._classify_intent_node)
            workflow.add_node("direct_answer", AgentRuntimeService._direct_answer_node)
            workflow.add_node("retrieve_approved_context", AgentRuntimeService._retrieve_node)
            workflow.add_node("validate_relevance", AgentRuntimeService._validate_relevance_node)
            workflow.add_node("answer_or_refuse", AgentRuntimeService._answer_node)

            # Add edges
            workflow.add_edge(START, "classify_intent")
            workflow.add_conditional_edges(
                "classify_intent",
                AgentRuntimeService._route_after_intent,
                {
                    "direct_answer": "direct_answer",
                    "retrieve_approved_context": "retrieve_approved_context",
                },
            )
            workflow.add_edge("direct_answer", END)
            workflow.add_edge("retrieve_approved_context", "validate_relevance")
            workflow.add_edge("validate_relevance", "answer_or_refuse")
            workflow.add_edge("answer_or_refuse", END)

            AgentRuntimeService._graph = workflow.compile()
        return AgentRuntimeService._graph

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
    ) -> dict[str, Any]:
        language = AgentRuntimeService.normalize_language(response_language)
        state = await AgentRuntimeService.graph().ainvoke(
            {
                "query": normalize_message(query),
                "tenant_res": tenant_res,
                "db": db,
                "top_k": top_k or settings.AGENT_RETRIEVAL_TOP_K,
                "latency_budget_ms": latency_budget_ms or settings.AGENT_LLM_TIMEOUT_MS,
                "runtime_mode": "langgraph",
                "response_language": language,
                "conversation_history": conversation_history or [],
            }
        )
        return {
            "query": query,
            "answer": state.get("answer") or AgentRuntimeService.agent_refusal(language),
            "refused": bool(state.get("refused")),
            "citations": state.get("citations") or [],
            "chunks": state.get("chunks") or [],
            "runtime": {
                "graph": "langgraph_dual_brain",
                "mode": state.get("runtime_mode") or "langgraph",
                "model": settings.AZURE_OPENAI_AGENT_MODEL,
                "response_language": state.get("response_language") or language,
                "detected_language": state.get("response_language") or language,
                "llm_error": state.get("error"),
            },
            "intent": {
                "type": state.get("intent_type", "unknown"),
                "confidence": state.get("intent_confidence", 0.0),
                "reason": state.get("intent_reason", ""),
                "classifier": state.get("intent_classifier", "unknown"),
                "should_retrieve": state.get("should_retrieve", False),
                "retrieval_skipped_reason": state.get("retrieval_skipped_reason"),
            },
            "retrieval": {
                "used": bool(state.get("should_retrieve")),
                "skipped_reason": state.get("retrieval_skipped_reason"),
                "retrieved_count": int(state.get("retrieved_count") or 0),
                "relevant_count": len(state.get("chunks") or []),
                "top_score": float(state.get("top_retrieval_score") or 0.0),
                "relevance_threshold": settings.AGENT_MIN_RELEVANCE_SCORE,
            },
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
        result = await AgentRuntimeService.answer_text(
            tenant_res,
            query,
            db=db,
            top_k=settings.AGENT_RETRIEVAL_TOP_K,
            latency_budget_ms=min(settings.AGENT_LLM_TIMEOUT_MS, max(200, target_ms - 120)),
            response_language=response_language,
        )
        answer_elapsed_ms = int((perf_counter() - started) * 1000)
        tts_probe = await AgentRuntimeService._tts_first_audio_probe(
            tenant_res,
            result.get("answer") or "",
            language=AgentRuntimeService.normalize_language(response_language),
            target_ms=target_ms,
        )
        first_audio_ms = tts_probe.get("first_audio_ms")
        transcript_to_first_audio_ms = (
            answer_elapsed_ms + int(first_audio_ms)
            if isinstance(first_audio_ms, int)
            else None
        )
        return {
            **result,
            "latency": {
                "target_ms": target_ms,
                "final_transcript_to_answer_ms": answer_elapsed_ms,
                "answer_text_to_first_tts_audio_ms": first_audio_ms,
                "final_transcript_to_first_tts_audio_ms": transcript_to_first_audio_ms,
                "tts_first_audio_passed": bool(tts_probe.get("passed")),
                "passed": bool(tts_probe.get("passed")),
                "measurement": "server-side Agent answer text produced to first Soniox TTS audio chunk",
                "tts_probe": tts_probe,
            },
        }

    @staticmethod
    async def _tts_first_audio_probe(
        tenant_res: TenantResources,
        text: str,
        *,
        language: str = "en",
        target_ms: int = 800,
    ) -> dict[str, Any]:
        """Measure text-to-first-TTS-audio without sending audio to the browser."""
        if not text.strip():
            return {
                "status": "skipped",
                "passed": False,
                "reason": "empty answer text",
            }

        started = perf_counter()
        try:
            api_key = await AgentRuntimeService.soniox_api_key(tenant_res, "tts")
        except RuntimeError as exc:
            return {
                "status": "failed",
                "passed": False,
                "error_code": "missing_api_key",
                "error_message": str(exc),
            }

        provider_status = dict(tenant_res.provider_status or {})
        endpoint = provider_status.get("tts_stream_endpoint") or settings.SONIOX_TTS_STREAM_URL
        stream_id = f"latency-probe"
        timeout_seconds = max(1.0, (target_ms / 1000) + 1.5)
        try:
            async with connect(endpoint, max_size=8 * 1024 * 1024) as tts_ws:
                connected_ms = int((perf_counter() - started) * 1000)
                await tts_ws.send(
                    json.dumps(
                        {
                            "api_key": api_key,
                            "model": provider_status.get("tts_model") or settings.SONIOX_TTS_MODEL,
                            "language": language,
                            "voice": provider_status.get("tts_voice") or settings.SONIOX_TTS_VOICE,
                            "audio_format": provider_status.get("tts_audio_format") or settings.SONIOX_TTS_AUDIO_FORMAT,
                            "sample_rate": provider_status.get("tts_sample_rate") or settings.SONIOX_TTS_SAMPLE_RATE,
                            "stream_id": stream_id,
                        }
                    )
                )
                await tts_ws.send(json.dumps({"text": text[:5000], "text_end": True, "stream_id": stream_id}))
                request_sent_ms = int((perf_counter() - started) * 1000)
                while True:
                    raw = await asyncio.wait_for(tts_ws.recv(), timeout=timeout_seconds)
                    message = json.loads(raw)
                    if message.get("audio"):
                        first_audio_ms = int((perf_counter() - started) * 1000)
                        return {
                            "status": "passed" if first_audio_ms <= target_ms else "slow",
                            "passed": first_audio_ms <= target_ms,
                            "target_ms": target_ms,
                            "first_audio_ms": first_audio_ms,
                            "connected_ms": connected_ms,
                            "request_sent_ms": request_sent_ms,
                            "audio_format": provider_status.get("tts_audio_format") or settings.SONIOX_TTS_AUDIO_FORMAT,
                        }
                    if message.get("error_code"):
                        return {
                            "status": "failed",
                            "passed": False,
                            "error_code": message.get("error_code"),
                            "error_message": message.get("error_message") or "TTS service error",
                            "connected_ms": connected_ms,
                            "request_sent_ms": request_sent_ms,
                        }
                    if message.get("terminated"):
                        return {
                            "status": "failed",
                            "passed": False,
                            "error_code": "no_audio_returned",
                            "error_message": "Soniox TTS terminated before sending audio.",
                            "connected_ms": connected_ms,
                            "request_sent_ms": request_sent_ms,
                        }
        except asyncio.TimeoutError:
            return {
                "status": "failed",
                "passed": False,
                "error_code": "tts_first_audio_timeout",
                "error_message": f"No first TTS audio chunk within {timeout_seconds:.1f}s.",
            }
        except Exception as exc:
            return {
                "status": "failed",
                "passed": False,
                "error_code": "tts_probe_failed",
                "error_message": str(exc)[:200],
            }

    @staticmethod
    async def soniox_api_key(tenant_res: TenantResources, role: str) -> str:
        provider_status = dict(tenant_res.provider_status or {})
        key_ref = provider_status.get(f"{role}_api_key_ref")
        if key_ref:
            try:
                secret = await AzureKeyVaultService.get_secret_value(key_ref)
                if secret:
                    return secret
            except Exception:
                # Use global Soniox key as a safe fallback when tenant Key Vault refs exist
                # but have not been materialized yet.
                pass
        if settings.SONIOX_API_KEY:
            return settings.SONIOX_API_KEY
        raise RuntimeError("Soniox API key is not configured.")

    @staticmethod
    def runtime_status(tenant_res: TenantResources) -> dict[str, Any]:
        provider_status = dict(tenant_res.provider_status or {})
        return {
            "runtime": "agent_voice_langgraph_dual_brain",
            "graph": "langgraph_dual_brain",
            "knowledge_scope": "approved_organization_chunks_only",
            "intent_gating": True,
            "brains": ["conversation_brain", "knowledge_brain"],
            "optimization": {
                "templates_without_llm": True,
                "answer_cards_before_qdrant": True,
                "exact_cache_enabled": settings.AGENT_ANSWER_CACHE_ENABLED,
                "policy_version": provider_status.get("agent_policy_version") or "pv_default",
                "qdrant_top_k": settings.AGENT_RETRIEVAL_TOP_K,
                "max_voice_tokens": 120,
            },
            "supported_indian_languages": SONIOX_INDIAN_LANGUAGE_OPTIONS,
            "stt": {
                "provider": "soniox",
                "endpoint": provider_status.get("stt_endpoint") or settings.SONIOX_STT_WEBSOCKET_URL,
                "model": provider_status.get("stt_model") or settings.SONIOX_STT_MODEL,
                "status": "configured" if settings.SONIOX_API_KEY or provider_status.get("stt_api_key_ref") else "missing_api_key",
            },
            "llm": {
                "provider": "azure_openai",
                "model": settings.AZURE_OPENAI_AGENT_MODEL,
                "deployment": settings.AZURE_OPENAI_AGENT_DEPLOYMENT,
                "latency_budget_ms": settings.AGENT_LLM_TIMEOUT_MS,
            },
            "tts": {
                "provider": "soniox",
                "endpoint": provider_status.get("tts_stream_endpoint") or settings.SONIOX_TTS_STREAM_URL,
                "model": provider_status.get("tts_model") or settings.SONIOX_TTS_MODEL,
                "voice": provider_status.get("tts_voice") or settings.SONIOX_TTS_VOICE,
                "sample_rate": provider_status.get("tts_sample_rate") or settings.SONIOX_TTS_SAMPLE_RATE,
                "audio_format": provider_status.get("tts_audio_format") or settings.SONIOX_TTS_AUDIO_FORMAT,
                "status": "configured" if settings.SONIOX_API_KEY or provider_status.get("tts_api_key_ref") else "missing_api_key",
            },
        }
