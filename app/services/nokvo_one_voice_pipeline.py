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


# A "." only ends a sentence when NOT preceded by a digit, so a decimal amount
# ("₹2.45Cr", or a model-spaced "₹2. 45Cr") is never split mid-number. ! ? ।
# always end. The terminator is captured (group-less) so _first_sentence keeps
# slicing through match.start()+1 (the char before the whitespace).
_SENTENCE_RE = re.compile(r"(?:(?<!\d)\.|[!?।])\s+")
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
_WEEKDAY_RE = re.compile(r"\b(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b")

# Spoken ordinals → day-of-month, so "first of July" / "the twenty third" parse.
_ORDINAL_WORDS: dict[str, int] = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5, "sixth": 6,
    "seventh": 7, "eighth": 8, "ninth": 9, "tenth": 10, "eleventh": 11,
    "twelfth": 12, "thirteenth": 13, "fourteenth": 14, "fifteenth": 15,
    "sixteenth": 16, "seventeenth": 17, "eighteenth": 18, "nineteenth": 19,
    "twentieth": 20, "twenty first": 21, "twenty second": 22, "twenty third": 23,
    "twenty fourth": 24, "twenty fifth": 25, "twenty sixth": 26, "twenty seventh": 27,
    "twenty eighth": 28, "twenty ninth": 29, "thirtieth": 30, "thirty first": 31,
}


def _next_day_of_month(day: int, today: "datetime.date"):
    """Next calendar date with the given day-of-month, today or later."""
    year, month = today.year, today.month
    for _ in range(13):
        try:
            candidate = datetime(year, month, day, tzinfo=_APPOINTMENT_LOCAL_TZ).date()
        except ValueError:
            candidate = None
        if candidate is not None and candidate >= today:
            return candidate
        month += 1
        if month > 12:
            month, year = 1, year + 1
    return None


