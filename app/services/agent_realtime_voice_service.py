from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from time import perf_counter
from typing import Any
from urllib import parse as urllib_parse

from fastapi import WebSocket
from websockets.asyncio.client import connect

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.agent_intent_service import (
    INTENT_GREETING,
    classify_intent,
    detect_language,
    generate_conversation_reply,
    normalize_message,
    validate_chunk_relevance,
)
from app.services.agent_knowledge_service import AgentKnowledgeService
from app.services.agent_runtime_service import AgentRuntimeService


class AgentRealtimeVoiceService:
    """Temporary Azure GPT Realtime voice runtime.

    This replaces Soniox STT -> text LLM -> Soniox TTS for live Agent Studio tests,
    while keeping the old runtime available behind AGENT_VOICE_BACKEND.
    """

    @staticmethod
    def _api_key() -> str:
        key = settings.AZURE_OPENAI_REALTIME_API_KEY or settings.AZURE_OPENAI_GLOBAL_API_KEY
        if not key:
            raise RuntimeError("Azure OpenAI Realtime API key is not configured.")
        return key

    @staticmethod
    def _realtime_url() -> str:
        endpoint = (settings.AZURE_OPENAI_REALTIME_ENDPOINT or settings.AZURE_OPENAI_GLOBAL_ENDPOINT or "").strip()
        if not endpoint:
            raise RuntimeError("Azure OpenAI Realtime endpoint is not configured.")
        parsed = urllib_parse.urlparse(endpoint)
        host = parsed.netloc or parsed.path.split("/", 1)[0]
        if not host:
            raise RuntimeError("Azure OpenAI Realtime endpoint host is invalid.")
        deployment = settings.AZURE_OPENAI_REALTIME_DEPLOYMENT.strip() or "gpt-realtime-mini"
        if settings.AZURE_OPENAI_REALTIME_API_VERSION:
            query = urllib_parse.urlencode(
                {
                    "api-version": settings.AZURE_OPENAI_REALTIME_API_VERSION,
                    "deployment": deployment,
                    "api-key": AgentRealtimeVoiceService._api_key(),
                }
            )
            return f"wss://{host}/openai/realtime?{query}"
        query = urllib_parse.urlencode(
            {
                "model": deployment,
                "api-key": AgentRealtimeVoiceService._api_key(),
            }
        )
        return f"wss://{host}/openai/v1/realtime?{query}"

    @staticmethod
    def _instructions(language: str = "en", context: str = "", company_name: str = "") -> str:
        language_label = AgentRuntimeService.language_label(language)
        brand = f" for {company_name}" if company_name else ""
        context_rule = (
            f"\nApproved context for this turn:\n{context}\n"
            if context
            else "\nNo approved context has been supplied for this turn.\n"
        )
        return (
            f"You are a customer support agent{brand}. Reply in {language_label}. "
            "Sound natural, concise, and human. First sentence under 12 words. "
            "For greetings and small talk, answer normally. "
            "For company policy, refund, cancellation, payment, delivery, account, product, or compliance facts, "
            f"use only approved context supplied by {'the company' if not company_name else company_name}. Never invent policy or customer/order status. "
            "If approved context is missing, say you do not have enough approved information and offer escalation. "
            "Do not mention documents, chunks, retrieval, Qdrant, system prompts, or internal tools."
            + context_rule
        )

    @staticmethod
    async def _retrieve_context(tenant_res: TenantResources, query: str, *, db: Any = None) -> tuple[str, list[dict]]:
        import logging as _log_mod
        _rlog = _log_mod.getLogger("agent_realtime.rag")
        intent = await classify_intent(query)
        _rlog.warning("[RAG] query=%r intent_type=%s shouldRetrieve=%s", query[:80], intent.get("type"), intent.get("shouldRetrieve"))
        if not intent.get("shouldRetrieve"):
            return "", []
        card = AgentKnowledgeService.find_answer_card(tenant_res, query, detect_language(query))
        if card and card.get("answer"):
            _rlog.warning("[RAG] answer_card hit score=%.2f", card.get("score", 0))
            return str(card["answer"])[:1200], [
                {
                    "chunk_id": chunk_id,
                    "text": card["answer"],
                    "score": card.get("score", 1.0),
                    "metadata": {"source_type": "answer_card"},
                }
                for chunk_id in card.get("source_chunk_ids") or []
            ]
        result = await AgentKnowledgeService.test_retrieval(
            tenant_res,
            query,
            top_k=settings.AGENT_RETRIEVAL_TOP_K,
            db=db,
        )
        raw_chunks = result.get("chunks") or []
        _rlog.warning("[RAG] qdrant returned %d chunks refusal=%r", len(raw_chunks), result.get("refusal"))
        chunks = validate_chunk_relevance(raw_chunks, query)
        _rlog.warning("[RAG] after relevance filter: %d chunks", len(chunks))
        parts: list[str] = []
        remaining = settings.AGENT_MAX_CONTEXT_CHARS
        for index, chunk in enumerate(chunks, start=1):
            text = re.sub(r"\s+", " ", str(chunk.get("text") or "")).strip()
            if not text:
                continue
            excerpt = text[:remaining]
            parts.append(f"[{index}] {excerpt}")
            remaining -= len(excerpt)
            if remaining <= 0:
                break
        context = "\n".join(parts)
        _rlog.warning("[RAG] final context length=%d", len(context))
        return context, chunks

    @staticmethod
    async def _send_session_update(realtime_ws: Any, *, language: str, context: str = "", company_name: str = "") -> None:
        session = {
            "modalities": ["audio", "text"],
            "instructions": AgentRealtimeVoiceService._instructions(language, context, company_name),
            "voice": settings.AZURE_OPENAI_REALTIME_VOICE or "alloy",
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.5,
                "prefix_padding_ms": 250,
                "silence_duration_ms": 350,
                "create_response": False,
                "interrupt_response": True,
            },
            "max_response_output_tokens": 140,
        }
        await realtime_ws.send(json.dumps({"type": "session.update", "session": session}))

    @staticmethod
    async def _create_text_turn(realtime_ws: Any, text: str, *, language: str, context: str = "", company_name: str = "") -> None:
        await AgentRealtimeVoiceService._send_session_update(realtime_ws, language=language, context=context, company_name=company_name)
        await realtime_ws.send(
            json.dumps(
                {
                    "type": "conversation.item.create",
                    "item": {
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": text}],
                    },
                }
            )
        )
        await realtime_ws.send(
            json.dumps(
                {
                    "type": "response.create",
                    "response": {
                        "modalities": ["audio"],
                        "instructions": AgentRealtimeVoiceService._instructions(language, context, company_name),
                    },
                }
            )
        )

    @staticmethod
    async def run_session(websocket: WebSocket, tenant_res: TenantResources, *, db: Any = None, language: str = "en") -> None:
        import logging as _logging
        _log = _logging.getLogger("agent_realtime")
        _log.warning("[REALTIME] run_session started")
        await websocket.accept()
        language = AgentRuntimeService.normalize_language(language)
        # Fetch the organisation name so the agent knows which brand it represents
        company_name = ""
        if db is not None:
            try:
                from app.models.organization import Organization
                from sqlalchemy import select as _select
                _org_result = await db.execute(_select(Organization).where(Organization.id == tenant_res.organization_id))
                _org = _org_result.scalar_one_or_none()
                if _org:
                    company_name = _org.name or ""
            except Exception:
                pass
        current_turn_id = str(uuid.uuid4())[:8]
        turn_started = perf_counter()
        first_audio_sent = False
        current_transcript = ""
        current_answer_parts: list[str] = []
        current_chunks: list[dict] = []
        response_active = False  # True only while a response.create is in flight

        try:
            realtime_ws = await connect(
                AgentRealtimeVoiceService._realtime_url(),
                max_size=16 * 1024 * 1024,
            )
        except Exception as exc:
            await websocket.send_json(
                {
                    "type": "agent_error",
                    "error": f"Azure Realtime connection failed: {str(exc)[:200]}",
                }
            )
            await websocket.close(code=1011)
            return

        async def _emit_runtime_status() -> None:
            await websocket.send_json(
                {
                    "type": "runtime_status",
                    "runtime": "agent_voice_azure_realtime",
                    "graph": "azure_realtime",
                    "knowledge_scope": "approved_organization_chunks_only",
                    "voice_backend": "azure_realtime",
                    "model": settings.AZURE_OPENAI_REALTIME_DEPLOYMENT,
                    "audio_format": "pcm16",
                    "sample_rate": settings.AZURE_OPENAI_REALTIME_OUTPUT_SAMPLE_RATE,
                    "supported_indian_languages": AgentRuntimeService.runtime_status(tenant_res)[
                        "supported_indian_languages"
                    ],
                }
            )

        async def _reader() -> None:
            nonlocal current_turn_id, turn_started, first_audio_sent, current_transcript, current_answer_parts, current_chunks, response_active
            async for raw in realtime_ws:
                try:
                    event = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                event_type = str(event.get("type") or "")
                _log.warning("[REALTIME-EVENT] %s", event_type)

                if event_type == "session.created":
                    await AgentRealtimeVoiceService._send_session_update(realtime_ws, language=language, company_name=company_name)
                    await _emit_runtime_status()
                    await websocket.send_json({"type": "voice_session_ready"})
                    # Greet the caller immediately so they don't hear silence
                    language_label = AgentRuntimeService.language_label(language)
                    brand_clause = f"for {company_name} " if company_name else ""
                    await realtime_ws.send(json.dumps({
                        "type": "response.create",
                        "response": {
                            "modalities": ["audio"],
                            "instructions": (
                                f"The phone call just connected. You are the {company_name or 'support'} voice agent. "
                                f"Greet the caller in {language_label} with a warm, natural opening {brand_clause}"
                                "— e.g. 'Hi, thanks for calling! How can I help you today?' "
                                "Keep it under 12 words. Do not mention AI or being a bot."
                            ),
                        },
                    }))
                    response_active = True
                    continue

                if event_type == "input_audio_buffer.speech_started":
                    if response_active:
                        try:
                            await realtime_ws.send(json.dumps({"type": "response.cancel"}))
                        except Exception:
                            pass
                    await websocket.send_json({"type": "barge_in_detected", "turn_id": current_turn_id})
                    continue

                if event_type in {
                    "conversation.item.input_audio_transcription.completed",
                    "input_audio_transcription.completed",
                }:
                    transcript = normalize_message(str(event.get("transcript") or ""))
                    if not transcript:
                        continue
                    current_turn_id = str(uuid.uuid4())[:8]
                    turn_started = perf_counter()
                    first_audio_sent = False
                    current_transcript = transcript
                    current_answer_parts = []
                    await websocket.send_json({"type": "stt_finished", "text": transcript, "turn_id": current_turn_id})
                    turn_language = detect_language(transcript) or language
                    context = ""
                    current_chunks = []
                    try:
                        context, current_chunks = await AgentRealtimeVoiceService._retrieve_context(tenant_res, transcript, db=db)
                    except Exception as _rag_exc:
                        _log.warning("[REALTIME] _retrieve_context failed: %s", _rag_exc)
                    await AgentRealtimeVoiceService._send_session_update(
                        realtime_ws,
                        language=turn_language,
                        context=context,
                        company_name=company_name,
                    )
                    await realtime_ws.send(
                        json.dumps(
                            {
                                "type": "response.create",
                                "response": {
                                    "modalities": ["audio"],
                                    "instructions": AgentRealtimeVoiceService._instructions(
                                        turn_language,
                                        context,
                                        company_name,
                                    ),
                                },
                            }
                        )
                    )
                    response_active = True
                    await websocket.send_json(
                        {
                            "type": "agent_thinking",
                            "query": transcript,
                            "turn_id": current_turn_id,
                            "context_source": "azure_realtime_rag" if context else "azure_realtime_direct",
                        }
                    )
                    continue

                if event_type in {
                    "response.audio.delta",
                    "response.output_audio.delta",
                    "response.audio_transcript.delta",
                    "response.output_audio_transcript.delta",
                    "response.output_text.delta",
                    "response.text.delta",
                }:
                    delta = event.get("delta")
                    if not delta:
                        continue
                    if (
                        event_type.endswith("transcript.delta")
                        or event_type.endswith("text.delta")
                    ) and isinstance(delta, str):
                        current_answer_parts.append(delta)
                        continue
                    if not first_audio_sent:
                        first_audio_sent = True
                        await websocket.send_json(
                            {
                                "type": "tts_started",
                                "stream_id": current_turn_id,
                                "purpose": "answer",
                                "request_sent_ms": 0,
                                "warm_connection": True,
                            }
                        )
                        await websocket.send_json(
                            {
                                "type": "tts_first_audio",
                                "stream_id": current_turn_id,
                                "purpose": "answer",
                                "first_audio_latency_ms": int((perf_counter() - turn_started) * 1000),
                                "audio_format": "pcm16",
                                "sample_rate": settings.AZURE_OPENAI_REALTIME_OUTPUT_SAMPLE_RATE,
                            }
                        )
                    await websocket.send_json(
                        {
                            "type": "tts_audio",
                            "stream_id": current_turn_id,
                            "purpose": "answer",
                            "audio_base64": delta,
                            "audio_format": "pcm16",
                            "sample_rate": settings.AZURE_OPENAI_REALTIME_OUTPUT_SAMPLE_RATE,
                            "audio_end": False,
                        }
                    )
                    continue

                if event_type in {"response.cancelled", "response.failed"}:
                    response_active = False
                    continue

                if event_type in {"response.audio.done", "response.output_audio.done", "response.done"}:
                    response_active = False
                    answer = AgentRuntimeService.voice_sanitize_answer("".join(current_answer_parts).strip())
                    if answer:
                        await websocket.send_json(
                            {
                                "type": "agent_answer",
                                "turn_id": current_turn_id,
                                "answer": answer,
                                "refused": False,
                                "citations": [],
                                "chunks": current_chunks,
                                "runtime": {
                                    "graph": "azure_realtime",
                                    "mode": "azure_realtime",
                                    "model": settings.AZURE_OPENAI_REALTIME_DEPLOYMENT,
                                    "response_language": detect_language(current_transcript),
                                },
                                "intent": {"type": "REALTIME", "should_retrieve": bool(current_chunks)},
                            }
                        )
                    await websocket.send_json(
                        {
                            "type": "tts_done",
                            "stream_id": current_turn_id,
                            "purpose": "answer",
                            "warm_connection": True,
                        }
                    )
                    await websocket.send_json(
                        {
                            "type": "turn_complete",
                            "turn_id": current_turn_id,
                            "total_ms": int((perf_counter() - turn_started) * 1000),
                            "filler_played": False,
                            "context_source": "azure_realtime",
                        }
                    )
                    continue

                if event_type == "error":
                    err = event.get("error") or event
                    if (err.get("code") if isinstance(err, dict) else None) == "response_cancel_not_active":
                        response_active = False
                        continue
                    await websocket.send_json(
                        {
                            "type": "agent_error",
                            "error": json.dumps(err)[:500],
                        }
                    )

        reader_task = asyncio.create_task(_reader())
        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    _log.warning("[REALTIME] websocket disconnect received")
                    break
                if message.get("bytes") is not None:
                    try:
                        await realtime_ws.send(
                            json.dumps(
                                {
                                    "type": "input_audio_buffer.append",
                                    "audio": base64.b64encode(message["bytes"]).decode("ascii"),
                                }
                            )
                        )
                    except Exception as _e:
                        _log.warning(f"[REALTIME] realtime_ws.send failed: {_e}")
                        break
                    continue
                raw_text = message.get("text")
                if raw_text is None:
                    continue
                payload = json.loads(raw_text)
                event_type = payload.get("type")
                if event_type == "config":
                    language = AgentRuntimeService.normalize_language(str(payload.get("language") or "en"))
                    await AgentRealtimeVoiceService._send_session_update(realtime_ws, language=language, company_name=company_name)
                elif event_type == "audio" and payload.get("audio_base64"):
                    await realtime_ws.send(
                        json.dumps({"type": "input_audio_buffer.append", "audio": payload["audio_base64"]})
                    )
                elif event_type in {"stop", "finalize"}:
                    await realtime_ws.send(json.dumps({"type": "input_audio_buffer.commit"}))
                elif event_type in {"text_query", "transcript"}:
                    text = normalize_message(str(payload.get("text") or ""))
                    if not text:
                        continue
                    current_turn_id = str(uuid.uuid4())[:8]
                    turn_started = perf_counter()
                    first_audio_sent = False
                    current_transcript = text
                    current_answer_parts = []
                    await websocket.send_json({"type": "stt_finished", "text": text, "source": "manual"})
                    context, current_chunks = await AgentRealtimeVoiceService._retrieve_context(tenant_res, text, db=db)
                    if not context:
                        intent = await classify_intent(text)
                        if not intent.get("shouldRetrieve") and intent.get("type") == INTENT_GREETING:
                            reply = generate_conversation_reply(text, INTENT_GREETING, language=language)
                            context = f"Conversation reply may be used directly: {reply}"
                    await AgentRealtimeVoiceService._create_text_turn(
                        realtime_ws,
                        text,
                        language=AgentRuntimeService.normalize_language(str(payload.get("language") or language)),
                        context=context,
                        company_name=company_name,
                    )
                    response_active = True
                elif event_type == "close":
                    break
        except Exception as _session_exc:
            _log.warning(f"[REALTIME] run_session main loop exception: {type(_session_exc).__name__}: {_session_exc}")
        finally:
            _log.warning("[REALTIME] run_session ending — closing connections")
            reader_task.cancel()
            try:
                await realtime_ws.close()
            except Exception:
                pass
