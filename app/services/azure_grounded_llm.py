"""Azure OpenAI LLM client for the shared gpt-5-mini pool.

Extracted from nokvo_one_voice_pipeline.py — this client is consumed by a
dozen non-voice services (summaries, condensers, Nova, classifiers), so it is
a top-level service module, not a voice-pipeline building block. The pipeline
module re-exports every name here, so existing imports keep working; new code
should import from this module directly.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator
from urllib import parse as urllib_parse

import httpx

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.azure_keyvault_service import AzureKeyVaultService

logger = logging.getLogger(__name__)


def _meter_call_llm(usage: dict | None) -> None:
    """Add one LLM response's token usage to the in-flight call's COGS sink.

    Best-effort + null-safe: no-op outside a voice session (the contextvar is
    unset) or when the provider didn't return a usage block. ``cached_tokens``
    is the discounted-rate subset of ``prompt_tokens`` (Azure prompt cache).
    Accepts BOTH usage shapes: chat-completions (``prompt_tokens`` /
    ``completion_tokens`` / ``prompt_tokens_details``) and the Responses API
    (``input_tokens`` / ``output_tokens`` / ``input_tokens_details``) — a pool
    member configured with a /responses endpoint would otherwise meter zero.
    """
    if not usage:
        return
    try:
        from app.services.call_usage import current_call_usage

        sink = current_call_usage()
        if sink is not None:
            details = (
                usage.get("prompt_tokens_details")
                or usage.get("input_tokens_details")
                or {}
            )
            sink.add_llm(
                prompt_tokens=usage.get("prompt_tokens") or usage.get("input_tokens"),
                completion_tokens=usage.get("completion_tokens") or usage.get("output_tokens"),
                cached_tokens=details.get("cached_tokens"),
            )
    except Exception:
        pass


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
            or "gpt-5-mini"
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
        # Shared per-deployment param profile (llm_pool): gpt-5/o chat/completions
        # members need max_completion_tokens + reasoning_effort and reject
        # temperature; classic members (gpt-4.1-nano summarizer) keep temperature.
        # Same negotiation cache LLMPoolClient.chat learns from 400s.
        from app.services.llm_pool import build_chat_body

        body = build_chat_body(member, messages, max_tokens=max_tokens, temperature=0.2, stream=stream)
        if stream:
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
            _meter_call_llm(usage)
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
            _meter_call_llm(usage)
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
            # COGS: nano completions (intent classifier, in-call summary, …)
            # were the ONE unmetered LLM path — every other entry point
            # (complete / complete_global / stream) already meters. No-op
            # outside a call (contextvar unset), so campaign-creation /
            # scheduler uses stay free of side effects.
            _meter_call_llm(payload.get("usage"))
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
                                _meter_call_llm(_usage)
                            elif event.get("type") == "response.completed":
                                # Responses-API streams carry the final usage on
                                # the completed event, nested under "response" —
                                # a /responses pool member would otherwise meter
                                # zero for every streamed turn.
                                _usage = (event.get("response") or {}).get("usage")
                                _meter_call_llm(_usage)
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

