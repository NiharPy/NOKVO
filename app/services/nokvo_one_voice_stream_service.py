from __future__ import annotations

import asyncio
import json
import uuid
from time import perf_counter
from typing import Any

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.services.agent_session_store import AgentSessionStore
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.sarvam_voice_service import SarvamVoiceService


class NokvoOneVoiceStreamService:
    @staticmethod
    async def _company_name(db: AsyncSession | None, tenant_res: TenantResources) -> str:
        if db is None:
            return ""
        try:
            result = await db.execute(select(Organization).where(Organization.id == tenant_res.organization_id))
            organization = result.scalars().first()
            return organization.name if organization else ""
        except Exception:
            return ""

    @staticmethod
    async def _emit_runtime_status(websocket: WebSocket, tenant_res: TenantResources) -> None:
        await websocket.send_json({"type": "runtime_status", **NokvoOneVoicePipeline.runtime_status(tenant_res)})

    @staticmethod
    async def _run_text_turn(
        websocket: WebSocket,
        tenant_res: TenantResources,
        text: str,
        *,
        db: AsyncSession | None = None,
        language: str = "en",
        call_id: str | None = None,
        company_name: str | None = None,
        campaign_context: dict[str, Any] | None = None,
        source: str = "manual",
    ) -> None:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return
        turn_id = str(uuid.uuid4())[:8]
        started = perf_counter()
        await websocket.send_json({"type": "stt_finished", "text": cleaned, "turn_id": turn_id, "source": source})
        await websocket.send_json({"type": "agent_thinking", "turn_id": turn_id, "query": cleaned})
        answer_parts: list[str] = []
        final_payload: dict[str, Any] | None = None
        first_sentence_ms: int | None = None
        try:
            async for event in NokvoOneVoicePipeline.stream_answer_sentences(
                tenant_res,
                cleaned,
                db=db,
                top_k=settings.AGENT_RETRIEVAL_TOP_K,
                response_language=language,
                call_id=call_id,
                campaign_id=(campaign_context or {}).get("campaign_id"),
                campaign_goal=(campaign_context or {}).get("goal"),
                company_name=company_name,
            ):
                if event.get("type") == "sentence":
                    sentence = str(event.get("text") or "").strip()
                    if not sentence:
                        continue
                    if first_sentence_ms is None:
                        first_sentence_ms = int((perf_counter() - started) * 1000)
                    answer_parts.append(sentence)
                    await websocket.send_json(
                        {
                            "type": "agent_sentence",
                            "turn_id": turn_id,
                            "sentence": sentence,
                            "first_sentence_ms": first_sentence_ms,
                            "cache_hit": bool(event.get("cache_hit")),
                        }
                    )
                    try:
                        await SarvamVoiceService.stream_sentence_tts(
                            websocket,
                            tenant_res,
                            sentence,
                            language=language,
                            purpose="answer",
                        )
                    except Exception as exc:
                        await websocket.send_json(
                            {
                                "type": "tts_error",
                                "turn_id": turn_id,
                                "error_message": str(exc)[:240],
                                "provider": "sarvam",
                            }
                        )
                elif event.get("type") == "final":
                    final_payload = event
        except asyncio.CancelledError:
            await websocket.send_json({"type": "turn_cancelled", "turn_id": turn_id})
            raise
        except Exception as exc:
            fallback = NokvoOneVoicePipeline._refusal(language)
            answer_parts = [fallback]
            final_payload = {"answer": fallback, "refused": True, "chunks": [], "citations": [], "runtime": {"error": str(exc)[:240]}}
            await websocket.send_json({"type": "agent_error", "turn_id": turn_id, "error": str(exc)[:240]})

        answer = str((final_payload or {}).get("answer") or " ".join(answer_parts)).strip()
        await websocket.send_json(
            {
                "type": "agent_answer",
                "turn_id": turn_id,
                "answer": answer,
                "refused": bool((final_payload or {}).get("refused")),
                "citations": (final_payload or {}).get("citations") or [],
                "chunks": (final_payload or {}).get("chunks") or [],
                "runtime": (final_payload or {}).get("runtime") or {},
                "retrieval": (final_payload or {}).get("retrieval") or {},
                "intent": (final_payload or {}).get("intent") or {"type": "RAG_ALWAYS_ON", "should_retrieve": True},
            }
        )
        await websocket.send_json(
            {
                "type": "turn_complete",
                "turn_id": turn_id,
                "total_ms": int((perf_counter() - started) * 1000),
                "context_source": "semantic_cache_or_qdrant",
                "filler_played": False,
            }
        )

    @staticmethod
    async def run_session(
        websocket: WebSocket,
        tenant_res: TenantResources,
        *,
        db: AsyncSession | None = None,
        language: str = "en",
        call_id: str | None = None,
        campaign_context: dict[str, Any] | None = None,
    ) -> None:
        await websocket.accept()
        language = SarvamVoiceService.normalize_language(language)
        call_id = call_id or str(uuid.uuid4())
        company_name = await NokvoOneVoiceStreamService._company_name(db, tenant_res)
        current_turn: asyncio.Task | None = None
        stt_ws: Any = None
        stt_reader_task: asyncio.Task | None = None
        audio_buffer = bytearray()
        sample_rate = int(settings.SARVAM_STT_SAMPLE_RATE)

        async def _start_stt() -> None:
            nonlocal stt_ws, stt_reader_task
            if stt_ws is not None:
                return
            stt_ws = await SarvamVoiceService.connect_stt(
                tenant_res,
                language=language,
                sample_rate=sample_rate,
            )

            async def _reader() -> None:
                nonlocal current_turn
                async for raw in stt_ws:
                    parsed = SarvamVoiceService.parse_stt_message(raw)
                    if not parsed:
                        continue
                    event_type = parsed.get("type")
                    if event_type == "speech_start":
                        if current_turn and not current_turn.done():
                            current_turn.cancel()
                        await websocket.send_json({"type": "barge_in_detected", "call_id": call_id})
                        continue
                    text = str(parsed.get("text") or "").strip()
                    if not text:
                        continue
                    await websocket.send_json(
                        {
                            "type": "stt_transcript",
                            "text": text,
                            "is_final": bool(parsed.get("is_final")),
                            "language": parsed.get("language"),
                        }
                    )
                    if parsed.get("is_final"):
                        if current_turn and not current_turn.done():
                            current_turn.cancel()
                        turn_language = SarvamVoiceService.normalize_language(parsed.get("language") or language)
                        current_turn = asyncio.create_task(
                            NokvoOneVoiceStreamService._run_text_turn(
                                websocket,
                                tenant_res,
                                text,
                                db=db,
                                language=turn_language,
                                call_id=call_id,
                                company_name=company_name,
                                campaign_context=campaign_context,
                                source="sarvam_stt",
                            )
                        )

            stt_reader_task = asyncio.create_task(_reader())

        await NokvoOneVoiceStreamService._emit_runtime_status(websocket, tenant_res)
        await AgentSessionStore.set_state(
            tenant_res,
            call_id,
            {
                "status": "connected",
                "language": language,
                "campaign_id": (campaign_context or {}).get("campaign_id"),
            },
        )
        await websocket.send_json({"type": "voice_session_ready", "call_id": call_id})

        opening = (campaign_context or {}).get("opening_message")
        if opening:
            await NokvoOneVoiceStreamService._run_text_turn(
                websocket,
                tenant_res,
                opening,
                db=db,
                language=language,
                call_id=call_id,
                company_name=company_name,
                campaign_context=campaign_context,
                source="campaign_opening",
            )

        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    chunk = message.get("bytes") or b""
                    audio_buffer.extend(chunk)
                    try:
                        await _start_stt()
                        await SarvamVoiceService.send_stt_audio(stt_ws, chunk, sample_rate=sample_rate)
                    except Exception as exc:
                        await websocket.send_json(
                            {
                                "type": "stt_error",
                                "error_message": str(exc)[:220],
                                "fallback": "audio will be transcribed on finalize when possible",
                            }
                        )
                    continue

                raw_text = message.get("text")
                if raw_text is None:
                    continue
                try:
                    payload = json.loads(raw_text)
                except json.JSONDecodeError:
                    continue
                event_type = payload.get("type")
                if event_type == "config":
                    language = SarvamVoiceService.normalize_language(str(payload.get("language") or language))
                    await NokvoOneVoiceStreamService._emit_runtime_status(websocket, tenant_res)
                    continue
                if event_type in {"text_query", "transcript"}:
                    if current_turn and not current_turn.done():
                        current_turn.cancel()
                    current_turn = asyncio.create_task(
                        NokvoOneVoiceStreamService._run_text_turn(
                            websocket,
                            tenant_res,
                            str(payload.get("text") or ""),
                            db=db,
                            language=SarvamVoiceService.normalize_language(str(payload.get("language") or language)),
                            call_id=call_id,
                            company_name=company_name,
                            campaign_context=campaign_context,
                            source="manual",
                        )
                    )
                    continue
                if event_type in {"finalize", "stop"}:
                    if stt_ws is not None:
                        try:
                            await SarvamVoiceService.flush_stt(stt_ws)
                        except Exception:
                            pass
                    elif audio_buffer:
                        try:
                            stt = await SarvamVoiceService.transcribe_rest(
                                tenant_res,
                                bytes(audio_buffer),
                                language=language,
                            )
                            text = stt.get("transcript") or ""
                            if text:
                                await NokvoOneVoiceStreamService._run_text_turn(
                                    websocket,
                                    tenant_res,
                                    text,
                                    db=db,
                                    language=SarvamVoiceService.normalize_language(stt.get("language") or language),
                                    call_id=call_id,
                                    company_name=company_name,
                                    campaign_context=campaign_context,
                                    source="sarvam_rest_stt",
                                )
                        except Exception as exc:
                            await websocket.send_json({"type": "stt_error", "error_message": str(exc)[:220]})
                    if event_type == "stop":
                        break
        finally:
            if current_turn and not current_turn.done():
                current_turn.cancel()
                try:
                    await current_turn
                except BaseException:
                    pass
            if stt_reader_task and not stt_reader_task.done():
                stt_reader_task.cancel()
            if stt_ws is not None:
                try:
                    await stt_ws.close()
                except Exception:
                    pass