# Canonical date/time parse error lives in app.services.datetime_parse; this
# stays as a thin subclass so the appointment handler's existing
# ``except _AppointmentToolInputError`` clauses keep working while the parser
# logic is consolidated behind one module.
class _AppointmentToolInputError(DateTimeParseError):
    pass


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
    async def _acquire_member(messages: list[dict[str, str]], max_tokens: int):
        """Pick a shared LLM-pool member (replaces per-tenant key/endpoint).

        Reserves the estimated tokens from the call's sticky home box (so every
        turn hits the same deployment → prompt cache hits); if the whole pool is
        momentarily saturated, soft-falls back to the same home box rather than
        failing a live call. Returns ``(member, est_tokens)`` or ``(None, est)``
        only when no pool member is configured at all.
        """
        from app.services.llm_pool import LLMPool, _sticky_start, _call_id_var

        chars = sum(len(str(m.get("content") or "")) for m in messages)
        est = max(1, chars // 4) + int(max_tokens)
        member = await LLMPool.reserve(est)  # sticky home-preferred + failover
        if member is None:
            members = LLMPool.members()
            if not members:
                return None, est
            member = members[_sticky_start(len(members), _call_id_var.get())]
            logger.warning("NOKVO-LLM: pool saturated — soft fallback to %s", member.key_id)
        return member, est

    @staticmethod
    def _member_request(member, messages, *, stream: bool = False, max_tokens: int = 180):
        endpoint = member.endpoint.rstrip("/")
        # Responses API endpoint (Azure AI Foundry v1 / `.../openai/responses`):
        # the URL is already complete (carries its own api-version); the model +
        # input go in the body. Output is parsed by `extract_text` (output_text /
        # output[]) and streamed deltas by the `response.output_text.delta` handler
        # in `stream`. (chat/completions params like stream_options don't apply.)
        if "/responses" in endpoint:
            # gpt-5 family: (1) rejects `temperature` (only default allowed), and
            # (2) is a reasoning model — without minimal effort its reasoning
            # tokens eat the whole budget and the visible reply comes back empty.
            # `max_output_tokens` must cover reasoning + the answer, so give
            # headroom (the FORMAT prompt keeps replies to 1-3 sentences anyway).
            body: dict[str, Any] = {
                "model": member.deployment,
                "input": messages,
                "reasoning": {"effort": settings.AZURE_OPENAI_REASONING_EFFORT},
                "max_output_tokens": max(int(max_tokens), 512),
            }
            if stream:
                body["stream"] = True
            return endpoint, body

        api_version = urllib_parse.quote(settings.AZURE_OPENAI_POOL_API_VERSION.strip())
        deployment = urllib_parse.quote(member.deployment)
        if "/openai/deployments/" in endpoint:
            url = f"{endpoint}?api-version={api_version}" if "api-version=" not in endpoint else endpoint
        else:
            url = f"{endpoint}/openai/deployments/{deployment}/chat/completions?api-version={api_version}"
        body = {"messages": messages, "temperature": 0.2, "max_tokens": max_tokens}
        if stream:
            body["stream"] = True
            # Emit a final usage chunk (incl. prompt_tokens_details.cached_tokens)
            # so streamed turns report real + cached token counts for cost/caching
            # measurement. The usage chunk has empty choices → no extra spoken token.
            body["stream_options"] = {"include_usage": True}
        return url, body

    @staticmethod
    async def complete(tenant_res: TenantResources, messages: list[dict[str, str]], *, max_tokens: int = 180) -> str:
        # Lazy-import the tracer so a disabled config never pays the
        # import cost on this hot path.
        from app.services.langsmith_tracer import trace_llm, end_llm_span
        from app.services.llm_pool import LLMPool
        import time as _time

        member, _pool_est = await AzureGroundedLLM._acquire_member(messages, max_tokens)
        if member is None:
            raise NokvoOneAgentRuntimeError("No LLM pool members configured for Nokvo One agent runtime.")
        api_key = member.api_key
        url, body = AzureGroundedLLM._member_request(member, messages, max_tokens=max_tokens)
        # Deployment name lives in the URL after /openai/deployments/.
        # Pull it out for the trace metadata so a developer can filter on
        # "which deployment served this turn" from LangSmith.
        try:
            _dep_for_trace = (url.split("/openai/deployments/", 1)[1] or "").split("/", 1)[0]
        except Exception:
            _dep_for_trace = ""

        attempts = 4
        last_response = None
        _t0 = _time.perf_counter()
        attempt_count = 0
        async with trace_llm(
            name="azure_openai.complete",
            model=_dep_for_trace or None,
            messages=messages,
            max_tokens=max_tokens,
            tenant_id=str(getattr(tenant_res, "tenant_id", "")) or None,
        ) as _llm_span:
            for attempt in range(attempts):
                attempt_count = attempt + 1
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
                    logger.warning(
                        "NOKVO-LLM: 429 (complete) attempt %s/%s — sleeping %.2fs (retry_after=%r)",
                        attempt + 1, attempts, wait_for, retry_after_hdr,
                    )
                    await asyncio.sleep(wait_for)
                    continue
                logger.warning(f"NOKVO-LLM: 429 (complete) — giving up after {attempt + 1} attempt(s); retry_after={retry_after_hdr!r}")
                await LLMPool.cooldown(member)
                end_llm_span(_llm_span, {
                    "status": "rate_limited",
                    "attempts": attempt_count,
                    "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                })
                raise NokvoOneAgentRateLimited(
                    f"Azure OpenAI rate-limited (429): {response.text[:300]}",
                    retry_after_seconds=retry_after or None,
                )
            response = last_response
            if response.status_code >= 400:
                end_llm_span(_llm_span, {
                    "status": "error",
                    "http_status": response.status_code,
                    "attempts": attempt_count,
                    "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                })
                raise NokvoOneAgentRuntimeError(f"Azure OpenAI request failed ({response.status_code}): {response.text[:300]}")
            payload = response.json()
            text = AzureGroundedLLM.extract_text(payload)
            usage = payload.get("usage") if isinstance(payload, dict) else None
            await LLMPool.reconcile(member, _pool_est, int((usage or {}).get("total_tokens") or _pool_est))
            end_llm_span(_llm_span, {
                "response": text,
                "status": "completed",
                "attempts": attempt_count,
                "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                "prompt_tokens": (usage or {}).get("prompt_tokens"),
                "completion_tokens": (usage or {}).get("completion_tokens"),
                "total_tokens": (usage or {}).get("total_tokens"),
            })
            return text

    @staticmethod
    async def complete_global(
        messages: list[dict[str, str]], *, max_tokens: int = 200
    ) -> str:
        """Same as :meth:`complete`, but pinned to the GLOBAL deployment
        (``settings.AZURE_OPENAI_GLOBAL_*``) regardless of tenant.

        Use this for cheap background tasks that don't justify burning a
        tenant's agent-deployment quota — the post-call condenser, future
        bulk summary jobs, etc. The endpoint, deployment, API version, and
        API key all come from the env-level global settings; tenant
        provider_status / secret_refs are ignored.

        Returns the model's text response. Raises on non-2xx (no rate
        limit retry: this is for background work, callers handle failure
        by skipping the artefact).
        """
        from app.services.llm_pool import LLMPool
        member, _pool_est = await AzureGroundedLLM._acquire_member(messages, max_tokens)
        if member is None:
            raise NokvoOneAgentRuntimeError(
                "No LLM pool members configured — complete_global() needs the "
                "AZURE_OPENAI pool (or GLOBAL fallback) configured."
            )
        api_key = member.api_key
        deployment = member.deployment
        url, body = AzureGroundedLLM._member_request(member, messages, max_tokens=max_tokens)
        from app.services.langsmith_tracer import trace_llm, end_llm_span
        import time as _time

        _t0 = _time.perf_counter()
        async with trace_llm(
            name="azure_openai.complete_global",
            model=deployment,
            messages=messages,
            max_tokens=max_tokens,
            deployment_kind="global",
        ) as _llm_span:
            response = await AzureGroundedLLM.http().post(
                url,
                headers={"api-key": api_key, "Content-Type": "application/json"},
                json=body,
            )
            if response.status_code >= 400:
                end_llm_span(_llm_span, {
                    "status": "error",
                    "http_status": response.status_code,
                    "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                })
                raise NokvoOneAgentRuntimeError(
                    f"Azure OpenAI (global) request failed ({response.status_code}): "
                    f"{response.text[:300]}"
                )
            payload = response.json()
            text = AzureGroundedLLM.extract_text(payload)
            usage = payload.get("usage") if isinstance(payload, dict) else None
            await LLMPool.reconcile(member, _pool_est, int((usage or {}).get("total_tokens") or _pool_est))
            end_llm_span(_llm_span, {
                "response": text,
                "status": "completed",
                "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                "prompt_tokens": (usage or {}).get("prompt_tokens"),
                "completion_tokens": (usage or {}).get("completion_tokens"),
                "total_tokens": (usage or {}).get("total_tokens"),
            })
            return text

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
    async def complete_nano(messages: list[dict[str, str]], *, max_tokens: int = 120) -> str:
        """Cheap, non-streaming completion on the gpt-4.1-nano POOL — for
        background tasks like the in-call rolling summary. Round-robin across nano
        keys for quota (no stickiness — summaries don't benefit from caching).
        Falls back to the gpt-5-mini pool (``complete_global``) when no nano is
        configured. No retry: callers treat failure as 'keep the prior artefact'."""
        from app.services.llm_pool import LLMPool

        if not LLMPool.members("nano"):
            return await AzureGroundedLLM.complete_global(messages, max_tokens=max_tokens)
        chars = sum(len(str(m.get("content") or "")) for m in messages)
        est = max(1, chars // 4) + int(max_tokens)
        member = await LLMPool.reserve(est, pool="nano", sticky=False)
        if member is None:
            import random as _random
            members = LLMPool.members("nano")
            member = members[_random.randrange(len(members))]  # all boxes capped → soft-fall
        url, body = AzureGroundedLLM._member_request(member, messages, max_tokens=max_tokens)
        try:
            resp = await AzureGroundedLLM.http().post(
                url, headers={"api-key": member.api_key, "Content-Type": "application/json"}, json=body,
            )
            if resp.status_code == 429:
                await LLMPool.cooldown(member, pool="nano")
            resp.raise_for_status()
            payload = resp.json()
            actual = int(((payload.get("usage") or {}).get("total_tokens")) or est)
            await LLMPool.reconcile(member, est, actual, pool="nano")
            return AzureGroundedLLM.extract_text(payload)
        except Exception:
            await LLMPool.reconcile(member, est, 0, pool="nano")
            raise

    @staticmethod
    async def stream(
        tenant_res: TenantResources,
        messages: list[dict[str, str]],
        *,
        max_tokens: int = 180,
        retry_attempts: int | None = None,
        max_retry_wait_s: float | None = None,
    ) -> AsyncIterator[str]:
        # Lazy-import so the disabled-tracing path costs nothing.
        from app.services.langsmith_tracer import trace_llm, end_llm_span
        import time as _time

        member, _pool_est = await AzureGroundedLLM._acquire_member(messages, max_tokens)
        if member is None:
            raise NokvoOneAgentRuntimeError("No LLM pool members configured for Nokvo One agent runtime.")
        api_key = member.api_key
        url, body = AzureGroundedLLM._member_request(
            member,
            messages,
            stream=True,
            max_tokens=max_tokens,
        )
        try:
            _dep_for_trace = (url.split("/openai/deployments/", 1)[1] or "").split("/", 1)[0]
        except Exception:
            _dep_for_trace = ""

        # Azure OpenAI per-tenant deployments often have low TPM/RPM. In
        # interactive testing the user fires several turns in quick succession
        # and trips the quota — the agent then says "Give me a second, I'm a
        # bit busy" which sounds like Sarvam crashed but is actually Azure LLM.
        # Be more patient: up to 4 attempts, honor Retry-After up to 3.5s,
        # and fall back to an exponential 0.6 / 1.2 / 2.4s wait when Azure
        # doesn't tell us how long.
        attempts = max(1, int(retry_attempts or 4))
        retry_wait_cap = 3.5 if max_retry_wait_s is None else max(0.0, float(max_retry_wait_s))

        # Trace wrapper around the entire stream. Buffer every yielded token
        # so the trace carries the full assembled response (or the partial
        # one, when barge-in cancels mid-flight). The barge-in path is
        # signposted with status="cancelled_barge_in" so a developer can
        # search LangSmith for "why did the agent stop mid-sentence?".
        _t0 = _time.perf_counter()
        _buffer: list[str] = []
        _usage: dict[str, Any] | None = None
        async with trace_llm(
            name="azure_openai.stream",
            model=_dep_for_trace or None,
            messages=messages,
            max_tokens=max_tokens,
            stream=True,
            tenant_id=str(getattr(tenant_res, "tenant_id", "")) or None,
        ) as _llm_span:
            try:
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
                                logger.warning(
                                    "NOKVO-LLM: 429 attempt %s/%s — sleeping %.2fs (retry_after=%r)",
                                    attempt + 1, attempts, wait_for, retry_after_hdr,
                                )
                                await asyncio.sleep(wait_for)
                                continue
                            logger.warning(f"NOKVO-LLM: 429 — giving up after {attempt + 1} attempt(s); retry_after={retry_after_hdr!r}")
                            end_llm_span(_llm_span, {
                                "response": "".join(_buffer),
                                "status": "rate_limited",
                                "attempts": attempt + 1,
                                "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                                "tokens_before_cancel": len(_buffer),
                            })
                            raise NokvoOneAgentRateLimited(
                                f"Azure OpenAI rate-limited (429): {body_text}",
                                retry_after_seconds=retry_after or None,
                            )
                        if response.status_code >= 400:
                            text = await response.aread()
                            end_llm_span(_llm_span, {
                                "response": "".join(_buffer),
                                "status": "error",
                                "http_status": response.status_code,
                                "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                            })
                            raise NokvoOneAgentRuntimeError(f"Azure OpenAI stream failed ({response.status_code}): {text[:300]!r}")
                        async for line in response.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            raw = line[6:]
                            if raw.strip() == "[DONE]":
                                end_llm_span(_llm_span, {
                                    "response": "".join(_buffer),
                                    "status": "completed",
                                    "tokens": len(_buffer),
                                    "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                                    "prompt_tokens": (_usage or {}).get("prompt_tokens"),
                                    "completion_tokens": (_usage or {}).get("completion_tokens"),
                                    "total_tokens": (_usage or {}).get("total_tokens"),
                                    "cached_tokens": ((_usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens"),
                                })
                                return
                            try:
                                event = json.loads(raw)
                            except json.JSONDecodeError:
                                continue
                            if event.get("usage"):
                                _usage = event["usage"]  # final stream_options usage chunk
                            if event.get("type") == "response.output_text.delta":
                                token = event.get("delta") or ""
                                if token:
                                    _buffer.append(token)
                                    yield token
                                continue
                            choices = event.get("choices") or []
                            if choices:
                                token = ((choices[0].get("delta") or {}).get("content")) or ""
                                if token:
                                    _buffer.append(token)
                                    yield token
                        end_llm_span(_llm_span, {
                            "response": "".join(_buffer),
                            "status": "completed",
                            "tokens": len(_buffer),
                            "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                            "prompt_tokens": (_usage or {}).get("prompt_tokens"),
                            "completion_tokens": (_usage or {}).get("completion_tokens"),
                            "total_tokens": (_usage or {}).get("total_tokens"),
                            "cached_tokens": ((_usage or {}).get("prompt_tokens_details") or {}).get("cached_tokens"),
                        })
                        return  # successful stream — exit the retry loop
            except (asyncio.CancelledError, GeneratorExit):
                # Barge-in: the voice turn arbiter cancelled this stream
                # because the caller started speaking. Stamp the partial
                # response with a status so a developer can find "agent
                # stopped mid-sentence" turns in LangSmith without spelunking
                # logs. Re-raise so the arbiter still tears down TTS.
                end_llm_span(_llm_span, {
                    "response": "".join(_buffer),
                    "status": "cancelled_barge_in",
                    "tokens_before_cancel": len(_buffer),
                    "latency_ms": int((_time.perf_counter() - _t0) * 1000),
                })
                raise


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
        # Knowledge-Base document retrieval is retired: the agent answers from
        # its vertical system prompt, not Qdrant/embedding retrieval. Returns an
        # empty result; callers handle ``chunks == []`` by not grounding.
        return {
            "query": query,
            "chunks": [],
            "refusal": None,
            "sensitive": bool(intent_result and intent_result.sensitive),
            "min_score": 0.0,
            "top_k": top_k or 0,
        }
        # --- retired (unreachable below) ---
        # LangSmith retriever span. Currently this function returns an
        # empty chunks list because Qdrant retrieval is retired from the
        # runtime path (see comment below). The span still posts — the
        # zero-chunk result is itself useful debugging signal ("retrieval
        # is disabled, answers come from prompt + memory only"). When
        # Qdrant is re-enabled the spans populate without further work.
        try:
            from langsmith.run_helpers import traceable, get_current_run_tree
            _parent = get_current_run_tree()
        except Exception:
            _parent = None
        if _parent is not None:
            try:
                _retr_span = _parent.create_child(
                    name="retrieval",
                    run_type="retriever",
                    inputs={
                        "query": query,
                        "top_k": top_k,
                        "campaign_id": campaign_id,
                        "dual": dual_retrieval,
                    },
                )
                _retr_span.post()
            except Exception:
                _retr_span = None
        else:
            _retr_span = None
        # Qdrant / KB-document retrieval has been retired from the runtime
        # pipeline. The agent now answers from: (a) the curated per-vertical
        # system prompt + the org's BUSINESS FACTS (see
        # app/services/vertical_prompts.py + agent_runtime_bundle), (b) the
        # live real-estate project inventory, (c) conversational memory, and
        # (d) the slot FSM's deterministic flow. Returning an empty chunks
        # list short-circuits every downstream "no chunks → refuse" gate AND
        # keeps the LLM path active because every call site also checks
        # ``single_prompt_guidance`` (now always present) / ``outbound_mode``
        # before refusing. (Policy-card synthetic chunks are injected by
        # callers separately and are unaffected.)
        if not query.strip():
            _result = {"query": query, "chunks": [], "refusal": "Empty query."}
            if _retr_span is not None:
                try:
                    _retr_span.add_outputs({"chunks": [], "chunk_count": 0, "status": "empty_query"})
                    _retr_span.end()
                    _retr_span.patch()
                except Exception:
                    pass
            return _result
        _result = {"query": query, "chunks": [], "refusal": None}
        if _retr_span is not None:
            try:
                _retr_span.add_outputs({
                    "chunks": [],
                    "chunk_count": 0,
                    "status": "qdrant_disabled",
                })
                _retr_span.end()
                _retr_span.patch()
            except Exception:
                pass
        return _result
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
            "source_type": "agent_knowledge",
        }
        if campaign_id:
            filters["campaign_id"] = campaign_id
        if sensitive and intent_result and intent_result.topic and intent_result.topic != "general":
            filters["topic"] = intent_result.topic

        vector = None  # retired: embeddings/KB retrieval removed
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
            points = []  # retired: Qdrant/KB retrieval removed
            # Debug-level so production stdout/log volume doesn't carry the
            # caller's query text or per-turn retrieval stats by default; ops
            # can flip the logger to DEBUG when actually investigating.
            logger.debug(
                "NOKVO-RETRIEVE: tenant=%s label=%s query=%r filters=%s min_score=%s "
                "top_k=%s raw_results=%s scores=%s qdrant_ms=%s",
                tenant_res.tenant_id, label, query[:60], payload_filters,
                min_score, effective_top_k, len(points),
                [round(float(getattr(p, 'score', 0.0) or 0.0), 3) for p in points[:5]],
                int((perf_counter() - started) * 1000),
            )
            return points

        primary_task = asyncio.create_task(_search("primary", filters))
        relaxed_task: asyncio.Task[list[Any]] | None = None
        minimal_task: asyncio.Task[list[Any]] | None = None
        relaxed_filters: dict[str, Any] | None = None
        minimal_filters = {"source_type": "agent_knowledge"}

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
        # Explicit-admin-override probe only. This gates whether to SUPPRESS
        # the built-in FSMs (clinic appointments, etc.) — NOT whether the agent
        # has a persona. The curated per-vertical persona is always present and
        # is composed separately on the async bundle path
        # (``agent_runtime_bundle._single_prompt_guidance``). Returning "" when
        # no legacy override is configured (the normal case now) lets the
        # built-in FSMs run.
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
    async def _projects_block_for_bundle(
        db: AsyncSession | None,
        bundle: "RuntimeBundle",
    ) -> tuple[str, list]:
        """Return ``(inventory_block, active_projects)`` for a real-estate org,
        or ``("", [])`` otherwise.

        The block is injected as its own top-level system section by the
        voice prompt builder so the live agent treats it as the source of
        truth for inventory questions (overriding any project names the
        admin may have hardcoded into their single-prompt text). The project
        list is handed back so callers can reuse it (project-name hints,
        objection focus) without a second round-trip — the underlying
        ``load_active_projects`` is uncached."""
        if (bundle.organization_industry or "").lower() != "real_estate":
            return "", []
        organization_id = getattr(bundle.organization, "id", None)
        if organization_id is None:
            return "", []
        try:
            from app.services.real_estate_project_service import (
                load_active_projects,
                projects_prompt_section,
            )

            projects = await load_active_projects(db, organization_id)
        except Exception:
            return "", []
        return projects_prompt_section(projects), projects

    @staticmethod
    async def _services_block_for_bundle(
        db: AsyncSession | None,
        bundle: "RuntimeBundle",
    ) -> str:
        """Authoritative clinic SERVICES catalog block (services + which doctors
        + price/duration) for a clinic org, else "". Injected as its own system
        section so the agent quotes real services/doctors and routes booking
        service-first. Loaded per-call (uncached) so edits reflect immediately."""
        if (bundle.organization_industry or "").lower() != "clinics":
            return ""
        organization_id = getattr(bundle.organization, "id", None)
        if organization_id is None:
            return ""
        try:
            from app.services.clinic_service_service import (
                load_services_with_providers,
                services_prompt_section,
            )

            services = await load_services_with_providers(db, organization_id)
        except Exception:
            return ""
        return services_prompt_section(services)

    @staticmethod
    def _focus_project_summary(
        projects: list,
        conversational_memory: Any,
    ) -> str | None:
        """One-line summary of the project the caller named (matched from
        FACT_PROPERTY), for the strategy layer's price/competitor objection
        focus. ``None`` when no property is known or no confident match exists."""
        if conversational_memory is None or not projects:
            return None
        try:
            from app.services.conversational_memory import FACT_PROPERTY
            from app.services.real_estate_project_service import (
                find_project_match,
                project_summary_lines,
            )

            spoken = conversational_memory.get(FACT_PROPERTY)
            if not spoken:
                return None
            project = find_project_match(projects, project_name=str(spoken))
            if project is None:
                return None
            lines = project_summary_lines([project])
            return lines[0] if lines else None
        except Exception:
            return None

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
        raw = re.sub(r"\s+", " ", normalize_relative_datetime_text(str(value or "")).strip().lower())
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
        # "in/after N days" → concrete offset.
        rel_days = re.search(r"\b(?:in|after)\s+(\d{1,2})\s+days?\b", raw)
        if rel_days:
            n = int(rel_days.group(1))
            if 0 < n <= 60:
                return today + timedelta(days=n)
        # "this/next weekend" → the upcoming Saturday.
        if "weekend" in raw:
            delta = (5 - today.weekday()) % 7
            return today + timedelta(days=delta or 7)
        # Weekday name — word-boundary match (so "mondayish" doesn't match) and
        # ALWAYS the upcoming occurrence, never today (fixes "Monday"/"next
        # Monday" resolving to today when today is that weekday).
        weekday_match = _WEEKDAY_RE.search(raw)
        if weekday_match:
            target = _WEEKDAY_INDEX[weekday_match.group(1)]
            delta = (target - today.weekday()) % 7
            if delta == 0:
                delta = 7
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

        # Bare day-of-month with an ordinal suffix: "the 15th", "15th", "on the 3rd".
        # (Requires the suffix so a stray "15" isn't mistaken for a date.)
        bare_dom = re.search(r"\b(?:on\s+the\s+|the\s+)?(\d{1,2})(?:st|nd|rd|th)\b", raw)
        if bare_dom:
            cand = _next_day_of_month(int(bare_dom.group(1)), today)
            if cand is not None:
                return cand
            raise _AppointmentToolInputError(
                "preferred_date",
                "That date does not look valid. Which date should I note?",
                clear_date=True,
            )

        # Spoken word ordinals: "first of July", "the twenty third". Longest
        # phrase first so "twenty first" beats "first".
        for word in sorted(_ORDINAL_WORDS, key=len, reverse=True):
            if re.search(rf"\b{word}\b", raw):
                day = _ORDINAL_WORDS[word]
                month_for_word = None
                for mname, mnum in _MONTH_INDEX.items():
                    if re.search(rf"\b{mname}\b", raw):
                        month_for_word = mnum
                        break
                if month_for_word is not None:
                    try:
                        cand = datetime(today.year, month_for_word, day, tzinfo=_APPOINTMENT_LOCAL_TZ).date()
                    except ValueError:
                        cand = None
                    if cand is not None:
                        return cand if cand >= today else cand.replace(year=cand.year + 1)
                cand = _next_day_of_month(day, today)
                if cand is not None:
                    return cand
                break

        raise _AppointmentToolInputError(
            "preferred_date",
            "I need the appointment date clearly. Which date should I note?",
            clear_date=True,
        )

    @staticmethod
    def _parse_appointment_time(value: Any) -> time:
        raw = re.sub(r"\s+", " ", normalize_relative_datetime_text(str(value or "")).strip().lower())
        if not raw:
            raise _AppointmentToolInputError("preferred_time", "What time should I note for the appointment?")
        # Midnight is out of booking hours — clarify instead of resolving it (it
        # used to substring-match "night" and book 7 PM).
        if re.search(r"\bmidnight\b", raw):
            raise _AppointmentToolInputError(
                "preferred_time",
                "We don't book at midnight — what daytime works for you?",
                clear_time=True,
            )

        def _daytime_hour(h: int) -> int | None:
            """Map a 1–12 spoken hour to 24h assuming a daytime booking
            (8–11 → AM, 12 → noon, 1–7 → PM). Returns None when ambiguous."""
            if h == 12:
                return 12
            if 8 <= h <= 11:
                return h
            if 1 <= h <= 7:
                return h + 12
            return None

        # Spoken fractions: "half past 4", "quarter past 4", "quarter to 5".
        half = re.search(r"\bhalf\s*past\s+(\d{1,2})\b", raw)
        if half:
            hh = _daytime_hour(int(half.group(1)))
            if hh is not None:
                return time(hh, 30)
        qpast = re.search(r"\bquarter\s*past\s+(\d{1,2})\b", raw)
        if qpast:
            hh = _daytime_hour(int(qpast.group(1)))
            if hh is not None:
                return time(hh, 15)
        qto = re.search(r"\bquarter\s*to\s+(\d{1,2})\b", raw)
        if qto:
            base = int(qto.group(1)) - 1
            hh = _daytime_hour(base if base >= 1 else 12)
            if hh is not None:
                return time(hh, 45)

        named_times = {
            "morning": time(9, 0),
            "afternoon": time(14, 0),
            "evening": time(17, 0),
            "night": time(19, 0),
            "noon": time(12, 0),
        }
        for label, parsed in named_times.items():
            if re.search(rf"\b{label}\b", raw):
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
        bare = re.fullmatch(r"(?:at\s+)?(\d{1,2})(?:\s*ish)?", raw)
        if bare:
            hour = int(bare.group(1))
            if 13 <= hour <= 23:
                return time(hour, 0)
            inferred = _daytime_hour(hour)
            if inferred is not None:
                return time(inferred, 0)
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

        # Snapshot the FK primitive now (tenant_res is still fresh — nothing has
        # committed/rolled back yet). The fresh-session retry below rolls back on
        # failure, which expires tenant_res's attributes; re-reading them would
        # trigger a sync ORM reload outside the greenlet (MissingGreenlet).
        org_id = getattr(tenant_res, "organization_id")

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
        # Service-first routing (clinics): the captured service text is passed
        # to the booking tool, which resolves it to the doctors who provide it
        # and constrains assignment to them. Optional — omitted when not asked.
        _svc = appointment.get("service")
        if isinstance(_svc, str) and _svc.strip():
            args["service"] = _svc.strip()[:200]
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
        from app.db.session import AsyncSessionLocal

        result = None
        last_exc: Exception | None = None
        max_inline_attempts = 1 + RETRY_POLICY.inline_retries
        for attempt in range(max_inline_attempts):
            # First attempt uses the shared call session (tests assert
            # on its commit flag). Retries use a fresh AsyncSession to
            # sidestep greenlet_spawn / session-corruption issues that
            # the long-lived call session can accumulate across many turns.
            use_fresh_session = attempt > 0
            try:
                if use_fresh_session:
                    async with AsyncSessionLocal() as tool_db:
                        result = await PredefinedToolsService.execute(
                            tool_db,
                            org_id,
                            None,
                            tool,
                            args,
                            session_id=call_id,
                        )
                        await tool_db.commit()
                else:
                    result = await PredefinedToolsService.execute(
                        db,
                        org_id,
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
                logger.warning(
                    "NOKVO-APPT: %s failed (attempt %s/%s, fresh_session=%s): %r",
                    tool.key,
                    attempt + 1,
                    max_inline_attempts,
                    use_fresh_session,
                    exc,
                    exc_info=True,
                )
                if not use_fresh_session and db is not None:
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
                    organization_id=org_id,
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
            # "Property" column → prefer the matched project name (what the
            # caller is actually visiting); fall back to the free-text area.
            if not merged.get("property_id"):
                property_value = merged.get("project_name") or merged.get("location")
                if property_value:
                    merged["property_id"] = property_value
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
        force_ticket: bool = False,
    ) -> None:
        """Decide which tab a macro-created record belongs in.

        Two rules, in priority order:

        * ``force_ticket`` — the *action* is tab-defining. A booked site
          visit always belongs in the Site Visits (tickets) tab no matter
          who placed the call, so callers set this for action-routed flows
          (e.g. ``real_estate_site_visit``). This is what the operator means
          by "site visits go to the Site Visits tab" — it's about what the
          caller booked, not the call direction.
        * Otherwise fall back to the call-direction heuristic: inbound
          callers reached out for help → tickets tab; outbound calls we
          initiated → leads tab (the macro already creates leads there, so
          outbound needs no rewrite).

        When we do rewrite, we flip ``record_type`` from ``lead`` to
        ``ticket`` AND project the data dict onto the ticket schema's
        expected field keys so the UI renders populated cells (otherwise the
        row looks blank and the operator thinks no record was created)."""
        if not record_ids:
            return
        rewrite_to_ticket = force_ticket or call_surface == "voice_inbound"
        if not rewrite_to_ticket:
            return
        from app.models.nokvo_one_tool_record import NokvoOneToolRecord
        from app.services.nokvo_one_business_templates import STATUS_VOCABULARIES
        from sqlalchemy import select
        import uuid as _uuid

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
                if call_surface:
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
    @staticmethod
    def _real_estate_opt_out(
        *,
        memory: dict[str, Any],
        history: list[dict[str, str]],
    ) -> bool:
        """True when the caller explicitly opted out — wrong number / do-not-call
        / not interested. Such callers must NEVER become a follow-up-eligible
        lead (DND/TRAI). Checks both the extracted objection and recent caller
        utterances."""
        blob = str(memory.get("objection") or "").lower()
        user_text = " ".join(
            str(turn.get("content") or "")
            for turn in (history or [])[-12:]
            if turn.get("role") == "user"
        ).lower()
        return bool(
            re.search(
                r"\b(not interested|don'?t call|do not call|do-not-call|remove me|"
                r"wrong number|stop calling|take me off|unsubscribe)\b",
                f"{blob} {user_text}",
            )
        )

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
        # A lead is name + phone only. The rest of the conversation (BHK, budget,
        # area, intent) is captured in the post-call "call notes"
        # (data.handoff_note), not as structured lead fields.
        args: dict[str, Any] = {
            "name": str(name).strip()[:200] or "Property inquiry",
            "phone": phone,
        }
        return {key: value for key, value in args.items() if value not in (None, "")}

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
        """Build ``qualify_lead_and_schedule_visit`` args when the call holds a
        FIRM site-visit booking — name + phone + a parseable visit date AND
        time. Returns ``None`` for enquiry / vague calls (no firm date/time) so
        those stay leads. Used by the end-of-call safety net so a booking the
        deterministic flow didn't capture is filed as a Site Visit, not a Lead."""
        try:
            from app.services.conversational_memory import (
                ConversationalMemory as _CM,
                FACT_NAME as _FACT_NAME,
                FACT_PHONE as _FACT_PHONE,
                FACT_PROPERTY as _FACT_PROPERTY,
                FACT_VISIT_DATE as _FACT_VISIT_DATE,
                FACT_VISIT_TIME as _FACT_VISIT_TIME,
            )

            cm = _CM.from_state_blob((state or {}).get("memory") or {})
        except Exception:
            return None

        collected = dict((state.get("tool_flow") or {}).get("collected") or {})

        def _collected_by(predicate) -> Any:
            for key, value in collected.items():
                if value not in (None, "") and predicate(key):
                    return value
            return None

        date_raw = cm.get(_FACT_VISIT_DATE) or _collected_by(lambda k: "date" in k.lower())
        time_raw = cm.get(_FACT_VISIT_TIME) or _collected_by(lambda k: "time" in k.lower())
        if not (date_raw and time_raw):
            return None
        # A firm booking needs a concrete date AND time. Vague input ("morning",
        # "sometime next week") raises here, which correctly keeps it a lead.
        try:
            visit_date = NokvoOneVoicePipeline._parse_appointment_date(date_raw)
            visit_time = NokvoOneVoicePipeline._parse_appointment_time(time_raw)
        except Exception:
            return None

        name_val = (
            cm.get(_FACT_NAME)
            or memory.get("name")
            or _collected_by(lambda k: "name" in k.lower())
        )
        phone_val = (
            cm.get(_FACT_PHONE)
            or NokvoOneVoicePipeline._phone_from_call_context(memory, campaign_context)
        )
        if not (name_val and phone_val):
            return None

        project_val = (
            cm.get(_FACT_PROPERTY)
            or _collected_by(lambda k: "project" in k.lower())
            or collected.get("property_id")
        )

        visit_at = datetime.combine(
            visit_date, visit_time, tzinfo=_APPOINTMENT_LOCAL_TZ
        ).astimezone(timezone.utc).isoformat()

        # Project record_data onto the org's configured Site Visit Fields so the
        # Site Visits tab renders populated cells, mirroring the deterministic
        # flow's construction.
        canonical = {
            "date": visit_date.isoformat(),
            "time": visit_time.strftime("%I:%M %p").lstrip("0"),
            # Combined "Date and Time" field (a single datetime slot) renders one
            # human-readable cell. Mirrors the split date/time formatting above so
            # the Site Visits tab shows the same values, just in one column.
            "datetime": f"{visit_date.isoformat()} {visit_time.strftime('%I:%M %p').lstrip('0')}",
            "name": str(name_val),
            "phone": str(phone_val),
            "project": str(project_val) if project_val else None,
        }
        record_data: dict[str, Any] = {}
        try:
            from app.services.tool_flow_questions import build_tool_flow_questions

            sv_bundle = build_tool_flow_questions(
                getattr(organization, "industry", None), overrides, custom_tabs
            )
            flow_slots = (
                ((sv_bundle.get("flows") or {}).get("real_estate_site_visit") or {}).get("slots") or []
            )
            for slot in flow_slots:
                kind = str(slot.get("kind") or "")
                fkey = str(slot.get("source_field") or slot.get("key") or "")
                value = canonical.get(kind)
                if fkey and value not in (None, ""):
                    record_data[fkey] = value
        except Exception:
            record_data = {}
        if not record_data:
            # Default real_estate Site Visit Fields.
            record_data = {
                "name": str(name_val),
                "phone": str(phone_val),
                "visit_date": canonical["date"],
                "visit_time": canonical["time"],
            }
            if project_val:
                record_data["project_name"] = str(project_val)

        args: dict[str, Any] = {
            "name": str(name_val),
            "phone": str(phone_val),
            "visit_at": visit_at,
            "record_data": record_data,
        }
        if project_val not in (None, ""):
            args["project_name"] = str(project_val)
        return args

    @staticmethod
    async def _send_brochure_and_location_sms(
        db: AsyncSession,
        org_id: Any,
        tenant_res: TenantResources,
        call_id: str,
        state: dict[str, Any],
    ) -> None:
        """At call end, text the caller (their ANI) the project's brochure +
        location links in ONE SMS. This is the inbound-real-estate delivery channel
        while WhatsApp is off — the number is the one they're calling from, so
        nothing is asked. Idempotent per call (``sms_sent``); best-effort — callers
        wrap it so an SMS failure never affects the lead."""
        ani = str((state or {}).get("caller_phone") or "").strip()
        if not ani:
            return
        if state.get("sms_sent"):
            return
        from app.services.real_estate_project_service import (
            find_project_match,
            load_active_projects,
        )
        from app.services.sms_service import SmsService

        projects = await load_active_projects(db, org_id)
        if not projects:
            return
        # Resolve which project to send: the one the caller discussed (memory
        # FACT_PROPERTY), else the sole active project. Never guess across many —
        # the QUERY prompt asks which project before promising when there's >1.
        captured = None
        try:
            from app.services.conversational_memory import (
                ConversationalMemory as _CM,
                FACT_PROPERTY as _FACT_PROPERTY,
            )

            captured = _CM.from_state_blob((state or {}).get("memory") or {}).get(_FACT_PROPERTY)
        except Exception:
            captured = None
        matched = find_project_match(
            projects, project_name=str(captured) if captured else None
        )
        if matched is None and len(projects) == 1:
            matched = projects[0]
        if matched is None:
            return

        # The two links: brochure (a column) + location maps URL (lives in the
        # project's whatsapp.location config — reused as the SMS map link).
        brochure_url = str(getattr(matched, "brochure_url", None) or "").strip()
        wa_cfg = getattr(matched, "whatsapp", None) or {}
        maps_url = str(((wa_cfg.get("location") or {}).get("maps_url")) or "").strip()
        if not brochure_url and not maps_url:
            return  # nothing to send

        name = getattr(matched, "name", None) or "your enquiry"
        parts = [f"Hi! Details for {name}:"]
        if brochure_url:
            parts.append(f"Brochure: {brochure_url}")
        if maps_url:
            parts.append(f"Location: {maps_url}")
        text = " ".join(parts)

        res = await SmsService.send_for_org(db, org_id, to_number=ani, text=text)
        if res.get("ok"):
            await AgentSessionStore.merge_state(tenant_res, call_id, {"sms_sent": True})

    @staticmethod
    def _deterministic_call_note(
        *,
        kind: str,
        name: str | None,
        ani: str | None,
        memory: dict[str, Any],
        history: list[dict[str, str]],
    ) -> str:
        """Plain-prose fallback call note built deterministically from captured
        facts, written SYNCHRONOUSLY at record creation so a flaky post-call LLM
        condenser can never leave the record noteless. The background condenser
        overwrites this with a richer summary when it succeeds. Shaped so
        ``REAgentScheduler``'s extractor can still read the visit date/time."""
        from app.services.voice_turn_policy import extract_datetime_phrase

        mem = memory or {}
        parts: list[str] = [
            "Caller agreed to a site visit."
            if kind == "site_visit"
            else "Caller enquired about properties."
        ]
        # Visit date/time — scan recent caller turns, normalising hi/te relative
        # tokens so a Telugu "రేపు 10" still yields "tomorrow 10 AM".
        when = ""
        for turn in reversed((history or [])[-12:]):
            if turn.get("role") != "user":
                continue
            when = extract_datetime_phrase(str(turn.get("content") or ""))
            if when:
                break
        if when:
            parts.append(f"Proposed visit time: {when}.")
        if mem.get("bhk"):
            parts.append(f"Configuration: {mem['bhk']}.")
        if mem.get("location_preference"):
            parts.append(f"Preferred area: {mem['location_preference']}.")
        if mem.get("purpose"):
            parts.append(f"Purpose: {mem['purpose']}.")
        if mem.get("budget"):
            parts.append(f"Budget: {mem['budget']}.")
        if mem.get("requested_info"):
            parts.append(f"Asked for: {mem['requested_info']}.")
        who = [bit for bit in (f"Name: {name}" if name else "", f"Phone: {ani}" if ani else "") if bit]
        if who:
            parts.append("; ".join(who) + ".")
        return " ".join(parts)

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
        """Create a minimal inbound site-visit TICKET when the caller agreed to
        come — ANI + name (if captured) only. NO structured date/time/project
        fields: the date/time the agent clarified rides in the post-call note.
        We write a DETERMINISTIC ``data.handoff_note`` here synchronously (so the
        record is never noteless if the post-call LLM condenser fails); the
        condenser later overwrites it with a richer summary when it succeeds.
        Mirrors the phoneless-lead direct write; best-effort."""
        from app.models.nokvo_one_tool_record import NokvoOneToolRecord
        from app.services.nokvo_one_business_templates import STATUS_VOCABULARIES

        ani = str((state or {}).get("caller_phone") or "").strip() or None
        name = str((memory or {}).get("name") or "").strip() or None
        if not name:
            # Fall back to the durable captured name (ConversationalMemory
            # FACT_NAME) — the same source the structured booking path reads.
            try:
                from app.services.conversational_memory import (
                    ConversationalMemory as _CM,
                    FACT_NAME as _FACT_NAME,
                )

                name = str(
                    _CM.from_state_blob((state or {}).get("memory") or {}).get(_FACT_NAME) or ""
                ).strip() or None
            except Exception:
                name = None
        status = (
            (STATUS_VOCABULARIES.get("real_estate", {}).get("tickets") or {}).get("initial")
            or "open"
        )
        data: dict[str, Any] = {
            "source": "voice_inbound",
            "auto_created_from_call": True,
            "request_type": "site_visit",
            "issue_type": "site_visit",
            "agent_mode_final": "site_visit",
            "call_id": call_id,
        }
        if name:
            data["name"] = name
        if ani:
            data["phone"] = ani
        # Deterministic note up front — guarantees the ticket always carries a
        # readable note (with the visit date/time for RE_agent_scheduler) even
        # if the post-call condenser returns None.
        data["handoff_note"] = NokvoOneVoicePipeline._deterministic_call_note(
            kind="site_visit", name=name, ani=ani, memory=memory, history=history or [],
        )
        data["handoff_note_generated_at"] = datetime.now(timezone.utc).isoformat()
        data["handoff_note_source"] = "deterministic"
        record = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            record_type="ticket",
            status=status,
            data=data,
            contact_phone=ani,
        )
        try:
            db.add(record)
            await db.commit()
        except Exception:
            logger.exception("NOKVO-SITE-VISIT: failed to persist inbound site visit")
            try:
                await db.rollback()
            except Exception:
                pass
            return None
        # auto_lead_created=True keeps the function idempotent and stops a
        # duplicate lead; auto_site_visit_id is what the post-call condenser
        # loop attaches the call note (with the clarified date/time) to.
        await AgentSessionStore.merge_state(
            tenant_res,
            call_id,
            {
                "auto_lead_created": True,
                "auto_site_visit_created": True,
                "auto_site_visit_id": str(record.id),
                "agent_mode_final": "site_visit",
            },
        )
        return {
            "tool": "site_visit_create_minimal",
            "arguments": data,
            "result": {"ok": True, "id": str(record.id)},
        }

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
        # Snapshot the FK primitive (see _maybe_execute_turn_policy_action) so a
        # later commit/rollback can't force a sync ORM reload → MissingGreenlet.
        org_id = getattr(tenant_res, "organization_id")
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
        # Lead overhaul: ANY inbound real-estate call that didn't book a site
        # visit becomes a lead (ANI + call summary + name-if-known). We no longer
        # require a positive "interest" signal — the caller engaging at all is
        # enough. The ONE hard exclusion is an explicit opt-out (wrong number /
        # do-not-call / not interested): turning that into a follow-up-eligible
        # lead would be a DND/TRAI violation.
        if NokvoOneVoicePipeline._real_estate_opt_out(memory=memory, history=history):
            return None
        # Outbound keeps its engagement gate (don't lead-ify a call where only
        # the opener ran — the outbound outcome classifier owns those). Inbound
        # always proceeds.
        if call_surface == "voice_outbound" and not NokvoOneVoicePipeline._real_estate_interest_signal(
            memory=memory,
            history=history,
            call_surface=call_surface,
            outbound_context=outbound_context,
        ):
            return None

        # End-of-call SMS push (inbound real-estate). Replaces the old in-call
        # lead/site-visit interrogation: text the project brochure + location links
        # to the caller's own number (the ANI we already have). Placed after the
        # opt-out gate so we never message someone who opted out; bounded +
        # best-effort so a slow/failed send never delays or breaks the lead
        # creation below. Idempotent via the sms_sent state flag.
        if call_surface == "voice_inbound":
            try:
                await asyncio.wait_for(
                    NokvoOneVoicePipeline._send_brochure_and_location_sms(
                        db, org_id, tenant_res, call_id, state
                    ),
                    timeout=25,
                )
            except Exception:
                logger.debug(
                    "NOKVO-SMS: end-of-call brochure/location send failed", exc_info=True
                )

        catalog = resolve_index(organization.industry, overrides, custom_tabs)

        if call_surface == "voice_inbound":
            # INBOUND: a site visit is created the moment the caller agrees to
            # come — no field interrogation. The record is just ANI + name (if
            # captured); the date/time the agent clarified rides in the post-call
            # note (condenser → data.handoff_note on auto_site_visit_id). We do
            # NOT run the structured date/time path for inbound, so a clarified
            # date/time never gets persisted as fields.
            from app.services.tool_flow_policy import caller_agreed_to_site_visit

            if caller_agreed_to_site_visit(history):
                sv = await NokvoOneVoicePipeline._create_inbound_site_visit(
                    db, org_id, tenant_res, call_id, state=state, memory=memory, history=history,
                )
                if sv is not None:
                    return sv
            # No visit agreement → capture as a lead below.
        else:
            # OUTBOUND: a firm booking (date + time + name + phone) the
            # deterministic flow didn't capture must still file as a SITE VISIT,
            # not a lead. Outbound keeps the structured visit_at it needs to
            # schedule / assign the visit.
            site_visit_args = NokvoOneVoicePipeline._site_visit_args_from_call_state(
                state=state,
                organization=organization,
                overrides=overrides,
                custom_tabs=custom_tabs,
                memory=memory,
                campaign_context=campaign_context,
            )
            sv_tool = catalog.get("qualify_lead_and_schedule_visit") if site_visit_args else None
            if site_visit_args and sv_tool is not None:
                sv_result = None
                try:
                    sv_result = await PredefinedToolsService.execute(
                        db,
                        org_id,
                        None,
                        sv_tool,
                        site_visit_args,
                        session_id=f"{call_id}:auto_real_estate_site_visit",
                    )
                    await db.commit()
                except Exception:
                    if db is not None:
                        try:
                            await db.rollback()
                        except Exception:
                            pass
                    sv_result = None
                if sv_result and sv_result.get("ok"):
                    await AgentSessionStore.merge_state(
                        tenant_res,
                        call_id,
                        {
                            "auto_lead_created": True,
                            "auto_site_visit_created": True,
                            "auto_site_visit_id": sv_result.get("ticket_id") or sv_result.get("id"),
                            # FSM terminal mode marker. Booking landed — call
                            # ended in site_visit, not inbound_lead.
                            "agent_mode_final": "site_visit",
                        },
                    )
                    return {
                        "tool": "qualify_lead_and_schedule_visit",
                        "arguments": site_visit_args,
                        "result": sv_result,
                    }
                # Site-visit creation unavailable or failed — fall through to lead
                # so the prospect is still captured.

        args = NokvoOneVoicePipeline._lead_args_from_call_memory(
            memory=memory,
            campaign_context=campaign_context,
            outbound_context=outbound_context,
        )
        # Phone-less inbound lead: caller showed interest but hung up before
        # giving a number. By spec they still belong in the Leads → Uncategorized
        # tab so the operator can see the engagement. The ``leads_create`` tool
        # requires phone, so we write a NokvoOneToolRecord directly with whatever
        # facts we did capture.
        if not args.get("phone") and call_surface == "voice_inbound":
            from app.models.nokvo_one_tool_record import NokvoOneToolRecord

            # A lead is intentionally minimal: name + phone + the post-call
            # "call notes" (data.handoff_note, written by the condenser at end of
            # call). We deliberately do NOT persist structured facts (budget,
            # purpose, timeline, objection, property type, location) — the notes
            # carry that context in prose. Only name + routing markers here.
            direct_data: dict[str, Any] = {
                "source": "voice_inbound",
                "auto_created_from_call": True,
                "uncategorized": True,
                "agent_mode_final": "inbound_lead",
                "no_phone": True,
                "call_id": call_id,
                "name": str(args.get("name") or "Property inquiry"),
            }
            # Deterministic note up front so the lead is never noteless if the
            # post-call condenser fails (it overwrites this on success).
            direct_data["handoff_note"] = NokvoOneVoicePipeline._deterministic_call_note(
                kind="lead", name=args.get("name"), ani=None, memory=memory, history=history,
            )
            direct_data["handoff_note_generated_at"] = datetime.now(timezone.utc).isoformat()
            direct_data["handoff_note_source"] = "deterministic"
            direct_data = {k: v for k, v in direct_data.items() if v not in (None, "")}
            record = NokvoOneToolRecord(
                id=uuid.uuid4(),
                organization_id=org_id,
                record_type="lead",
                status="new",
                data=direct_data,
            )
            try:
                db.add(record)
                await db.commit()
                await AgentSessionStore.merge_state(
                    tenant_res,
                    call_id,
                    {
                        "auto_lead_created": True,
                        "auto_lead_id": str(record.id),
                        "agent_mode_final": "inbound_lead",
                    },
                )
                return {
                    "tool": "leads_create_phoneless",
                    "arguments": direct_data,
                    "result": {"ok": True, "id": str(record.id)},
                }
            except Exception:
                logger.exception(
                    "NOKVO-INBOUND-LEAD: failed to persist phoneless uncategorized lead"
                )
                try:
                    await db.rollback()
                except Exception:
                    pass
                return None
        if not args.get("phone"):
            return None
        tool = catalog.get("leads_create")
        if tool is None:
            return None
        result = await PredefinedToolsService.execute(
            db,
            org_id,
            None,
            tool,
            args,
            session_id=f"{call_id}:auto_real_estate_lead",
        )
        await db.commit()
        lead_id = result.get("id") or result.get("lead_id")

        # Inbound real-estate FSM terminal state. Caller showed interest
        # (asked questions, mentioned BHK/budget/location, maybe even
        # started a booking but didn't confirm) and hung up. By spec, the
        # auto-created lead lands in the Leads page's Uncategorized tab
        # (data.uncategorized=true is what the frontend filters on).
        #
        # We DO NOT mark outbound auto-leads as uncategorized — those have
        # their own outcome classifier that decides interested vs
        # partial vs not_interested for tab routing.
        is_inbound = (call_surface == "voice_inbound")
        # A lead is name + phone + post-call notes only. We keep routing markers
        # (source / uncategorized / agent_mode_final / campaign linkage) but
        # deliberately drop the structured facts (budget, purpose, timeline,
        # objection, project, partial visit slots) — that context now lives in
        # the prose "call notes" the condenser writes after the call ends.
        metadata = {
            "source": call_surface or "voice_call",
            "auto_created_from_call": True,
            "campaign_id": (campaign_context or {}).get("campaign_id"),
            # FSM terminal mode marker. The frontend Uncategorized tab
            # filters on ``data.uncategorized === true``; setting it here
            # is what routes the row off the campaign tab and into the
            # uncategorized bucket.
            "uncategorized": True if is_inbound else None,
            "agent_mode_final": "inbound_lead" if is_inbound else None,
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
            {
                "auto_lead_created": True,
                "auto_lead_id": lead_id,
                "agent_mode_final": "inbound_lead" if is_inbound else "query",
            },
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
        # Snapshot the FK primitive (see _maybe_execute_turn_policy_action) so the
        # fresh-session retry below can't force a sync ORM reload → MissingGreenlet.
        org_id = getattr(tenant_res, "organization_id")
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
            # Resolve the flow's slots so we can (a) find date/time/name/phone/
            # project slots by KIND (slot keys equal the admin's Site Visit
            # Field keys, which are arbitrary), and (b) store each captured
            # value back under its configured field key (``source_field``) so
            # the Site Visits tab renders the admin's Site Visit Fields.
            from app.services.tool_flow_questions import build_tool_flow_questions

            sv_bundle = build_tool_flow_questions(
                getattr(organization, "industry", None), overrides, custom_tabs
            )
            flow_slots = (
                ((sv_bundle.get("flows") or {}).get("real_estate_site_visit") or {}).get("slots") or []
            )

            def _slot_keys(kind: str) -> list[str]:
                return [str(s.get("key")) for s in flow_slots if s.get("kind") == kind]

            date_keys = _slot_keys("date") or ["visit_date"]
            time_keys = _slot_keys("time") or ["visit_time"]
            date_raw = next((raw_args.get(k) for k in date_keys if raw_args.get(k)), None)
            time_raw = next((raw_args.get(k) for k in time_keys if raw_args.get(k)), None)
            try:
                visit_date = NokvoOneVoicePipeline._parse_appointment_date(date_raw)
                visit_time = NokvoOneVoicePipeline._parse_appointment_time(time_raw)
            except _AppointmentToolInputError as exc:
                flow_state = dict(((tool_flow.get("state_patch") or {}).get("tool_flow") or {}))
                flow_state["active"] = True
                flow_state["completed"] = False
                flow_state["pending_slot"] = (
                    date_keys[0] if exc.slot == "preferred_date" else time_keys[0]
                )
                return {
                    "answer": exc.answer,
                    "state_patch": {"tool_flow": flow_state},
                    "state_slot": flow_state["pending_slot"],
                    "route_reason": "tool flow needs exact scheduling detail",
                    "tool_calls": [],
                }
            visit_at = datetime.combine(visit_date, visit_time, tzinfo=_APPOINTMENT_LOCAL_TZ).astimezone(timezone.utc).isoformat()

            # Field-keyed site-visit data for the Site Visits tab.
            record_data: dict[str, Any] = {}
            for slot in flow_slots:
                skey = str(slot.get("key") or "")
                fkey = str(slot.get("source_field") or skey)
                value = raw_args.get(skey)
                if value in (None, ""):
                    continue
                kind = slot.get("kind")
                if kind == "date":
                    record_data[fkey] = visit_date.isoformat()
                elif kind == "time":
                    record_data[fkey] = visit_time.strftime("%I:%M %p").lstrip("0")
                else:
                    record_data[fkey] = value

            name_val = next((raw_args.get(k) for k in _slot_keys("name") if raw_args.get(k)), None) or raw_args.get("name")
            phone_val = next((raw_args.get(k) for k in _slot_keys("phone") if raw_args.get(k)), None) or raw_args.get("phone")
            project_val = next((raw_args.get(k) for k in _slot_keys("project") if raw_args.get(k)), None) or raw_args.get("project_name")

            args = {
                "name": name_val,
                "phone": phone_val,
                "visit_at": visit_at,
                "record_data": record_data,
            }
            if project_val not in (None, ""):
                args["project_name"] = project_val
            if raw_args.get("project_id") not in (None, ""):
                args["project_id"] = raw_args["project_id"]
        else:
            args = {k: v for k, v in raw_args.items() if v not in (None, "")}
        # Same retry shape as the clinic appointment path — reads from spec.
        from app.services.agent_spec import RETRY_POLICY
        from app.db.session import AsyncSessionLocal

        result = None
        last_exc: Exception | None = None
        max_inline_attempts = 1 + RETRY_POLICY.inline_retries
        for attempt in range(max_inline_attempts):
            # First attempt uses the shared call session (matches the
            # historical behaviour the tests cover). Retries fall back to
            # a fresh AsyncSession because the long-lived WS-bound session
            # can sit in a state where ``await db.commit()`` raises
            # ``greenlet_spawn has not been called`` — a one-shot session
            # sidesteps that entire class of session-corruption issues.
            use_fresh_session = attempt > 0
            try:
                if use_fresh_session:
                    async with AsyncSessionLocal() as tool_db:
                        result = await PredefinedToolsService.execute(
                            tool_db,
                            org_id,
                            None,
                            tool,
                            args,
                            session_id=call_id,
                        )
                        await tool_db.commit()
                else:
                    result = await PredefinedToolsService.execute(
                        db,
                        org_id,
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
                logger.warning(
                    "NOKVO-TOOL-FLOW: %s failed (attempt %s/%s, fresh_session=%s) args=%s: %r",
                    tool.key,
                    attempt + 1,
                    max_inline_attempts,
                    use_fresh_session,
                    {k: v for k, v in args.items() if k != "record_data"},
                    exc,
                    exc_info=True,
                )
                # Only roll back the shared session — the fresh session's
                # ``async with`` block already rolls itself back on
                # exception.
                if not use_fresh_session and db is not None:
                    try:
                        await db.rollback()
                    except Exception:
                        logger.exception(
                            "NOKVO-TOOL-FLOW: rollback after %s failure crashed", tool.key
                        )
                if attempt < max_inline_attempts - 1:
                    await asyncio.sleep(RETRY_POLICY.inline_delay_seconds)
        if result is None:
            try:
                from app.services.tool_retry_service import ToolRetryService

                await ToolRetryService.enqueue(
                    db,
                    organization_id=org_id,
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

        # Record routing. A completed site-visit booking is tab-defining: it
        # always belongs in the Site Visits (tickets) tab regardless of who
        # placed the call (force_ticket below). Other macros fall back to the
        # call-direction heuristic — inbound → tickets, outbound → leads. The
        # macro defaults to creating leads, so a rewrite only happens when the
        # destination is the tickets tab.
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
                        industry=organization.industry,
                        force_ticket=(flow_key == "real_estate_site_visit"),
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
        projects_block, active_projects = await NokvoOneVoicePipeline._projects_block_for_bundle(db, bundle)
        services_block = await NokvoOneVoicePipeline._services_block_for_bundle(db, bundle)
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

        project_names_for_prompt = [p.name for p in active_projects if p.name]
        field_questions_prompt = NokvoOneVoicePipeline._field_questions_prompt_for_bundle(
            bundle, language=language, project_names=project_names_for_prompt
        )
        memory_block_v2 = ""
        strategy_block_v2 = ""
        if conversational_memory is not None:
            try:
                memory_block_v2 = conversational_memory.compose_prompt_block(
                    language=language,
                    business_type=bundle.organization_industry,
                )
            except Exception:
                memory_block_v2 = ""
            try:
                from app.services.conversation_strategy import compose_strategy_block

                # A record-capture flow (site visit / lead) owns field collection
                # via the FIELD-COLLECTION SCRIPT (operator-configured fields).
                # Tell the strategy layer to suppress its hardcoded "ask exactly
                # X" Next-Best-Action directive so it doesn't compete with — and
                # override — the configured schema.
                _tf_for_strategy = (turn_cache.get("state") or {}).get("tool_flow") or {}
                _capture_flow_active = bool(
                    _tf_for_strategy.get("active")
                    and not _tf_for_strategy.get("completed")
                    and not _tf_for_strategy.get("deferred_for_kb")
                    and str(_tf_for_strategy.get("flow_key") or "")
                    in ("real_estate_site_visit", "leads_create")
                )
                strategy_block_v2 = compose_strategy_block(
                    conversational_memory,
                    business_type=bundle.organization_industry,
                    is_outbound=outbound_context is not None,
                    language=language,
                    focus_project=NokvoOneVoicePipeline._focus_project_summary(
                        active_projects, conversational_memory
                    ),
                    company_name=company_name,
                    capture_flow_active=_capture_flow_active,
                    # Cold-open greeting+discovery agenda only on the genuine first
                    # turn (no prior agent reply) — else it re-greets every turn.
                    is_first_turn=not any((t or {}).get("role") == "assistant" for t in (history or [])),
                )
            except Exception:
                strategy_block_v2 = ""
        # Live tool_flow snapshot — when an inbound-style booking flow is
        # active during an outbound call, surface its slot state so the LLM
        # is forced to drive the next slot instead of free-form chatting.
        # For real-estate INBOUND, we also surface it (FSM site_visit mode).
        from app.services.real_estate_agent_fsm import (
            current_mode as _fsm_current_mode,
            enabled_for_business_type as _fsm_enabled,
            mode_block_for_prompt as _fsm_mode_block,
        )
        from app.services.agent_outbound_context import render_booking_flow_state

        tf_state = dict((turn_cache.get("state") or {}).get("tool_flow") or {})
        _fsm_active_inbound = (
            outbound_context is None
            and bundle is not None
            and _fsm_enabled(bundle.organization_industry)
        )
        tf_bundle: dict[str, Any] | None = None
        if bundle is not None and tf_state.get("active") and (
            outbound_context is not None or _fsm_active_inbound
        ):
            try:
                tf_bundle = build_tool_flow_questions(
                    bundle.organization_industry,
                    bundle.overrides,
                    bundle.custom_tabs,
                )
            except Exception:
                tf_bundle = None

        # Compose the inbound FSM mode block. Empty when org isn't real-estate
        # so other industries keep their existing behaviour unchanged.
        agent_mode_block_inbound: str | None = None
        if _fsm_active_inbound:
            session_state = turn_cache.get("state") or {}
            # Brochure-on-WhatsApp request → stay in whatsapp_mode across the short
            # exchange (sticky over recent turns) so a follow-up "yeah" / number
            # readout doesn't drop back into lead-collection.
            from app.services.tool_flow_policy import brochure_intent_active as _brochure_active
            if _brochure_active(user_text, turn_cache.get("history")):
                _tf_wa = dict(session_state.get("tool_flow") or {})
                _tf_wa["whatsapp_intent"] = {"kind": "brochure"}
                session_state = {**session_state, "tool_flow": _tf_wa}
            current = _fsm_current_mode(session_state, memory=conversational_memory)
            pending_label: str | None = None
            pending_question: str | None = None
            # Both active capture modes drive a flow's slots; pick the flow that
            # matches the mode so the prompt names the next pending field.
            _mode_flow_key = {
                "site_visit": "real_estate_site_visit",
                "lead_capture": "leads_create",
            }.get(current)
            if _mode_flow_key and tf_bundle is not None:
                flow_def = ((tf_bundle.get("flows") or {}).get(_mode_flow_key) or {})
                pending_slot_key = str(tf_state.get("pending_slot") or "")
                for slot in (flow_def.get("slots") or []):
                    if not isinstance(slot, dict):
                        continue
                    skey = str(slot.get("key") or "")
                    if pending_slot_key and skey != pending_slot_key:
                        continue
                    if not pending_slot_key and (tf_state.get("collected") or {}).get(skey):
                        continue
                    pending_label = str(slot.get("label") or skey)
                    questions = slot.get("questions") or {}
                    pending_question = str(
                        questions.get(language) or questions.get("en") or ""
                    )
                    break
            blocks: list[str] = [
                _fsm_mode_block(
                    current,
                    pending_slot_label=pending_label,
                    pending_slot_question=pending_question,
                    memory=conversational_memory,
                )
            ]
            if _mode_flow_key:
                booking_block = render_booking_flow_state(
                    tf_state, tf_bundle, language=language
                )
                if booking_block:
                    blocks.append(booking_block)
            # In whatsapp_mode, surface the caller's own number (ANI) so the agent
            # passes it straight to the brochure tool instead of asking for it.
            if current == "whatsapp":
                _cp = str((session_state.get("caller_phone") or "")).strip()
                if _cp:
                    blocks.append(
                        "# CALLER'S WHATSAPP NUMBER (already known — do not ask)\n"
                        f"Send the brochure to {_cp} — the number they're calling from. "
                        "Pass this exact number to the brochure tool."
                    )
            agent_mode_block_inbound = "\n\n".join(b for b in blocks if b)

        # Clinic mode block. Clinics use the voice_turn_policy appointment FSM
        # (not tool_flow), so derive the clinic mode from appointment state +
        # the latest utterance (triage detection). Persona/guardrail only —
        # complementary to the slot engine.
        if agent_mode_block_inbound is None and outbound_context is None and bundle is not None:
            try:
                from app.services.clinic_agent_fsm import (
                    enabled_for_business_type as _clinic_enabled,
                    current_mode as _clinic_mode,
                    mode_block_for_prompt as _clinic_block,
                )

                if _clinic_enabled(bundle.organization_industry):
                    _c_state = turn_cache.get("state") or {}
                    _c_appt = _c_state.get("appointment") or {}
                    _c_pending = str(_c_appt.get("pending_slot") or "").replace("_", " ").strip() or None
                    agent_mode_block_inbound = _clinic_block(
                        _clinic_mode(_c_state, latest_user_text=user_text),
                        pending_slot_label=_c_pending,
                    )
            except Exception:
                pass

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
            conversation_strategy_block=strategy_block_v2,
            field_questions_prompt=field_questions_prompt,
            projects_block=projects_block,
            services_block=services_block,
            tool_flow_state=tf_state if outbound_context is not None else None,
            tool_flow_bundle=tf_bundle,
            turn_index=(len(history) // 2) + 1 if outbound_context is not None else None,
            agent_mode_block=agent_mode_block_inbound,
            conversational_memory=conversational_memory,
            business_type=bundle.organization_industry if bundle is not None else None,
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
                # Drive from actual retrieval result instead of hardcoded True —
                # the single_prompt_rag fallback doesn't always hit qdrant.
                "qdrant_called": bool(chunks),
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
        project_names: list[str] | None = None,
    ) -> str:
        """Build the "use these exact phrasings" prompt block from the
        per-tenant runtime bundle. Empty string when no record-creation
        fields are configured — keeps the prompt lean for inbound calls
        that aren't collecting structured records.

        ``project_names`` (real-estate only) is the live DB list and is
        substituted into the Project slot's question so the LLM can't fall
        back to a project list baked into the admin's single prompt.
        """
        try:
            catalog = build_tool_flow_questions(
                bundle.organization_industry,
                bundle.overrides,
                bundle.custom_tabs,
            )
        except Exception:
            return ""
        return format_field_questions_prompt(
            catalog, language=language, project_names=project_names
        )

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
        projects_block, active_projects = await NokvoOneVoicePipeline._projects_block_for_bundle(db, bundle)
        services_block = await NokvoOneVoicePipeline._services_block_for_bundle(db, bundle)
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

        project_names_for_prompt = [p.name for p in active_projects if p.name]
        field_questions_prompt = NokvoOneVoicePipeline._field_questions_prompt_for_bundle(
            bundle, language=language, project_names=project_names_for_prompt
        )
        memory_block = ""
        strategy_block = ""
        if conversational_memory is not None:
            try:
                memory_block = conversational_memory.compose_prompt_block(
                    language=language,
                    business_type=bundle.organization_industry,
                )
            except Exception:
                memory_block = ""
            try:
                from app.services.conversation_strategy import compose_strategy_block

                _tf_for_strategy = (turn_cache.get("state") or {}).get("tool_flow") or {}
                _capture_flow_active = bool(
                    _tf_for_strategy.get("active")
                    and not _tf_for_strategy.get("completed")
                    and not _tf_for_strategy.get("deferred_for_kb")
                    and str(_tf_for_strategy.get("flow_key") or "")
                    in ("real_estate_site_visit", "leads_create")
                )
                strategy_block = compose_strategy_block(
                    conversational_memory,
                    business_type=bundle.organization_industry,
                    is_outbound=outbound_context is not None,
                    language=language,
                    focus_project=NokvoOneVoicePipeline._focus_project_summary(
                        active_projects, conversational_memory
                    ),
                    company_name=company_name,
                    capture_flow_active=_capture_flow_active,
                    is_first_turn=not any((t or {}).get("role") == "assistant" for t in (history or [])),
                )
            except Exception:
                strategy_block = ""
        # Outbound's factual scope is the campaign brief alone. Suppress the
        # inbound real-estate project inventory block here so the campaign
        # doc_text + agent_prompt are the only product/pricing/availability
        # source the LLM sees. Inbound paths keep their inventory pin.
        _projects_block_for_messages = "" if outbound_mode else projects_block
        # Live tool_flow snapshot — surfaced for outbound and for real-estate
        # inbound (FSM site_visit mode).
        from app.services.real_estate_agent_fsm import (
            current_mode as _fsm_current_mode,
            enabled_for_business_type as _fsm_enabled,
            mode_block_for_prompt as _fsm_mode_block,
        )
        from app.services.agent_outbound_context import render_booking_flow_state

        tf_state_for_msg = dict((turn_cache.get("state") or {}).get("tool_flow") or {})
        _fsm_active_inbound_streaming = (
            not outbound_mode
            and bundle is not None
            and _fsm_enabled(bundle.organization_industry)
        )
        tf_bundle_for_msg: dict[str, Any] | None = None
        if bundle is not None and tf_state_for_msg.get("active") and (
            outbound_mode or _fsm_active_inbound_streaming
        ):
            try:
                tf_bundle_for_msg = build_tool_flow_questions(
                    bundle.organization_industry,
                    bundle.overrides,
                    bundle.custom_tabs,
                )
            except Exception:
                tf_bundle_for_msg = None

        agent_mode_block_inbound_streaming: str | None = None
        if _fsm_active_inbound_streaming:
            _ss_stream = turn_cache.get("state") or {}
            from app.services.tool_flow_policy import brochure_intent_active as _brochure_active
            if _brochure_active(user_text, turn_cache.get("history")):
                _tf_wa = dict(_ss_stream.get("tool_flow") or {})
                _tf_wa["whatsapp_intent"] = {"kind": "brochure"}
                _ss_stream = {**_ss_stream, "tool_flow": _tf_wa}
            current = _fsm_current_mode(_ss_stream, memory=conversational_memory)
            pending_label: str | None = None
            pending_question: str | None = None
            _mode_flow_key = {
                "site_visit": "real_estate_site_visit",
                "lead_capture": "leads_create",
            }.get(current)
            if _mode_flow_key and tf_bundle_for_msg is not None:
                flow_def = (
                    (tf_bundle_for_msg.get("flows") or {}).get(_mode_flow_key) or {}
                )
                pending_slot_key = str(tf_state_for_msg.get("pending_slot") or "")
                for slot in (flow_def.get("slots") or []):
                    if not isinstance(slot, dict):
                        continue
                    skey = str(slot.get("key") or "")
                    if pending_slot_key and skey != pending_slot_key:
                        continue
                    if not pending_slot_key and (tf_state_for_msg.get("collected") or {}).get(skey):
                        continue
                    pending_label = str(slot.get("label") or skey)
                    questions = slot.get("questions") or {}
                    pending_question = str(
                        questions.get(language) or questions.get("en") or ""
                    )
                    break
            blocks: list[str] = [
                _fsm_mode_block(
                    current,
                    pending_slot_label=pending_label,
                    pending_slot_question=pending_question,
                    memory=conversational_memory,
                )
            ]
            if _mode_flow_key:
                booking_block = render_booking_flow_state(
                    tf_state_for_msg, tf_bundle_for_msg, language=language
                )
                if booking_block:
                    blocks.append(booking_block)
            if current == "whatsapp":
                _cp = str((_ss_stream.get("caller_phone") or "")).strip()
                if _cp:
                    blocks.append(
                        "# CALLER'S WHATSAPP NUMBER (already known — do not ask)\n"
                        f"Send the brochure to {_cp} — the number they're calling from. "
                        "Pass this exact number to the brochure tool."
                    )
            agent_mode_block_inbound_streaming = "\n\n".join(b for b in blocks if b)

        # Clinic mode block (streaming path) — mirror the non-streaming branch.
        if agent_mode_block_inbound_streaming is None and not outbound_mode and bundle is not None:
            try:
                from app.services.clinic_agent_fsm import (
                    enabled_for_business_type as _clinic_enabled,
                    current_mode as _clinic_mode,
                    mode_block_for_prompt as _clinic_block,
                )

                if _clinic_enabled(bundle.organization_industry):
                    _c_state = turn_cache.get("state") or {}
                    _c_appt = _c_state.get("appointment") or {}
                    _c_pending = str(_c_appt.get("pending_slot") or "").replace("_", " ").strip() or None
                    agent_mode_block_inbound_streaming = _clinic_block(
                        _clinic_mode(_c_state, latest_user_text=query),
                        pending_slot_label=_c_pending,
                    )
            except Exception:
                pass

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
            conversation_strategy_block=strategy_block,
            field_questions_prompt=field_questions_prompt,
            projects_block=_projects_block_for_messages,
            services_block=("" if outbound_mode else services_block),
            tool_flow_state=tf_state_for_msg if outbound_mode else None,
            tool_flow_bundle=tf_bundle_for_msg,
            turn_index=(len(history) // 2) + 1 if outbound_mode else None,
            agent_mode_block=agent_mode_block_inbound_streaming,
            conversational_memory=conversational_memory,
            business_type=bundle.organization_industry if bundle is not None else None,
        )
        # Prosody-aware streaming: the LLM is asked to wrap each sentence in a
        # [tone]…[/tone] tag. The parser strips the tags and emits one chunk
        # per sentence-or-tone-boundary so we can synthesize each with
        # matching pace/pitch/loudness.
        answer_parts: list[str] = []
        rate_limited = False
        # Outbound: hard token cap so the model physically cannot generate a
        # paragraph reply. Hindi / Telugu / Tamil tokenise to 2-3× more tokens
        # per equivalent sentence than English, so the 48-token English cap
        # would cut them mid-clause. Lift the cap proportionally for those
        # languages so the 1-2 sentence target still ends cleanly. Inbound
        # keeps the default 180.
        _lang_code = (language or "en").split("-")[0].lower()[:2]
        if outbound_mode:
            _stream_max_tokens = 96 if _lang_code in {"hi", "te", "ta", "bn", "kn", "mr"} else 48
        else:
            _stream_max_tokens = 180
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
                # Drive from actual retrieval result instead of hardcoded True —
                # the single_prompt_rag fallback doesn't always hit qdrant.
                "qdrant_called": bool(chunks),
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
