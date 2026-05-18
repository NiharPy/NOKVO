"""Agent Voice Stream Service — Production RAG Voice Agent.

Pipeline:
    STT stream (interim tokens)
        → SpeculativeCache (intent + retrieval precomputed during transcription)
    Endpoint detected
        → _resolve_context() (spec-cache / topic-continuity / parallel live retrieval)
        → LLM stream with filler dead-air guard
        → Sentence-gated WarmTTS dispatch (first sentence immediately, remainder consecutively)

Latency target: ≤800ms caller-perceived from endpoint detection.

Features:
  - TurnState machine: IDLE → LISTENING → PROCESSING → SPEAKING
  - Barge-in: user speech during SPEAKING cancels TTS and restarts LISTENING
  - Filler audio: "Sure, one moment." plays on dedicated warm TTS if LLM takes > FILLER_TRIGGER_MS
  - Topic continuity: skips Qdrant retrieval when follow-up overlaps ≥35% with last query
  - Parallel intent + retrieval on cold path (asyncio.gather)
  - Sentence-gated TTS: first sentence dispatched while LLM generates remainder
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from dataclasses import dataclass, field
from enum import Enum
from time import perf_counter
from typing import Any, Callable

from fastapi import WebSocket
from websockets.asyncio.client import connect

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.agent_runtime_service import (
    AgentRuntimeService,
    AzureAgentLLMClient,
)
from app.services.agent_intent_service import (
    classify_intent_fast,
    classify_intent,
    detect_language,
    generate_conversation_reply,
    validate_chunk_relevance,
    normalize_message,
    INTENT_AMBIGUOUS,
)
from app.services.agent_knowledge_service import AgentKnowledgeService


# ---------------------------------------------------------------------------
# Turn State Machine
# ---------------------------------------------------------------------------

class TurnState(Enum):
    IDLE = "idle"
    LISTENING = "listening"
    PROCESSING = "processing"
    SPEAKING = "speaking"


# ---------------------------------------------------------------------------
# Session Context — conversation memory + topic continuity
# ---------------------------------------------------------------------------

@dataclass
class SessionContext:
    history: list[dict] = field(default_factory=list)
    last_chunks: list[dict] = field(default_factory=list)
    last_query: str = ""
    last_answer: str = ""
    last_intent: str = ""
    language: str = "en"

    def add_turn(self, query: str, answer: str, *, intent_type: str = "") -> None:
        self.history.append({"role": "user", "content": query})
        self.history.append({"role": "assistant", "content": answer})
        self.last_answer = answer
        self.last_intent = intent_type
        while len(self.history) > 20:
            self.history.pop(0)

    def update_chunks(self, query: str, chunks: list[dict]) -> None:
        self.last_query = query
        self.last_chunks = list(chunks)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------

_STOPWORDS = {
    "i", "a", "an", "the", "is", "my", "me", "to", "do", "can", "you", "your",
    "how", "what", "for", "about", "please", "get", "help", "need", "want", "just",
}


def _word_overlap(a: str, b: str) -> float:
    def _words(s: str) -> set[str]:
        return {w for w in re.findall(r"[a-z0-9]{3,}", s.lower()) if w not in _STOPWORDS}
    wa, wb = _words(a), _words(b)
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


_SENTENCE_END_RE = re.compile(r"(?<=[.!?।])\s+")
_REPEAT_LAST_RE = re.compile(
    r"\b(repeat|say that again|what did you say|can you repeat|come again|"
    r"phir se|dobara|दोबारा|फिर से|మళ్లీ|திரும்ப|ಮತ್ತೆ|വീണ്ടും)\b",
    re.IGNORECASE,
)


def _first_sentence(buf: str) -> tuple[str, str] | None:
    """Find the first natural sentence boundary in buf.

    Returns (sentence, remainder) or None if no boundary yet.
    Forces a split at AGENT_MAX_FIRST_SENTENCE_CHARS to guarantee TTS dispatch.
    """
    min_chars = 15
    max_chars = settings.AGENT_MAX_FIRST_SENTENCE_CHARS

    for m in _SENTENCE_END_RE.finditer(buf):
        candidate = buf[:m.start() + 1].strip()
        if len(candidate) >= min_chars:
            return candidate, buf[m.end():]

    if len(buf) >= max_chars:
        for sep in (",", ";"):
            idx = buf.rfind(sep, min_chars, max_chars)
            if idx > 0:
                return buf[:idx + 1].strip(), buf[idx + 1:].lstrip()
        idx = buf.rfind(" ", min_chars, max_chars)
        if idx > 0:
            return buf[:idx].strip(), buf[idx + 1:]

    return None


# ---------------------------------------------------------------------------
# Redis Answer Cache — eliminates LLM+Qdrant for repeated questions
# ---------------------------------------------------------------------------

class AgentAnswerCache:
    """Per-session Redis cache keyed by (tenant_id, language, normalized_query).

    Cache hit path: STT final → Redis lookup (~1ms) → answer ready → TTS immediately.
    No Qdrant, no LLM. Cache miss falls through to the normal speculative pipeline.
    TTL = AGENT_ANSWER_CACHE_TTL_SECONDS (default 5 min).
    """

    _DYNAMIC_OR_SENSITIVE_RE = re.compile(
        r"\b(order\s*(id|number|status)?|track|ticket|payment|paid|refund\s+status|"
        r"account|phone|email|address|wallet|coins|otp|password|card|upi|bank|"
        r"delete|cancel\s+my|my\s+order|my\s+account)\b",
        re.IGNORECASE,
    )
    _CACHE_SCHEMA_VERSION = "v2"

    def __init__(self, redis_client: Any, tenant_id: str, policy_version: str = "pv_default") -> None:
        self._redis = redis_client
        self._tenant_id = tenant_id
        self._policy_version = policy_version or "pv_default"

    def _key(self, query: str, language: str) -> str:
        normalized = re.sub(r"[^\w\s]", "", normalize_message(query).lower())
        normalized = re.sub(r"\s+", " ", normalized).strip()
        return f"vcache:{self._CACHE_SCHEMA_VERSION}:{self._tenant_id}:{self._policy_version}:{language}:{normalized}"

    @classmethod
    def cacheable(cls, query: str, answer: str, chunks: list[dict] | None = None) -> bool:
        if not query.strip() or not answer.strip():
            return False
        if cls._DYNAMIC_OR_SENSITIVE_RE.search(query):
            return False
        for chunk in chunks or []:
            metadata = dict(chunk.get("metadata") or {})
            if metadata.get("sensitivity") == "sensitive":
                return False
        return True

    async def get(self, query: str, language: str) -> dict[str, Any] | None:
        if not self._redis:
            return None
        try:
            raw = await self._redis.get(self._key(query, language))
            return json.loads(raw) if raw else None
        except Exception:
            return None

    async def set(self, query: str, language: str, answer: str, chunks: list[dict]) -> None:
        if not self._redis or not answer:
            return
        try:
            ttl = settings.AGENT_ANSWER_CACHE_TTL_SECONDS
            payload = json.dumps({
                "answer": answer,
                "first_sentence": _first_sentence(answer + " ")[0] if _first_sentence(answer + " ") else answer[:120],
                "language": language,
                "policy_version": self._policy_version,
                "intent": "support_policy_question",
                "answer_type": "rag_grounded",
                "cache_scope": "tenant_policy",
                "contains_pii": False,
                "used_tools": [],
                "chunks": [
                    {
                        "text": c.get("text", "")[:200],
                        "score": c.get("score", 0),
                        "chunk_id": c.get("chunk_id"),
                        "metadata": c.get("metadata") or {},
                    }
                    for c in (chunks or [])[:2]
                ],
            })
            await self._redis.setex(self._key(query, language), ttl, payload)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Speculative Pre-Processor
# ---------------------------------------------------------------------------

class SpeculativeCache:
    """Caches speculative intent + retrieval results computed during STT transcription.

    While the user is still speaking:
      1. Fast regex intent classification on each interim token (~0ms)
      2. If intent = SUPPORT: Qdrant retrieval starts in background (~80ms)
      3. If chunks found: LLM streaming starts in background (collects full answer)
    When final transcript arrives: reuse if ≥60% word overlap → skip straight to TTS.
    """

    def __init__(self) -> None:
        self.last_interim: str = ""
        self.speculative_intent: dict[str, Any] | None = None
        self.speculative_chunks: list[dict[str, Any]] | None = None
        self.speculative_citations: list[dict[str, Any]] | None = None
        self.retrieval_task: asyncio.Task | None = None
        self.answer_task: asyncio.Task | None = None
        self.speculative_answer: str | None = None
        self.answer_text: str = ""
        self.intent_text: str = ""
        self.retrieval_text: str = ""
        self._lock = asyncio.Lock()

    async def process_interim(
        self, text: str, tenant_res: TenantResources, *, db=None,
        answer_cache: "AgentAnswerCache | None" = None,
    ) -> None:
        if not text or len(text.strip()) < 4:
            return
        normalized = normalize_message(text)
        if not normalized or normalized == self.last_interim:
            return
        self.last_interim = normalized
        words = normalized.split()
        fast = classify_intent_fast(normalized)
        if fast:
            self.speculative_intent = fast
            self.intent_text = normalized
            if fast.get("shouldRetrieve") and len(words) >= 3:
                await self._start_speculative_retrieval(
                    normalized, tenant_res, db=db, answer_cache=answer_cache
                )

    async def _start_speculative_retrieval(
        self, text: str, tenant_res: TenantResources, *,
        db=None, answer_cache: "AgentAnswerCache | None" = None,
    ) -> None:
        # Skip if retrieval is already running for text that's substantially the same.
        # This prevents stammer/repetition from spawning a flood of Qdrant + LLM calls.
        if self.retrieval_task and not self.retrieval_task.done():
            if self._text_overlap(self.retrieval_text or self.intent_text, text) >= 0.70:
                return
            self.retrieval_task.cancel()

        async def _retrieve() -> None:
            language = detect_language(text)
            try:
                # Run Redis cache check and Qdrant retrieval in true parallel.
                # Cache hit cancels the Qdrant task and skips LLM entirely.
                qdrant_task = asyncio.create_task(
                    AgentKnowledgeService.test_retrieval(
                        tenant_res, text, top_k=settings.AGENT_RETRIEVAL_TOP_K, db=db
                    )
                )
                cache_task = asyncio.create_task(
                    answer_cache.get(text, language)
                ) if answer_cache else None

                if cache_task:
                    cached = await cache_task
                    if cached and cached.get("answer"):
                        qdrant_task.cancel()
                        async with self._lock:
                            self.speculative_answer = cached["answer"]
                            self.answer_text = text
                            self.speculative_chunks = cached.get("chunks") or []
                            self.speculative_citations = []
                            self.retrieval_text = text
                        return

                result = await qdrant_task
                chunks = result.get("chunks") or []
                relevant = validate_chunk_relevance(chunks, text)
                async with self._lock:
                    self.speculative_chunks = relevant
                    self.speculative_citations = [
                        {
                            "document_id": c.get("document_id"),
                            "document_name": c.get("document_name"),
                            "chunk_id": c.get("chunk_id"),
                            "score": c.get("score"),
                        }
                        for c in relevant
                    ]
                    self.retrieval_text = text
                if relevant:
                    await self._start_speculative_answer(text, tenant_res, relevant)
            except Exception:
                pass

        self.retrieval_task = asyncio.create_task(_retrieve())

    async def _start_speculative_answer(
        self, text: str, tenant_res: TenantResources, chunks: list[dict[str, Any]]
    ) -> None:
        # Don't restart if an answer task is already running for text that's ≥70% the
        # same — stammer/repetition would otherwise fire multiple LLM calls and drain
        # the rate-limit quota, causing 429s on the real answer call.
        if self.answer_task and not self.answer_task.done():
            if self._text_overlap(self.answer_text or self.intent_text, text) >= 0.70:
                return
            self.answer_task.cancel()

        async def _answer() -> None:
            language = detect_language(text)
            try:
                tokens: list[str] = []
                async for tok in AzureAgentLLMClient.stream_grounded_answer(
                    tenant_res, text, chunks, response_language=language
                ):
                    tokens.append(tok)
                answer = "".join(tokens).strip()
                if answer:
                    async with self._lock:
                        self.speculative_answer = answer
                        self.answer_text = text
            except Exception:
                pass

        self.answer_task = asyncio.create_task(_answer())

    def _text_overlap(self, a: str, b: str) -> float:
        wa = set(normalize_message(a).lower().split())
        wb = set(normalize_message(b).lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / max(len(wa), len(wb))

    def _compatible(self, speculative_text: str, final_text: str) -> bool:
        if self._text_overlap(speculative_text, final_text) < 0.6:
            return False
        if detect_language(speculative_text) != detect_language(final_text):
            return False
        neg_re = re.compile(r"\b(no|not|never|don't|dont|didn't|didnt|nahi|नहीं|मत)\b", re.IGNORECASE)
        if bool(neg_re.search(speculative_text)) != bool(neg_re.search(final_text)):
            return False
        return True

    async def get_cached_result(self, final_text: str) -> dict[str, Any] | None:
        if self.retrieval_task and not self.retrieval_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.retrieval_task), timeout=0.15)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass
        if self.answer_task and not self.answer_task.done():
            try:
                await asyncio.wait_for(asyncio.shield(self.answer_task), timeout=0.08)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                pass

        async with self._lock:
            if not self.speculative_intent:
                return None
            if not self._compatible(self.intent_text, final_text):
                return None

            result: dict[str, Any] = {
                "intent": self.speculative_intent,
                "intent_text": self.intent_text,
            }
            if self.speculative_chunks is not None:
                if self._compatible(self.retrieval_text, final_text):
                    result["chunks"] = self.speculative_chunks
                    result["citations"] = self.speculative_citations or []
            if self.speculative_answer:
                if self._compatible(self.answer_text, final_text):
                    result["answer"] = self.speculative_answer
            return result

    def reset(self) -> None:
        for task in (self.retrieval_task, self.answer_task):
            if task and not task.done():
                task.cancel()
        self.last_interim = ""
        self.speculative_intent = None
        self.speculative_chunks = None
        self.speculative_citations = None
        self.retrieval_task = None
        self.answer_task = None
        self.speculative_answer = None
        self.answer_text = ""
        self.intent_text = ""
        self.retrieval_text = ""


# ---------------------------------------------------------------------------
# Warm Soniox TTS Stream — session-scoped persistent WebSocket
# ---------------------------------------------------------------------------

class WarmSonioxTTSStream:
    """Keeps a warm Soniox TTS WebSocket open for the session lifetime.

    Each synthesis sends a fresh start message with a new stream_id on the
    existing connection — avoids TLS+handshake overhead (~913ms cold → ~180ms warm).
    """

    def __init__(self, websocket: WebSocket, tenant_res: TenantResources, *, purpose: str = "answer") -> None:
        self.websocket = websocket
        self.tenant_res = tenant_res
        self.provider_status = dict(tenant_res.provider_status or {})
        self.purpose = purpose
        self.tts_ws = None
        self.language = "en"
        self.connected_ms = 0
        self._connect_task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._api_key: str | None = None

    async def warm(self, language: str = "en") -> None:
        language = AgentRuntimeService.normalize_language(language)
        if self._connect_task and not self._connect_task.done():
            if self.language == language:
                await asyncio.shield(self._connect_task)
                return
            await self.close()
        self._connect_task = asyncio.create_task(self._warm_safe(language))
        await asyncio.shield(self._connect_task)

    def is_healthy(self) -> bool:
        if self.tts_ws is None:
            return False
        closed = getattr(self.tts_ws, "closed", None)
        if closed is not None:
            return not bool(closed)
        state = getattr(self.tts_ws, "state", None)
        return str(state).upper() not in {"CLOSED", "CLOSING"}

    async def _warm_safe(self, language: str) -> None:
        try:
            await self.ensure(language)
        except Exception as exc:
            try:
                await self.websocket.send_json({
                    "type": "tts_warm_error",
                    "error_code": "tts_warm_failed",
                    "error_message": f"TTS warm connection failed: {str(exc)[:180]}",
                })
            except Exception:
                pass

    async def ensure(self, language: str = "en"):
        language = AgentRuntimeService.normalize_language(language)
        current_task = asyncio.current_task()
        if self._connect_task and not self._connect_task.done() and self._connect_task is not current_task:
            try:
                await asyncio.shield(self._connect_task)
            except Exception:
                pass
        if self.tts_ws is not None and self.language == language and self.is_healthy():
            return self.tts_ws
        await self.close()
        started = perf_counter()
        api_key = await AgentRuntimeService.soniox_api_key(self.tenant_res, "tts")
        self._api_key = api_key
        endpoint = self.provider_status.get("tts_stream_endpoint") or settings.SONIOX_TTS_STREAM_URL
        tts_ws = await connect(endpoint, max_size=8 * 1024 * 1024)
        await tts_ws.send(json.dumps({
            "api_key": api_key,
            "model": self.provider_status.get("tts_model") or settings.SONIOX_TTS_MODEL,
            "language": language,
            "voice": self.provider_status.get("tts_voice") or settings.SONIOX_TTS_VOICE,
            "audio_format": self.provider_status.get("tts_audio_format") or settings.SONIOX_TTS_AUDIO_FORMAT,
            "sample_rate": self.provider_status.get("tts_sample_rate") or settings.SONIOX_TTS_SAMPLE_RATE,
            "stream_id": f"warm-tts-{uuid.uuid4()}",
        }))
        self.tts_ws = tts_ws
        self.language = language
        self.connected_ms = int((perf_counter() - started) * 1000)
        try:
                await self.websocket.send_json({
                    "type": "tts_warmed",
                    "connected_ms": self.connected_ms,
                    "language": language,
                    "purpose": self.purpose,
                })
        except Exception:
            pass
        return self.tts_ws

    async def stream(self, text: str, *, language: str = "en") -> bool:
        if not text.strip():
            return True
        async with self._lock:
            for attempt in range(2):
                try:
                    return await self._stream_once(text, language=language)
                except asyncio.CancelledError:
                    await self.close()
                    raise
                except Exception:
                    await self.close()
                    if attempt < 1:
                        continue
                    return False
        return False

    async def _stream_once(self, text: str, *, language: str = "en") -> bool:
        language = AgentRuntimeService.normalize_language(language)
        t0 = perf_counter()
        tts_ws = await self.ensure(language)
        stream_id = f"tts-{uuid.uuid4()}"
        api_key = self._api_key or await AgentRuntimeService.soniox_api_key(self.tenant_res, "tts")
        await tts_ws.send(json.dumps({
            "api_key": api_key,
            "model": self.provider_status.get("tts_model") or settings.SONIOX_TTS_MODEL,
            "language": language,
            "voice": self.provider_status.get("tts_voice") or settings.SONIOX_TTS_VOICE,
            "audio_format": self.provider_status.get("tts_audio_format") or settings.SONIOX_TTS_AUDIO_FORMAT,
            "sample_rate": self.provider_status.get("tts_sample_rate") or settings.SONIOX_TTS_SAMPLE_RATE,
            "stream_id": stream_id,
        }))
        await tts_ws.send(json.dumps({"text": text[:5000], "text_end": True, "stream_id": stream_id}))
        request_sent_ms = int((perf_counter() - t0) * 1000)
        try:
            await self.websocket.send_json({
                "type": "tts_started",
                "stream_id": stream_id,
                "connected_ms": 0,
                "request_sent_ms": request_sent_ms,
                "warm_connection": True,
                "warm_connected_ms": self.connected_ms,
                "purpose": self.purpose,
            })
        except Exception:
            pass
        first_audio_sent = False
        while True:
            timeout = (
                settings.TTS_SEGMENT_IDLE_DONE_MS / 1000
                if first_audio_sent
                else settings.TTS_SEGMENT_FIRST_AUDIO_TIMEOUT_MS / 1000
            )
            try:
                raw = await asyncio.wait_for(tts_ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                if first_audio_sent:
                    # Some Soniox streams do not promptly send audio_end/terminated.
                    # Once audio has gone idle, release this segment so the next
                    # answer sentence can start instead of blocking the call for seconds.
                    try:
                        await self.websocket.send_json({
                            "type": "tts_done",
                            "stream_id": stream_id,
                            "warm_connection": True,
                            "purpose": self.purpose,
                            "done_reason": "idle_after_audio",
                        })
                    except Exception:
                        pass
                    return True
                raise RuntimeError("TTS first audio timeout")
            message = json.loads(raw)
            msg_sid = message.get("stream_id")
            if msg_sid and msg_sid != stream_id:
                continue
            if message.get("audio"):
                first_audio_latency_ms = None
                if not first_audio_sent:
                    first_audio_sent = True
                    first_audio_latency_ms = int((perf_counter() - t0) * 1000)
                    try:
                        await self.websocket.send_json({
                            "type": "tts_first_audio",
                            "stream_id": stream_id,
                            "first_audio_latency_ms": first_audio_latency_ms,
                            "connected_ms": 0,
                            "request_sent_ms": request_sent_ms,
                            "warm_connection": True,
                            "purpose": self.purpose,
                        })
                    except Exception:
                        pass
                audio_end = bool(message.get("audio_end"))
                try:
                    await self.websocket.send_json({
                        "type": "tts_audio",
                        "stream_id": stream_id,
                        "audio_base64": message["audio"],
                        "audio_end": audio_end,
                        "first_audio_latency_ms": first_audio_latency_ms,
                        "purpose": self.purpose,
                    })
                except Exception:
                    pass
                if audio_end:
                    # Soniox signals end-of-stream inside the audio frame on a persistent
                    # WebSocket. "terminated" may follow later but we must not wait for it
                    # or the lock stays held and the remainder call blocks for 20+ seconds.
                    try:
                        await self.websocket.send_json({
                            "type": "tts_done",
                            "stream_id": stream_id,
                            "warm_connection": True,
                            "purpose": self.purpose,
                        })
                    except Exception:
                        pass
                    return True
            if message.get("error_code"):
                error_msg = message.get("error_message") or message.get("error_code") or "TTS service error"
                raise RuntimeError(f"Warm TTS stream failed: {error_msg}")
            if message.get("terminated"):
                try:
                    await self.websocket.send_json({
                        "type": "tts_done",
                        "stream_id": stream_id,
                        "warm_connection": True,
                        "purpose": self.purpose,
                    })
                except Exception:
                    pass
                return True

    async def close(self) -> None:
        current_task = asyncio.current_task()
        if self._connect_task and not self._connect_task.done() and self._connect_task is not current_task:
            self._connect_task.cancel()
            self._connect_task = None
        elif self._connect_task is not current_task:
            self._connect_task = None
        if self.tts_ws is not None:
            try:
                await self.tts_ws.close()
            except Exception:
                pass
        self.tts_ws = None


# ---------------------------------------------------------------------------
# Agent Voice Stream Service
# ---------------------------------------------------------------------------

class AgentVoiceStreamService:

    @staticmethod
    def _clean_stt_text(text: str) -> str:
        cleaned = normalize_message(text)
        if not cleaned:
            return ""
        cleaned = re.sub(
            r"(?i)(?:[\s,.;:!?]+(?:end|eos|silence|speech_final|speech final))+$", "", cleaned
        ).strip()
        return cleaned

    @staticmethod
    def _extract_transcript(payload: dict[str, Any]) -> tuple[str, bool]:
        tokens = payload.get("tokens") or []
        if isinstance(tokens, list) and tokens:
            text = "".join(str(t.get("text") or "") for t in tokens)
            flags = [bool(t.get("is_final")) for t in tokens if isinstance(t, dict) and "is_final" in t]
            is_final = bool(
                payload.get("finished") or payload.get("speech_final") or (flags and all(flags))
            )
            return AgentVoiceStreamService._clean_stt_text(text), is_final
        for key in ("text", "transcript"):
            if isinstance(payload.get(key), str):
                return (
                    AgentVoiceStreamService._clean_stt_text(payload[key]),
                    bool(payload.get("is_final") or payload.get("final")),
                )
        return "", bool(payload.get("finished") or payload.get("speech_final"))

    @staticmethod
    async def stream_tts(
        websocket: WebSocket, tenant_res: TenantResources, text: str, *, language: str = "en"
    ) -> None:
        """Cold TTS fallback — opens a fresh WebSocket per call (~913ms first audio)."""
        if not text.strip():
            return
        t0 = perf_counter()
        try:
            api_key = await AgentRuntimeService.soniox_api_key(tenant_res, "tts")
        except RuntimeError as exc:
            try:
                await websocket.send_json({
                    "type": "tts_error",
                    "stream_id": f"tts-{uuid.uuid4()}",
                    "error_code": "missing_api_key",
                    "error_message": str(exc),
                })
            except Exception:
                pass
            return

        provider_status = dict(tenant_res.provider_status or {})
        stream_id = f"tts-{uuid.uuid4()}"
        endpoint = provider_status.get("tts_stream_endpoint") or settings.SONIOX_TTS_STREAM_URL
        try:
            async with connect(endpoint, max_size=8 * 1024 * 1024) as tts_ws:
                connected_ms = int((perf_counter() - t0) * 1000)
                await tts_ws.send(json.dumps({
                    "api_key": api_key,
                    "model": provider_status.get("tts_model") or settings.SONIOX_TTS_MODEL,
                    "language": language,
                    "voice": provider_status.get("tts_voice") or settings.SONIOX_TTS_VOICE,
                    "audio_format": provider_status.get("tts_audio_format") or settings.SONIOX_TTS_AUDIO_FORMAT,
                    "sample_rate": provider_status.get("tts_sample_rate") or settings.SONIOX_TTS_SAMPLE_RATE,
                    "stream_id": stream_id,
                }))
                await tts_ws.send(json.dumps({"text": text[:5000], "text_end": True, "stream_id": stream_id}))
                request_sent_ms = int((perf_counter() - t0) * 1000)
                try:
                    await websocket.send_json({
                        "type": "tts_started",
                        "stream_id": stream_id,
                        "connected_ms": connected_ms,
                        "request_sent_ms": request_sent_ms,
                    })
                except Exception:
                    pass
                first_audio_sent = False
                while True:
                    raw = await tts_ws.recv()
                    message = json.loads(raw)
                    if message.get("audio"):
                        first_audio_latency_ms = None
                        if not first_audio_sent:
                            first_audio_sent = True
                            first_audio_latency_ms = int((perf_counter() - t0) * 1000)
                            try:
                                await websocket.send_json({
                                    "type": "tts_first_audio",
                                    "stream_id": stream_id,
                                    "first_audio_latency_ms": first_audio_latency_ms,
                                    "connected_ms": connected_ms,
                                    "request_sent_ms": request_sent_ms,
                                })
                            except Exception:
                                pass
                        try:
                            await websocket.send_json({
                                "type": "tts_audio",
                                "stream_id": stream_id,
                                "audio_base64": message["audio"],
                                "audio_end": bool(message.get("audio_end")),
                                "first_audio_latency_ms": first_audio_latency_ms,
                            })
                        except Exception:
                            pass
                    if message.get("error_code"):
                        error_message = message.get("error_message") or "TTS service error"
                        if "balance" in str(error_message).lower() or "fund" in str(error_message).lower():
                            clean = "Voice synthesis is temporarily unavailable."
                        else:
                            clean = f"Voice synthesis error: {error_message}"
                        try:
                            await websocket.send_json({
                                "type": "tts_error",
                                "stream_id": stream_id,
                                "error_code": message.get("error_code"),
                                "error_message": clean,
                            })
                        except Exception:
                            pass
                        return
                    if message.get("terminated"):
                        try:
                            await websocket.send_json({"type": "tts_done", "stream_id": stream_id})
                        except Exception:
                            pass
                        return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await websocket.send_json({
                    "type": "tts_error",
                    "stream_id": stream_id,
                    "error_code": "tts_connection_error",
                    "error_message": f"Voice synthesis unavailable: {str(exc)[:200]}",
                })
            except Exception:
                pass

    # -----------------------------------------------------------------------
    # Context resolver — cascades spec-cache → topic-continuity → live parallel
    # -----------------------------------------------------------------------

    @staticmethod
    async def _resolve_context(
        tenant_res: TenantResources,
        query: str,
        *,
        cached: dict | None,
        session_ctx: SessionContext,
        db: Any,
        answer_cache: "AgentAnswerCache | None" = None,
        resp_language: str = "en",
    ) -> dict[str, Any]:
        """Resolve intent + chunks for a query.

        Cascade:
          1. Speculative cache hit (precomputed during STT — may already have Redis answer)
          2. Topic continuity (reuse last chunks for follow-up questions)
          3. Live: Redis cache + intent + Qdrant all in parallel
        """
        ctx: dict[str, Any] = {
            "chunks": [],
            "citations": [],
            "intent_type": INTENT_AMBIGUOUS,
            "should_retrieve": False,
            "pre_answer": None,
            "source": "unknown",
            "redis_latency_ms": None,
        }

        if session_ctx.last_answer and _REPEAT_LAST_RE.search(query):
            ctx["intent_type"] = "REPEAT_LAST_ANSWER"
            ctx["should_retrieve"] = False
            ctx["pre_answer"] = session_ctx.last_answer
            ctx["source"] = "session_memory"
            return ctx

        # 1. Speculative cache (already ran during STT — may include Redis-cached answer)
        if cached and cached.get("intent"):
            intent = cached["intent"]
            if intent.get("type") == INTENT_AMBIGUOUS:
                live = await classify_intent(query)
                if live.get("type") != INTENT_AMBIGUOUS:
                    intent = live
            ctx["intent_type"] = intent.get("type", INTENT_AMBIGUOUS)
            ctx["should_retrieve"] = bool(intent.get("shouldRetrieve"))
            if not ctx["should_retrieve"]:
                ctx["source"] = "speculative_cache_conversation"
                return ctx
            if cached.get("chunks") is not None:
                ctx["chunks"] = cached["chunks"]
                ctx["citations"] = cached.get("citations") or []
                ctx["pre_answer"] = cached.get("answer")
                ctx["source"] = "speculative_cache_knowledge" if not cached.get("answer") else "redis_cache"
                return ctx
            # Intent cached but retrieval not ready — fall through to live

        # 2. Topic continuity
        if session_ctx.last_chunks and session_ctx.last_query:
            overlap = _word_overlap(query, session_ctx.last_query)
            if overlap >= settings.AGENT_TOPIC_CONTINUITY_OVERLAP:
                intent = await classify_intent(query)
                if intent.get("shouldRetrieve"):
                    ctx["intent_type"] = intent.get("type", INTENT_AMBIGUOUS)
                    ctx["should_retrieve"] = True
                    ctx["chunks"] = session_ctx.last_chunks
                    ctx["source"] = "topic_continuity"
                    return ctx

        # 3. Live: Redis answer cache + intent classification in parallel.
        # Qdrant starts only after intent says retrieval is required; greetings
        # and unclear fillers must not pay a vector-search penalty.
        intent_task = asyncio.create_task(classify_intent(query))
        redis_task = asyncio.create_task(
            answer_cache.get(query, resp_language)
        ) if answer_cache else None

        intent = await intent_task
        redis_hit = None
        if redis_task:
            redis_started = perf_counter()
            try:
                redis_hit = await asyncio.wait_for(asyncio.shield(redis_task), timeout=0.15)
            except (asyncio.TimeoutError, Exception):
                pass
            finally:
                ctx["redis_latency_ms"] = int((perf_counter() - redis_started) * 1000)

        ctx["intent_type"] = intent.get("type", INTENT_AMBIGUOUS)
        ctx["should_retrieve"] = bool(intent.get("shouldRetrieve"))

        if ctx["should_retrieve"] and redis_hit and redis_hit.get("answer"):
            ctx["pre_answer"] = redis_hit["answer"]
            ctx["chunks"] = redis_hit.get("chunks") or []
            ctx["source"] = "redis_cache"
            return ctx

        if ctx["should_retrieve"]:
            answer_card = AgentKnowledgeService.find_answer_card(tenant_res, query, resp_language)
            if answer_card and answer_card.get("answer"):
                ctx["pre_answer"] = answer_card["answer"]
                ctx["chunks"] = [
                    {
                        "chunk_id": chunk_id,
                        "text": answer_card["answer"],
                        "score": answer_card.get("score", 1.0),
                        "metadata": {
                            "source_type": "answer_card",
                            "policy_version": answer_card.get("policy_version"),
                            "sensitivity": answer_card.get("sensitivity"),
                            "topic": answer_card.get("topic"),
                        },
                    }
                    for chunk_id in answer_card.get("source_chunk_ids") or []
                ]
                ctx["citations"] = [
                    {"chunk_id": chunk_id, "score": answer_card.get("score", 1.0), "source": "answer_card"}
                    for chunk_id in answer_card.get("source_chunk_ids") or []
                ]
                ctx["source"] = "answer_card"
                return ctx

            retrieval_result = await AgentKnowledgeService.test_retrieval(
                tenant_res, query, top_k=settings.AGENT_RETRIEVAL_TOP_K, db=db
            )
            raw = retrieval_result.get("chunks") or []
            chunks = validate_chunk_relevance(raw, query)
            ctx["chunks"] = chunks
            ctx["citations"] = [
                {
                    "document_id": c.get("document_id"),
                    "document_name": c.get("document_name"),
                    "chunk_id": c.get("chunk_id"),
                    "score": c.get("score"),
                }
                for c in chunks
            ]
        ctx["source"] = "live"
        return ctx

    # -----------------------------------------------------------------------
    # Core answer + stream loop
    # -----------------------------------------------------------------------

    @staticmethod
    async def answer_and_stream(
        websocket: WebSocket,
        tenant_res: TenantResources,
        query: str,
        *,
        db: Any = None,
        session_ctx: SessionContext | None = None,
        speculative_cache: SpeculativeCache | None = None,
        answer_cache: AgentAnswerCache | None = None,
        warm_tts: WarmSonioxTTSStream | None = None,
        warm_tts_filler: WarmSonioxTTSStream | None = None,
        on_speaking: Callable[[], None] | None = None,
    ) -> None:
        """Run one full voice turn: resolve context → LLM → TTS.

        Raises asyncio.CancelledError if barge-in cancels this task — caller handles state.
        """
        t_start = perf_counter()
        turn_id = str(uuid.uuid4())[:8]
        query = normalize_message(query)
        ctx = session_ctx if session_ctx is not None else SessionContext()
        history = ctx.history

        resp_language = detect_language(query)
        if resp_language == "en":
            resp_language = ctx.language

        try:
            await websocket.send_json({"type": "agent_thinking", "query": query, "turn_id": turn_id})
        except Exception:
            pass

        # --- Filler coordination ------------------------------------------------
        # If the LLM doesn't produce a first sentence within FILLER_TRIGGER_MS,
        # play a short filler utterance on the dedicated connection to eliminate dead air.
        _filler_cancel = asyncio.Event()
        _filler_done = asyncio.Event()
        _filler_done.set()
        _filler_played = [False]

        async def _filler_guard() -> None:
            try:
                await asyncio.wait_for(
                    _filler_cancel.wait(), timeout=settings.FILLER_TRIGGER_MS / 1000
                )
            except asyncio.TimeoutError:
                if warm_tts_filler is not None:
                    _filler_played[0] = True
                    _filler_done.clear()
                    try:
                        await warm_tts_filler.stream(AgentRuntimeService.filler_phrase(resp_language), language=resp_language)
                    except asyncio.CancelledError:
                        pass
                    except Exception:
                        pass
                    finally:
                        _filler_done.set()

        filler_task = asyncio.create_task(_filler_guard())

        # Shared streaming state (dict avoids closure mutation issues)
        _st: dict[str, Any] = {
            "answer": "",
            "buf": "",
            "first_text": "",
            "first_sent": False,
            "tts_task": None,
        }

        async def _dispatch_first(sentence: str) -> None:
            """Cancel filler timer, wait for any filler to finish, then send to TTS."""
            sentence = AgentRuntimeService.voice_sanitize_answer(sentence)
            if not sentence:
                return
            _filler_cancel.set()
            if _filler_played[0]:
                try:
                    await asyncio.wait_for(_filler_done.wait(), timeout=0.35)
                except asyncio.TimeoutError:
                    pass
            if on_speaking:
                try:
                    on_speaking()
                except Exception:
                    pass
            if warm_tts is not None:
                _st["tts_task"] = asyncio.create_task(
                    warm_tts.stream(sentence, language=resp_language)
                )
            else:
                _st["tts_task"] = asyncio.create_task(
                    AgentVoiceStreamService.stream_tts(
                        websocket, tenant_res, sentence, language=resp_language
                    )
                )

        try:
            # --- Resolve context (spec cache → topic → live) --------------------
            cached = None
            if speculative_cache:
                cached = await speculative_cache.get_cached_result(query)

            resolve_ms = int((perf_counter() - t_start) * 1000)
            resolved = await AgentVoiceStreamService._resolve_context(
                tenant_res, query,
                cached=cached, session_ctx=ctx, db=db,
                answer_cache=answer_cache, resp_language=resp_language,
            )

            chunks = resolved["chunks"]
            citations = resolved["citations"]
            intent_type = resolved["intent_type"]
            should_retrieve = resolved["should_retrieve"]
            pre_answer = resolved.get("pre_answer")

            context_ms = int((perf_counter() - t_start) * 1000)
            if speculative_cache and not pre_answer:
                # Final turn owns generation now. Cancel any speculative LLM so
                # the session never runs duplicate answer-generation calls.
                if speculative_cache.answer_task and not speculative_cache.answer_task.done():
                    speculative_cache.answer_task.cancel()

            # --- Branch: conversation brain (no retrieval needed) ---------------
            if not should_retrieve:
                # No LLM for greetings, thanks, acknowledgements, or ambiguous
                # clarification turns. These must be instant, deterministic, and
                # never trigger document retrieval.
                answer = pre_answer or generate_conversation_reply(query, intent_type, language=resp_language)
                if not answer:
                    answer = AgentRuntimeService.how_can_i_help(resp_language)
                answer = AgentRuntimeService.voice_sanitize_answer(answer)
                _filler_cancel.set()
                if _filler_played[0]:
                    try:
                        await asyncio.wait_for(_filler_done.wait(), timeout=0.35)
                    except asyncio.TimeoutError:
                        pass
                if on_speaking:
                    try:
                        on_speaking()
                    except Exception:
                        pass

                result = {
                    "answer": answer,
                    "refused": False,
                    "citations": [],
                    "chunks": [],
                    "runtime": {
                        "graph": "conversation_brain",
                        "mode": "conversation_brain",
                        "model": settings.AZURE_OPENAI_AGENT_MODEL,
                        "response_language": resp_language,
                        "detected_language": resp_language,
                        "latency_ms": int((perf_counter() - t_start) * 1000),
                        "context_source": resolved["source"],
                        "redis_latency_ms": resolved.get("redis_latency_ms"),
                    },
                    "intent": {
                        "type": intent_type,
                        "should_retrieve": False,
                        "context_resolve_ms": resolve_ms,
                    },
                }

            # --- Branch: knowledge brain (with retrieved chunks) ----------------
            else:
                if pre_answer:
                    # Speculative cache already has the full answer
                    answer = pre_answer
                    answer = AgentRuntimeService.voice_sanitize_answer(answer)
                    _filler_cancel.set()
                    if _filler_played[0]:
                        try:
                            await asyncio.wait_for(_filler_done.wait(), timeout=0.35)
                        except asyncio.TimeoutError:
                            pass
                    if on_speaking:
                        try:
                            on_speaking()
                        except Exception:
                            pass
                elif chunks:
                    # Stream LLM, dispatch first sentence immediately, continue generating
                    async def _stream_llm() -> None:
                        async for tok in AzureAgentLLMClient.stream_grounded_answer(
                            tenant_res, query, chunks,
                            response_language=resp_language,
                            conversation_history=history,
                        ):
                            _st["buf"] += tok
                            _st["answer"] += tok
                            if not _st["first_sent"]:
                                boundary = _first_sentence(_st["buf"])
                                if boundary:
                                    first, rest = boundary
                                    _st["buf"] = rest
                                    _st["first_text"] = first
                                    _st["first_sent"] = True
                                    await _dispatch_first(first)

                    try:
                        await asyncio.wait_for(
                            _stream_llm(),
                            timeout=settings.AGENT_LLM_STREAM_TOTAL_MS / 1000,
                        )
                    except asyncio.TimeoutError:
                        pass
                    except asyncio.CancelledError:
                        raise

                    answer = _st["answer"].strip() or AgentRuntimeService._human_grounded_fallback(
                        query, chunks, resp_language
                    )
                    answer = AgentRuntimeService.voice_sanitize_answer(answer)

                    if not _st["first_sent"]:
                        # LLM produced nothing or timed out before first sentence
                        _filler_cancel.set()
                        if _filler_played[0]:
                            try:
                                await asyncio.wait_for(_filler_done.wait(), timeout=0.35)
                            except asyncio.TimeoutError:
                                pass
                        if on_speaking:
                            try:
                                on_speaking()
                            except Exception:
                                pass
                else:
                    answer = AgentRuntimeService.agent_refusal(resp_language)
                    answer = AgentRuntimeService.voice_sanitize_answer(answer)
                    _filler_cancel.set()
                    if _filler_played[0]:
                        try:
                            await asyncio.wait_for(_filler_done.wait(), timeout=0.35)
                        except asyncio.TimeoutError:
                            pass
                    if on_speaking:
                        try:
                            on_speaking()
                        except Exception:
                            pass

                result = {
                    "answer": answer or AgentRuntimeService.agent_refusal(resp_language),
                    "refused": not answer,
                    "citations": citations,
                    "chunks": chunks,
                    "runtime": {
                        "graph": "knowledge_brain",
                        "mode": "knowledge_brain",
                        "model": settings.AZURE_OPENAI_AGENT_MODEL,
                        "response_language": resp_language,
                        "detected_language": resp_language,
                        "latency_ms": int((perf_counter() - t_start) * 1000),
                        "context_source": resolved["source"],
                        "pre_answer_used": bool(pre_answer),
                        "context_ms": context_ms,
                        "redis_latency_ms": resolved.get("redis_latency_ms"),
                    },
                    "intent": {
                        "type": intent_type,
                        "should_retrieve": True,
                        "chunk_count": len(chunks),
                        "context_resolve_ms": resolve_ms,
                    },
                }

        except asyncio.CancelledError:
            # Barge-in: clean up filler, notify frontend, re-raise
            _filler_cancel.set()
            if not filler_task.done():
                filler_task.cancel()
            if _st.get("tts_task") and not _st["tts_task"].done():
                _st["tts_task"].cancel()
            try:
                await websocket.send_json({"type": "barge_in_detected", "turn_id": turn_id})
            except Exception:
                pass
            raise

        finally:
            # Always cancel filler if still pending
            if not _filler_cancel.is_set():
                _filler_cancel.set()
            if not filler_task.done():
                filler_task.cancel()

        # --- Emit answer to frontend -------------------------------------------
        total_ms = int((perf_counter() - t_start) * 1000)
        result["runtime"]["latency_ms"] = total_ms
        try:
            await websocket.send_json({
                "type": "agent_answer",
                "turn_id": turn_id,
                "answer": result["answer"],
                "refused": result["refused"],
                "citations": result.get("citations") or [],
                "runtime": result.get("runtime"),
                "intent": result.get("intent"),
            })
        except Exception:
            pass

        # --- Update session context --------------------------------------------
        if session_ctx is not None:
            session_ctx.add_turn(query, result["answer"], intent_type=result.get("intent", {}).get("type", ""))
            if chunks:
                session_ctx.update_chunks(query, chunks)
            if resp_language and resp_language != "en":
                session_ctx.language = resp_language

        # --- Write-through: cache successful grounded answers in Redis ---------
        if (
            answer_cache
            and result.get("answer")
            and not result.get("refused")
            and resolved.get("source") not in ("redis_cache",)  # type: ignore[possibly-undefined]
            and chunks
            and AgentAnswerCache.cacheable(query, result["answer"], chunks)
        ):
            asyncio.create_task(
                answer_cache.set(query, resp_language, result["answer"], chunks)
            )

        # --- TTS dispatch ------------------------------------------------------
        # First sentence may already be playing via _st["tts_task"].
        # Send remaining text after it completes.
        tts_lang = resp_language
        answer_text = result["answer"]

        if _st.get("tts_task"):
            try:
                await _st["tts_task"]
            except Exception:
                pass
            remainder = answer_text[len(_st["first_text"]):].strip()
            if remainder:
                if warm_tts is not None:
                    await warm_tts.stream(remainder, language=tts_lang)
                else:
                    await AgentVoiceStreamService.stream_tts(
                        websocket, tenant_res, remainder, language=tts_lang
                    )
        else:
            # No sentence was pre-dispatched (pre_answer / no-retrieve / refusal paths)
            if answer_text:
                if warm_tts is not None:
                    await warm_tts.stream(answer_text, language=tts_lang)
                else:
                    await AgentVoiceStreamService.stream_tts(
                        websocket, tenant_res, answer_text, language=tts_lang
                    )

        # --- Observability event -----------------------------------------------
        try:
            latency_profile = {
                "turn_id": turn_id,
                "tenant_id": tenant_res.tenant_id,
                "language": resp_language,
                "intent": result.get("intent", {}).get("type"),
                "context_source": resolved.get("source", "unknown"),  # type: ignore[possibly-undefined]
                "cache_hit": resolved.get("source") == "redis_cache",  # type: ignore[possibly-undefined]
                "answer_card_hit": resolved.get("source") == "answer_card",  # type: ignore[possibly-undefined]
                "topic_continuity_hit": resolved.get("source") == "topic_continuity",  # type: ignore[possibly-undefined]
                "context_ms": result.get("runtime", {}).get("context_ms"),
                "total_turn_ms": int((perf_counter() - t_start) * 1000),
                "filler_used": _filler_played[0],
                "error_type": None,
            }
            await websocket.send_json({
                "type": "turn_complete",
                "turn_id": turn_id,
                "total_ms": latency_profile["total_turn_ms"],
                "filler_played": _filler_played[0],
                "context_source": resolved.get("source", "unknown"),  # type: ignore[possibly-undefined]
                "latency_profile": latency_profile,
            })
            await websocket.send_json({"type": "latency_profile", **latency_profile})
        except Exception:
            pass

        if speculative_cache:
            speculative_cache.reset()

    # -----------------------------------------------------------------------
    # Session runner
    # -----------------------------------------------------------------------

    @staticmethod
    async def run_session(websocket: WebSocket, tenant_res: TenantResources, *, db=None) -> None:
        await websocket.accept()

        provider_status = dict(tenant_res.provider_status or {})
        session_ctx = SessionContext()
        spec_cache = SpeculativeCache()

        # Redis answer cache — eliminates LLM for repeated questions
        _redis_client = None
        _answer_cache: AgentAnswerCache | None = None
        if settings.AGENT_ANSWER_CACHE_ENABLED and settings.REDIS_URL:
            try:
                import redis.asyncio as aioredis
                _redis_client = aioredis.from_url(
                    settings.REDIS_URL, decode_responses=True, socket_connect_timeout=1
                )
                _answer_cache = AgentAnswerCache(
                    _redis_client,
                    tenant_res.tenant_id,
                    AgentKnowledgeService.policy_version(tenant_res),
                )
            except Exception:
                pass

        # Dual warm TTS: one for answers, one for filler
        warm_tts = WarmSonioxTTSStream(websocket, tenant_res, purpose="answer")
        warm_tts_filler = WarmSonioxTTSStream(websocket, tenant_res, purpose="filler")

        # Use a mutable container for state so all nested closures share it without nonlocal
        _s: dict[str, Any] = {
            "state": TurnState.IDLE,
            "answer_task": None,   # asyncio.Task | None
            "stt_ws": None,
            "stt_reader": None,
            "endpoint_timer": None,
        }
        final_transcript_parts: list[str] = []

        async def _prewarm_tts_pair(language: str, *, timeout_s: float = 2.0) -> None:
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        warm_tts.warm(language),
                        warm_tts_filler.warm(language),
                    ),
                    timeout=timeout_s,
                )
            except asyncio.TimeoutError:
                print(f"[NOKVO-TTS] warm timeout after {timeout_s:.1f}s; continuing with lazy reconnect")

        def _on_speaking() -> None:
            _s["state"] = TurnState.SPEAKING

        async def _run_answer(text: str) -> None:
            _s["state"] = TurnState.PROCESSING
            task = asyncio.create_task(
                AgentVoiceStreamService.answer_and_stream(
                    websocket, tenant_res, text,
                    db=db,
                    session_ctx=session_ctx,
                    speculative_cache=spec_cache,
                    answer_cache=_answer_cache,
                    warm_tts=warm_tts,
                    warm_tts_filler=warm_tts_filler,
                    on_speaking=_on_speaking,
                )
            )
            _s["answer_task"] = task
            try:
                await task
            except asyncio.CancelledError:
                # Barge-in cancelled this turn — re-warm TTS for the next turn
                asyncio.create_task(_prewarm_tts_pair(session_ctx.language))
            except Exception as exc:
                try:
                    await websocket.send_json({"type": "agent_error", "error": str(exc)[:200]})
                except Exception:
                    pass
            finally:
                _s["state"] = TurnState.IDLE
                _s["answer_task"] = None

        async def _trigger_answer() -> None:
            if _s["state"] in (TurnState.PROCESSING, TurnState.SPEAKING):
                return
            final_text = " ".join(final_transcript_parts).strip()
            if not final_text:
                return
            final_transcript_parts.clear()
            try:
                await websocket.send_json({"type": "stt_finished", "text": final_text})
            except Exception:
                pass
            await _run_answer(final_text)

        async def _debounce_endpoint(delay: float = 0.05) -> None:
            await asyncio.sleep(delay)
            await _trigger_answer()

        def _schedule_endpoint() -> None:
            if _s["endpoint_timer"] and not _s["endpoint_timer"].done():
                _s["endpoint_timer"].cancel()
            if _s["state"] not in (TurnState.PROCESSING, TurnState.SPEAKING):
                _s["endpoint_timer"] = asyncio.create_task(_debounce_endpoint())

        async def _ensure_stt() -> Any:
            if _s["stt_ws"] is not None:
                return _s["stt_ws"]

            api_key = await AgentRuntimeService.soniox_api_key(tenant_res, "stt")
            endpoint = provider_status.get("stt_endpoint") or settings.SONIOX_STT_WEBSOCKET_URL
            stt_conn = await connect(endpoint, max_size=8 * 1024 * 1024)
            await stt_conn.send(json.dumps({
                "api_key": api_key,
                "model": provider_status.get("stt_model") or settings.SONIOX_STT_MODEL,
                "audio_format": provider_status.get("stt_audio_format") or settings.SONIOX_STT_AUDIO_FORMAT,
                "language_hints": provider_status.get("stt_language_hints") or [session_ctx.language],
                "enable_endpoint_detection": True,
                "max_endpoint_delay_ms": 200,
                "client_reference_id": f"nokvo-{tenant_res.tenant_id}-{uuid.uuid4()}",
            }))
            _s["stt_ws"] = stt_conn

            async def _read_stt() -> None:
                try:
                    async for raw in stt_conn:
                        payload = json.loads(raw)
                        text, is_final = AgentVoiceStreamService._extract_transcript(payload)

                        if text:
                            # Barge-in: user speaks while agent is speaking → cancel TTS
                            task = _s["answer_task"]
                            if _s["state"] == TurnState.SPEAKING and task and not task.done():
                                task.cancel()
                                _s["state"] = TurnState.LISTENING

                            if is_final:
                                final_transcript_parts.append(text)
                                _schedule_endpoint()
                            else:
                                et = _s["endpoint_timer"]
                                if et and not et.done():
                                    et.cancel()

                            # Speculative processing on every token
                            running = " ".join(final_transcript_parts).strip()
                            if not is_final and text:
                                running = f"{running} {text}".strip() if running else text
                            if running:
                                await spec_cache.process_interim(running, tenant_res, db=db, answer_cache=_answer_cache)

                            try:
                                await websocket.send_json({
                                    "type": "stt_transcript",
                                    "text": text,
                                    "is_final": is_final,
                                })
                            except Exception:
                                pass

                        if payload.get("finished"):
                            et = _s["endpoint_timer"]
                            if et and not et.done():
                                et.cancel()
                            await _trigger_answer()

                except Exception as exc:
                    try:
                        await websocket.send_json({"type": "stt_error", "error": str(exc)})
                    except Exception:
                        pass

            _s["stt_reader"] = asyncio.create_task(_read_stt())
            try:
                await websocket.send_json({"type": "voice_session_ready"})
            except Exception:
                pass
            return stt_conn

        try:
            try:
                await websocket.send_json(
                    AgentRuntimeService.runtime_status(tenant_res) | {"type": "runtime_status"}
                )
            except Exception:
                pass

            # Warm both TTS connections at session start so the first answer
            # does not pay the WebSocket/TLS handshake cost.
            await _prewarm_tts_pair(session_ctx.language)

            while True:
                try:
                    message = await websocket.receive()
                except RuntimeError as exc:
                    if "disconnect message" in str(exc).lower():
                        break
                    raise
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    socket = await _ensure_stt()
                    await socket.send(message["bytes"])
                    continue
                raw_text = message.get("text")
                if raw_text is None:
                    continue
                payload = json.loads(raw_text)
                event_type = payload.get("type")

                if event_type == "config":
                    lang = AgentRuntimeService.normalize_language(str(payload.get("language") or "en"))
                    session_ctx.language = lang
                    asyncio.create_task(_prewarm_tts_pair(lang))
                    await _ensure_stt()

                elif event_type == "audio":
                    socket = await _ensure_stt()
                    await socket.send(base64.b64decode(payload.get("audio_base64") or ""))

                elif event_type in {"text_query", "transcript"}:
                    lang = AgentRuntimeService.normalize_language(
                        str(payload.get("language") or session_ctx.language)
                    )
                    session_ctx.language = lang
                    text = str(payload.get("text") or "").strip()
                    if text and _s["state"] not in (TurnState.PROCESSING, TurnState.SPEAKING):
                        try:
                            await websocket.send_json({
                                "type": "stt_finished",
                                "text": text,
                                "source": "manual",
                            })
                        except Exception:
                            pass
                        asyncio.create_task(_run_answer(text))

                elif event_type in {"stop", "finalize"}:
                    et = _s["endpoint_timer"]
                    if et and not et.done():
                        et.cancel()
                    stt_conn = _s["stt_ws"]
                    if stt_conn is not None:
                        try:
                            await stt_conn.send(b"")
                        except Exception:
                            pass
                    await _trigger_answer()

                elif event_type == "close":
                    break

        finally:
            et = _s["endpoint_timer"]
            if et and not et.done():
                et.cancel()
            task = _s["answer_task"]
            if task and not task.done():
                task.cancel()
            spec_cache.reset()
            reader = _s["stt_reader"]
            if reader:
                reader.cancel()
            stt_conn = _s["stt_ws"]
            if stt_conn is not None:
                try:
                    await stt_conn.close()
                except Exception:
                    pass
            await warm_tts.close()
            await warm_tts_filler.close()
            if _redis_client is not None:
                try:
                    await _redis_client.aclose()
                except Exception:
                    pass
