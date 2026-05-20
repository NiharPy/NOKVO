from __future__ import annotations

import asyncio
import json
import struct
import uuid
from time import perf_counter
from typing import Any


def _pcm16le_to_wav(pcm: bytes, *, sample_rate: int, channels: int = 1) -> bytes:
    """Wrap raw 16-bit little-endian PCM mono audio in a minimal WAV header.

    Used by the cross-lingual retrieval path: the translate-STT endpoint
    accepts a wav container; the streaming STT path captures raw PCM. This
    avoids pulling in a wave/ffmpeg dep just to add 44 bytes of header.
    """
    bits_per_sample = 16
    byte_rate = sample_rate * channels * bits_per_sample // 8
    block_align = channels * bits_per_sample // 8
    data_size = len(pcm)
    riff_size = 36 + data_size
    header = struct.pack(
        "<4sI4s4sIHHIIHH4sI",
        b"RIFF", riff_size, b"WAVE",
        b"fmt ", 16, 1, channels, sample_rate, byte_rate, block_align, bits_per_sample,
        b"data", data_size,
    )
    return header + pcm

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.services.agent_session_store import AgentSessionStore
from app.services.language_intent import detect_language_switch
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.predefined_tools_service import PredefinedToolsService, get_tool
from app.services.prosody import DEFAULT_TONE, ProsodyChunk, prosody_for, stream_prosody_chunks
from app.services.sarvam_voice_service import SarvamVoiceService


# Short utterances the user produces while the agent is still composing the
# previous answer. We acknowledge with a short "yes, one moment…" instead of
# treating these as barge-ins — the pending answer continues unmodified.
_CHECK_IN_EXACT = {
    "hello", "hello?", "hellooo", "hi", "hi?", "hey", "hey?",
    "hello hello", "hello are you there", "are you there",
    "are you there?", "you there", "you there?",
    "still there", "still there?", "you still there",
    "anyone there", "anyone there?", "can you hear me",
    "can you hear me?", "you hear me", "you listening",
    "are you listening", "hello can you hear me",
    "हलो", "हैलो", "हेलो", "हैलो?", "क्या आप हैं",
    "क्या आप वहाँ हैं", "हो क्या", "क्या आप सुन रहे हैं",
    "హలో", "హలో?", "ఉన్నారా", "మీరు ఉన్నారా",
    "ஹலோ", "இருக்கீங்களா", "கேக்குதா",
}
_CHECK_IN_CONTAINS = (
    "are you there",
    "you still there",
    "can you hear me",
    "are you listening",
    "anyone there",
    "क्या आप हैं",
    "सुन रहे हैं",
    "మీరు ఉన్నారా",
    "இருக்கீங்களா",
)


def _is_check_in_utterance(text: str) -> bool:
    cleaned = " ".join((text or "").lower().split())
    cleaned = cleaned.rstrip("?.,!।؟")
    if not cleaned:
        return False
    if len(cleaned.split()) > 6:
        return False
    if cleaned in _CHECK_IN_EXACT:
        return True
    return any(needle in cleaned for needle in _CHECK_IN_CONTAINS)


def _quick_ack_text(language: str | None) -> str:
    return {
        "hi": "जी हाँ, सुन रहा हूँ।",
        "ta": "ஆமா, கேக்குறேன்.",
        "te": "అవును, వింటున్నాను.",
        "bn": "হ্যাঁ, শুনছি।",
        "kn": "ಹೌದು, ಕೇಳ್ತಾ ಇದೀನಿ.",
        "ml": "അതെ, കേൾക്കുന്നു.",
        "mr": "हो, ऐकतोय.",
        "gu": "હા, સાંભળું છું.",
        "pa": "ਹਾਂ, ਸੁਣ ਰਿਹਾ ਹਾਂ।",
    }.get(language or "en", "Yes, I'm here.")


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
    def _inbound_opening_text(language: str | None) -> str:
        lang = SarvamVoiceService.normalize_language(language)
        if lang == "te":
            return "హలో, మీరు మీకు సౌకర్యమైన భాషలో మాట్లాడండి. నేను అదే భాషలో కొనసాగిస్తాను. నేను ఎలా సహాయం చేయగలను?"
        if lang == "hi":
            return "नमस्ते, आप जिस भाषा में सहज हैं उसमें बात कीजिए। मैं उसी भाषा में आगे बात करूंगा। मैं कैसे मदद कर सकता हूं?"
        return "Hello, please speak in the language you're comfortable with. I'll continue in that language. How can I help?"

    @staticmethod
    async def _load_recent_record_for_phone(
        db: AsyncSession | None,
        organization_id: Any,
        phone: str | None,
    ) -> dict[str, Any] | None:
        """Look up the most recent open record for a returning caller. Returns
        a small summary dict (record_type, name, reason/summary, when) or None.
        Used to enrich the inbound opener: "Hi again — still about your eye
        blurriness checkup, or something new?"."""
        if db is None or not phone or organization_id is None:
            return None
        from app.models.nokvo_one_tool_record import NokvoOneToolRecord
        from sqlalchemy import select

        # Normalise the phone: keep last 10 digits since that's what most
        # records use for Indian numbers.
        digits = "".join(c for c in str(phone) if c.isdigit())
        if len(digits) < 10:
            return None
        suffix = digits[-10:]
        OPEN_STATUSES = ("new", "requested", "assigned", "open", "scheduled", "in_progress")
        stmt = (
            select(NokvoOneToolRecord)
            .where(NokvoOneToolRecord.organization_id == organization_id)
            .where(NokvoOneToolRecord.status.in_(OPEN_STATUSES))
            .order_by(NokvoOneToolRecord.created_at.desc())
            .limit(20)
        )
        try:
            res = await db.execute(stmt)
        except Exception:
            return None
        for rec in res.scalars().all():
            phone_value = "".join(c for c in str(rec.contact_phone or "") if c.isdigit())
            if not phone_value:
                # Fall back to data.phone / data.contact_phone
                data = rec.data or {}
                phone_value = "".join(c for c in str(data.get("phone") or data.get("contact_phone") or "") if c.isdigit())
            if phone_value and phone_value[-10:] == suffix:
                data = rec.data or {}
                return {
                    "record_id": str(rec.id),
                    "record_type": rec.record_type,
                    "status": rec.status,
                    "name": (
                        data.get("patient_name")
                        or data.get("name")
                        or data.get("customer_name")
                        or data.get("guest_name")
                    ),
                    "summary": (
                        data.get("reason")
                        or data.get("care_need")
                        or data.get("subject")
                        or data.get("issue_summary")
                        or data.get("description")
                        or data.get("notes")
                    ),
                    "when": (
                        data.get("appointment_time")
                        or data.get("visit_at")
                        or data.get("callback_at")
                        or data.get("requested_time")
                    ),
                }
        return None

    @staticmethod
    def _returning_caller_opener(
        record: dict[str, Any],
        language: str | None,
        *,
        outcome_history: list[dict[str, Any]] | None = None,
    ) -> str:
        lang = SarvamVoiceService.normalize_language(language)
        name = str(record.get("name") or "").strip()
        summary = str(record.get("summary") or "").strip()
        rt = str(record.get("record_type") or "")
        topic = summary or {
            "appointment": "your appointment",
            "lead": "your enquiry",
            "ticket": "your support ticket",
            "callback": "the callback we have on file",
            "request": "your previous request",
        }.get(rt, "your previous request")
        # Adapt tone to outcome history: if the caller no-showed last time,
        # the opener acknowledges that gently and offers a reminder SMS.
        last_outcome = None
        for entry in outcome_history or []:
            status = ((entry or {}).get("outcome") or {}).get("status")
            if status:
                last_outcome = status
                break
        if last_outcome in {"no_show", "failed_followup"}:
            if lang == "te":
                prefix = f"హలో {name} గారు — last time miss అయ్యింది."
                return f"{prefix} ఈసారి appointment confirm చేయడానికి reminder pampāli ah?"
            if lang == "hi":
                prefix = f"नमस्ते {name} ji — पिछली बार miss हो गया था।"
                return f"{prefix} इस बार reminder SMS भेज दूँ कन्फर्म करने के लिए?"
            return (
                f"Hi {name} — last time the visit got missed. "
                "Want me to send a reminder SMS this time so it doesn't slip?"
            )
        if lang == "te":
            if name:
                return f"హలో {name} గారు — ఇది ఇంకా {topic} గురించేనా, లేక కొత్త విషయమా?"
            return f"హలో — ఇది ఇంకా {topic} గురించేనా, లేక కొత్త విషయమా?"
        if lang == "hi":
            if name:
                return f"नमस्ते {name} ji — क्या ये अभी भी {topic} के बारे में है, या कुछ नया?"
            return f"नमस्ते — क्या ये अभी भी {topic} के बारे में है, या कुछ नया?"
        if name:
            return f"Hi {name} — still about {topic}, or something new?"
        return f"Hi again — still about {topic}, or something new?"

    @staticmethod
    async def _log_voice_call(
        db: AsyncSession | None,
        tenant_res: TenantResources,
        call_id: str | None,
        *,
        duration_seconds: int,
        campaign_context: dict[str, Any] | None = None,
    ) -> None:
        if db is None or not call_id:
            return
        history = await AgentSessionStore.get_history(tenant_res, call_id)
        if not history:
            return
        lines: list[str] = []
        for item in history[-16:]:
            role = str(item.get("role") or "turn").strip() or "turn"
            content = " ".join(str(item.get("content") or "").split())
            if content:
                lines.append(f"{role}: {content}")
        if not lines:
            return
        contact = (campaign_context or {}).get("contact") or {}
        args: dict[str, Any] = {
            "channel": "voice",
            "summary": "\n".join(lines)[-4000:],
            "outcome": "completed",
            "duration_seconds": max(0, duration_seconds),
        }
        if isinstance(contact, dict):
            if contact.get("name"):
                args["contact_name"] = str(contact["name"])
            if contact.get("phone"):
                args["contact_phone"] = str(contact["phone"])
            if contact.get("email"):
                args["contact_email"] = str(contact["email"])
        tool = get_tool("call_log_create")
        if tool is None:
            return
        try:
            await PredefinedToolsService.execute(
                db,
                tenant_res.organization_id,
                None,
                tool,
                args,
                session_id=f"{call_id}:call_log",
            )
            await db.commit()
        except Exception:
            await db.rollback()

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
        retrieval_text: str | None = None,
        turn_state: dict[str, Any] | None = None,
    ) -> None:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return
        turn_id = str(uuid.uuid4())[:8]
        started = perf_counter()

        # Language-switch intent: caller said something like "speak in Telugu"
        # or "Telugu please". Override the reply language for this turn, and
        # notify the frontend so the UI reflects the new locked language.
        requested_lang = detect_language_switch(cleaned)
        pure_switch_request = False
        if requested_lang:
            normalized = SarvamVoiceService.normalize_language(requested_lang)
            if normalized != language:
                language = normalized
                await websocket.send_json({"type": "language_locked", "language": language})
            # If the whole utterance is just a switch request (short, no
            # substantive question), acknowledge it warmly instead of
            # putting it through the normal pipeline — otherwise the
            # caller hears "Sorry, I missed that" or worse.
            if len(cleaned.split()) <= 7:
                pure_switch_request = True

        def _mark_speaking() -> None:
            if turn_state is not None:
                turn_state["speaking"] = True

        if pure_switch_request:
            ack = {
                "hi": "ज़रूर, हिंदी में बात करते हैं। बताइए, मैं कैसे मदद कर सकता हूँ?",
                "ta": "சரி, தமிழில் பேசலாம். எப்படி உதவ முடியும்?",
                "te": "సరే, తెలుగులో మాట్లాడదాం. ఎలా సహాయం చేయగలను?",
                "bn": "ঠিক আছে, বাংলায় কথা বলব। আমি কীভাবে সাহায্য করতে পারি?",
                "kn": "ಸರಿ, ಕನ್ನಡದಲ್ಲಿ ಮಾತಾಡೋಣ. ನಾನು ಹೇಗೆ ಸಹಾಯ ಮಾಡಬಲ್ಲೆ?",
                "ml": "ശരി, മലയാളത്തിൽ സംസാരിക്കാം. എങ്ങനെ സഹായിക്കാം?",
                "mr": "ठीक आहे, मराठीत बोलूया. मी कशी मदत करू?",
                "gu": "બરાબર, ગુજરાતીમાં વાત કરીએ. કેવી રીતે મદદ કરી શકું?",
                "pa": "ਠੀਕ ਹੈ, ਪੰਜਾਬੀ ਵਿੱਚ ਗੱਲ ਕਰੀਏ। ਮੈਂ ਕਿਵੇਂ ਮਦਦ ਕਰਾਂ?",
            }.get(language, "Sure, let's switch. How can I help you?")
            await websocket.send_json(
                {"type": "stt_finished", "text": cleaned, "turn_id": turn_id, "source": source}
            )
            await websocket.send_json(
                {
                    "type": "agent_sentence",
                    "turn_id": turn_id,
                    "sentence": ack,
                    "tone": "warm",
                    "first_sentence_ms": int((perf_counter() - started) * 1000),
                    "cache_hit": False,
                    "source": "language_switch_ack",
                }
            )
            prosody = prosody_for("warm")
            _mark_speaking()
            try:
                await SarvamVoiceService.stream_sentence_tts(
                    websocket,
                    tenant_res,
                    ack,
                    language=language,
                    purpose="language_switch",
                    pace=prosody.pace,
                    pitch=prosody.pitch,
                    loudness=prosody.loudness,
                )
            except Exception:
                pass
            await AgentSessionStore.append_turn(tenant_res, call_id, cleaned, ack)
            await websocket.send_json(
                {
                    "type": "agent_answer",
                    "turn_id": turn_id,
                    "answer": ack,
                    "refused": False,
                    "citations": [],
                    "chunks": [],
                    "runtime": {"mode": "language_switch_ack"},
                    "intent": {"type": "language_switch", "should_retrieve": False},
                }
            )
            await websocket.send_json(
                {
                    "type": "turn_complete",
                    "turn_id": turn_id,
                    "total_ms": int((perf_counter() - started) * 1000),
                    "context_source": "language_switch_ack",
                    "filler_played": False,
                }
            )
            return

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
                retrieval_text=retrieval_text,
            ):
                if event.get("type") == "sentence":
                    sentence = str(event.get("text") or "").strip()
                    if not sentence:
                        continue
                    if first_sentence_ms is None:
                        first_sentence_ms = int((perf_counter() - started) * 1000)
                    tone = str(event.get("tone") or DEFAULT_TONE)
                    answer_parts.append(sentence)
                    await websocket.send_json(
                        {
                            "type": "agent_sentence",
                            "turn_id": turn_id,
                            "sentence": sentence,
                            "tone": tone,
                            "first_sentence_ms": first_sentence_ms,
                            "cache_hit": bool(event.get("cache_hit")),
                        }
                    )
                    prosody = prosody_for(tone)
                    _mark_speaking()
                    try:
                        await SarvamVoiceService.stream_sentence_tts(
                            websocket,
                            tenant_res,
                            sentence,
                            language=language,
                            purpose="answer",
                            pace=prosody.pace,
                            pitch=prosody.pitch,
                            loudness=prosody.loudness,
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
                "tool_calls": (final_payload or {}).get("tool_calls") or [],
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
    async def _process_blob_utterance(
        websocket: WebSocket,
        tenant_res: TenantResources,
        audio_bytes: bytes,
        *,
        db: AsyncSession | None,
        fallback_language: str,
        call_id: str | None,
        company_name: str | None,
        campaign_context: dict[str, Any] | None,
        session_locked_language: list[str | None],
        prev_turn: asyncio.Task | None = None,
        prev_turn_state: dict[str, Any] | None = None,
        turn_state: dict[str, Any] | None = None,
    ) -> None:
        """One Blob → one turn. Used by the frontend-VAD ("vad_blob") capture
        mode where the browser handles end-of-utterance detection and sends
        a complete recorded WebM/Opus blob per turn."""
        if not audio_bytes:
            return
        # The frontend now ships 16-kHz mono WAV blobs (it used to send
        # WebM/Opus via MediaRecorder, which dropped leading phonemes because
        # the recorder spun up *after* VAD detection). We sniff RIFF magic so
        # legacy clients still work.
        is_wav = audio_bytes.startswith(b"RIFF") and audio_bytes[8:12] == b"WAVE"
        filename = "utt.wav" if is_wav else "utt.webm"
        content_type = "audio/wav" if is_wav else "audio/webm"

        # Run native + translate STT concurrently. Cap each so a slow Sarvam
        # response can't blow the first-sentence latency budget.
        async def _native() -> dict[str, Any]:
            return await SarvamVoiceService.transcribe_rest(
                tenant_res,
                audio_bytes,
                filename=filename,
                content_type=content_type,
            )

        async def _translated() -> str:
            if not settings.AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED:
                return ""
            try:
                result = await asyncio.wait_for(
                    SarvamVoiceService.transcribe_translate(
                        tenant_res,
                        audio_bytes,
                        filename=filename,
                        content_type=content_type,
                    ),
                    timeout=1.5,
                )
                return str(result.get("transcript") or "").strip()
            except asyncio.TimeoutError:
                print("[NOKVO-TRANSLATE] timeout in vad_blob mode; using native only")
                return ""
            except Exception as exc:
                print(f"[NOKVO-TRANSLATE] failed: {exc!r}")
                return ""

        native_task = asyncio.create_task(_native())
        translate_task = asyncio.create_task(_translated())
        try:
            native_result = await native_task
            transcript = (native_result.get("transcript") or "").strip()
            if NokvoOneVoicePipeline.should_skip_translate_for_native_query(transcript):
                translate_task.cancel()
                try:
                    await translate_task
                except asyncio.CancelledError:
                    pass
                english_text = ""
            else:
                english_text = await translate_task
        except asyncio.CancelledError:
            translate_task.cancel()
            return
        except Exception as exc:
            if not native_task.done():
                native_task.cancel()
            if not translate_task.done():
                translate_task.cancel()
            err_text = str(exc)
            is_rate_limit = "429" in err_text or "rate limit" in err_text.lower()
            payload: dict[str, Any] = {
                "type": "stt_error",
                "error_message": err_text[:220],
                "provider": "sarvam",
            }
            if is_rate_limit:
                payload["rate_limited"] = True
                payload["user_message"] = (
                    "The voice transcription service is rate-limited. "
                    "Please try speaking again in a moment."
                )
                print(f"[NOKVO-VOICE] STT rate-limited after retries: {err_text[:200]!r}")
            await websocket.send_json(payload)
            return

        raw_detected_language = native_result.get("language")
        detected_lang = SarvamVoiceService.normalize_language(raw_detected_language or fallback_language)
        if not transcript:
            await websocket.send_json({"type": "stt_empty"})
            return

        # Echo the transcript to the frontend so the UI shows what the caller
        # said before the agent reply lands.
        await websocket.send_json(
            {
                "type": "stt_transcript",
                "text": transcript,
                "is_final": True,
                "language": detected_lang,
            }
        )

        # If the previous turn is still composing (no TTS audio sent yet) and
        # the user just said "hello, are you there?" — acknowledge with a quick
        # "yes" and keep the queued answer running. The original reply will
        # play right after the ack.
        prev_alive = (
            prev_turn is not None
            and not prev_turn.done()
            and not (prev_turn_state or {}).get("speaking")
        )
        if prev_alive and _is_check_in_utterance(transcript):
            ack_lang = session_locked_language[0] or detected_lang
            ack = _quick_ack_text(ack_lang)
            await websocket.send_json(
                {
                    "type": "agent_sentence",
                    "turn_id": f"ack-{uuid.uuid4().hex[:8]}",
                    "sentence": ack,
                    "tone": "warm",
                    "cache_hit": False,
                    "source": "check_in_ack",
                }
            )
            try:
                await SarvamVoiceService.stream_sentence_tts(
                    websocket,
                    tenant_res,
                    ack,
                    language=ack_lang,
                    purpose="check_in_ack",
                )
            except Exception:
                pass
            return

        # Real new turn — cancel the previous and run the new answer.
        if prev_turn is not None and not prev_turn.done():
            prev_turn.cancel()

        # Resolve reply language with the same precedence as the streaming path.
        requested = detect_language_switch(transcript)
        if requested:
            normalized = SarvamVoiceService.normalize_language(requested)
            if normalized != session_locked_language[0]:
                session_locked_language[0] = normalized
                await websocket.send_json({"type": "language_locked", "language": normalized})
            turn_language = normalized
        elif session_locked_language[0]:
            turn_language = session_locked_language[0]
        elif raw_detected_language:
            session_locked_language[0] = detected_lang
            await websocket.send_json({"type": "language_locked", "language": detected_lang})
            turn_language = detected_lang
        else:
            turn_language = detected_lang

        await NokvoOneVoiceStreamService._run_text_turn(
            websocket,
            tenant_res,
            transcript,
            db=db,
            language=turn_language,
            call_id=call_id,
            company_name=company_name,
            campaign_context=campaign_context,
            source="sarvam_rest_vad_blob",
            retrieval_text=english_text or None,
            turn_state=turn_state,
        )

    @staticmethod
    async def _play_opener(
        websocket: WebSocket,
        tenant_res: TenantResources,
        opening_text: str,
        *,
        language: str,
        call_id: str | None = None,
        campaign_context: dict[str, Any] | None = None,
    ) -> None:
        """Deterministic prosody-aware opener — no LLM round-trip.

        Saves ~150ms of perceived latency on outbound campaign calls vs.
        sending the opener through ``_run_text_turn``. The opening text may
        contain prosody tags (``[warm]…[/warm] [neutral]…[/neutral]``); if
        none are present, the whole opener is voiced as ``[warm]``.
        """
        text = (opening_text or "").strip()
        if not text:
            return
        if "[" not in text or "]" not in text:
            text = f"[warm]{text}[/warm]"
        turn_id = str(uuid.uuid4())[:8]
        started = perf_counter()
        await websocket.send_json({"type": "agent_thinking", "turn_id": turn_id, "query": "(opener)"})

        async def _single_chunk_stream():
            yield text

        first_sentence_ms: int | None = None
        answer_parts: list[str] = []
        async for chunk in stream_prosody_chunks(_single_chunk_stream()):
            sentence = chunk.text.strip()
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
                    "tone": chunk.tone,
                    "first_sentence_ms": first_sentence_ms,
                    "cache_hit": False,
                    "source": "campaign_opener",
                }
            )
            prosody = prosody_for(chunk.tone)
            try:
                await SarvamVoiceService.stream_sentence_tts(
                    websocket,
                    tenant_res,
                    sentence,
                    language=language,
                    purpose="opener",
                    pace=prosody.pace,
                    pitch=prosody.pitch,
                    loudness=prosody.loudness,
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

        # Record the opener as an assistant turn so the LLM sees it as
        # context for the caller's first reply.
        joined = " ".join(answer_parts).strip()
        if joined:
            await AgentSessionStore.append_turn(tenant_res, call_id, "(call opener)", joined)
        await websocket.send_json(
            {
                "type": "agent_answer",
                "turn_id": turn_id,
                "answer": joined,
                "refused": False,
                "citations": [],
                "chunks": [],
                "runtime": {"mode": "deterministic_opener"},
                "intent": {"type": "campaign_opener", "should_retrieve": False},
            }
        )
        await websocket.send_json(
            {
                "type": "turn_complete",
                "turn_id": turn_id,
                "total_ms": int((perf_counter() - started) * 1000),
                "context_source": "deterministic_opener",
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
        session_started = perf_counter()
        language = SarvamVoiceService.normalize_language(language)
        call_id = call_id or str(uuid.uuid4())
        company_name = await NokvoOneVoiceStreamService._company_name(db, tenant_res)
        current_turn: asyncio.Task | None = None
        # Mutable state shared with the in-flight _run_text_turn so the dispatcher
        # can tell whether the answer has begun streaming TTS. Used to decide
        # whether a fresh utterance is a barge-in or a "hello, are you there?"
        # check-in arriving during the agent's composing latency.
        turn_state: dict[str, Any] = {"speaking": False}
        stt_ws: Any = None
        stt_reader_task: asyncio.Task | None = None
        audio_buffer = bytearray()
        sample_rate = int(settings.SARVAM_STT_SAMPLE_RATE)
        # Capture mode:
        #   "stream"   (default) — frontend streams raw PCM, server uses
        #                          Sarvam streaming STT WS with EOU debouncing.
        #   "vad_blob"           — frontend does VAD client-side and sends one
        #                          complete utterance Blob per turn. Server
        #                          dispatches each Blob to Sarvam REST STT.
        # The VAD-blob mode eliminates server-side EOU guesswork — the
        # speaker's own microphone-silence decides when they're done. This is
        # what agent_lab uses and what proves robust for real conversational
        # speech with mid-thought pauses. See run_session() docstring.
        capture_mode: list[str] = ["stream"]

        # End-of-utterance buffering. Sarvam emits speech_end whenever its VAD
        # detects a pause — but speakers naturally pause mid-thought every few
        # words, especially in Indian-language code-switched speech. So
        # speech_end is treated as a HINT (restart the debounce), not as
        # authority to fire. The turn only fires after the debounce elapses
        # with no new speech — i.e., the user actually finished their thought.
        EOU_DEBOUNCE_MS = 2000
        EOU_CONTINUATION_BONUS_MS = 1500  # added when the buffer ends in a function word
        # Trailing words that almost always precede more speech. Bumping the
        # debounce when the buffer ends in one of these saves us from cutting
        # off thoughts like "Basically he asked me to" / "you guys would be".
        _CONTINUATION_TAIL_WORDS = {
            "a", "an", "and", "but", "or", "to", "of", "in", "on", "at", "by", "for",
            "with", "if", "when", "while", "because", "so", "that", "the", "this",
            "these", "those", "i", "he", "she", "we", "they", "it", "you", "my",
            "your", "his", "her", "our", "their", "is", "are", "was", "were", "be",
            "been", "being", "have", "has", "had", "do", "does", "did", "would",
            "could", "should", "will", "shall", "may", "might", "uh", "um", "uhm",
            "hmm", "like", "as", "from", "than", "then", "where", "why", "how",
        }
        utterance_segments: list[str] = []
        utterance_language: list[str] = [language]
        eou_timer_task: asyncio.Task | None = None
        # Sticky language lock: when the user explicitly asks to switch
        # ("speak in Telugu", "Hindi please") we lock that choice for the
        # rest of the session. Without this, Sarvam's per-segment language
        # detection flaps the reply language mid-conversation, especially on
        # code-switched utterances.
        session_locked_language: list[str | None] = [None]
        utterance_language_detected: list[bool] = [False]
        inbound_opener_played: list[bool] = [False]
        inbound_opener_task: asyncio.Task | None = None

        async def _play_default_inbound_opener() -> None:
            if inbound_opener_played[0] or (campaign_context or {}).get("opening_message"):
                return
            inbound_opener_played[0] = True
            # Returning-caller awareness: if the caller's phone is known and
            # matches an open record, switch to a context-aware greeting.
            opening_text = NokvoOneVoiceStreamService._inbound_opening_text(language)
            caller_phone = (
                (campaign_context or {}).get("contact", {}).get("phone")
                if isinstance((campaign_context or {}).get("contact"), dict)
                else None
            ) or (campaign_context or {}).get("from_phone")
            if caller_phone:
                try:
                    record = await NokvoOneVoiceStreamService._load_recent_record_for_phone(
                        db, tenant_res.organization_id, caller_phone
                    )
                except Exception:
                    record = None
                # Outcome history (#32): if a recent visit was a no-show or
                # failed_followup, the opener gets a softer, reminder-aware
                # tone. The agent learns from past calls instead of greeting
                # a no-show caller the same as a first-timer.
                outcome_history: list[dict[str, Any]] = []
                try:
                    from app.services.outcome_tracker import OutcomeTracker

                    outcome_history = await OutcomeTracker.recent_outcomes_for_caller(
                        db,
                        organization_id=tenant_res.organization_id,
                        phone=caller_phone,
                        limit=3,
                    )
                except Exception:
                    outcome_history = []
                if record:
                    opening_text = NokvoOneVoiceStreamService._returning_caller_opener(
                        record, language, outcome_history=outcome_history,
                    )
            await NokvoOneVoiceStreamService._play_opener(
                websocket,
                tenant_res,
                opening_text,
                language=language,
                call_id=call_id,
                campaign_context=campaign_context,
            )

        # Per-utterance audio buffer (separate from the call-long ``audio_buffer``).
        # Reset on speech_start so each turn's translate-STT call sees only the
        # current utterance. Used when AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED.
        utterance_audio = bytearray()

        async def _fire_turn() -> None:
            nonlocal current_turn, turn_state
            text = " ".join(s for s in utterance_segments if s).strip()
            if not text:
                return
            utterance_segments.clear()

            # "Hello, are you there?" while the previous answer is still being
            # composed: keep the queued reply running and inject a quick "yes"
            # ack. Only valid if the prior turn hasn't started speaking yet.
            prev_turn = current_turn
            prev_state = turn_state
            if (
                prev_turn is not None
                and not prev_turn.done()
                and not (prev_state or {}).get("speaking")
                and _is_check_in_utterance(text)
            ):
                ack_lang = session_locked_language[0] or utterance_language[0]
                ack = _quick_ack_text(ack_lang)
                await websocket.send_json(
                    {
                        "type": "agent_sentence",
                        "turn_id": f"ack-{uuid.uuid4().hex[:8]}",
                        "sentence": ack,
                        "tone": "warm",
                        "cache_hit": False,
                        "source": "check_in_ack",
                    }
                )
                try:
                    await SarvamVoiceService.stream_sentence_tts(
                        websocket,
                        tenant_res,
                        ack,
                        language=ack_lang,
                        purpose="check_in_ack",
                    )
                except Exception:
                    pass
                utterance_audio.clear()
                return

            # Resolve the reply language. Priority:
            #   1) Explicit switch request in THIS turn ("speak in Telugu")
            #   2) Previously-locked session language (sticky)
            #   3) Sarvam's per-segment STT language detection
            requested = detect_language_switch(text)
            if requested:
                normalized = SarvamVoiceService.normalize_language(requested)
                if normalized != session_locked_language[0]:
                    session_locked_language[0] = normalized
                    await websocket.send_json({"type": "language_locked", "language": normalized})
                turn_language = normalized
            elif session_locked_language[0]:
                turn_language = session_locked_language[0]
            elif utterance_language_detected[0]:
                session_locked_language[0] = utterance_language[0]
                turn_language = utterance_language[0]
                await websocket.send_json({"type": "language_locked", "language": turn_language})
            else:
                turn_language = utterance_language[0]
            utterance_language_detected[0] = False

            # Cross-lingual retrieval: when enabled AND the utterance isn't already
            # English, dispatch a parallel translate-STT call on the per-utterance
            # audio. The LLM gets the native transcript (preserves caller's exact
            # words); the embedding query uses the English translation (matches an
            # English doc corpus, ~4× better cosine recall in practice).
            retrieval_text: str | None = None
            translate_audio: bytes | None = None
            if (
                settings.AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED
                and turn_language != "en"
                and utterance_audio
                and not NokvoOneVoicePipeline.should_skip_translate_for_native_query(text)
            ):
                translate_audio = bytes(utterance_audio)
            utterance_audio.clear()

            if current_turn and not current_turn.done():
                current_turn.cancel()
            turn_state = {"speaking": False}
            new_state = turn_state

            if translate_audio:
                # Kick the translate call with a hard timeout. We'd rather
                # use the native transcript than wait 3-4s for translate to
                # finish — first-sentence latency on a phone call has to stay
                # under ~2s for the agent to feel responsive.
                TRANSLATE_TIMEOUT_S = 1.5

                async def _run_with_translate() -> None:
                    english = ""
                    try:
                        wav_bytes = _pcm16le_to_wav(translate_audio, sample_rate=sample_rate)
                        translate_result = await asyncio.wait_for(
                            SarvamVoiceService.transcribe_translate(
                                tenant_res, wav_bytes, filename="utt.wav", content_type="audio/wav",
                            ),
                            timeout=TRANSLATE_TIMEOUT_S,
                        )
                        english = (translate_result.get("transcript") or "").strip()
                    except asyncio.TimeoutError:
                        # Don't block the user on a slow translate — proceed
                        # with the native transcript for retrieval.
                        print(f"[NOKVO-TRANSLATE] timeout after {TRANSLATE_TIMEOUT_S}s; falling back to native")
                    except Exception as exc:
                        try:
                            await websocket.send_json(
                                {
                                    "type": "translate_stt_error",
                                    "error_message": str(exc)[:240],
                                }
                            )
                        except Exception:
                            pass
                    await NokvoOneVoiceStreamService._run_text_turn(
                        websocket,
                        tenant_res,
                        text,
                        db=db,
                        language=turn_language,
                        call_id=call_id,
                        company_name=company_name,
                        campaign_context=campaign_context,
                        source="sarvam_stt",
                        retrieval_text=english or None,
                        turn_state=new_state,
                    )

                current_turn = asyncio.create_task(_run_with_translate())
            else:
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
                        turn_state=new_state,
                    )
                )

        def _cancel_eou_timer() -> None:
            nonlocal eou_timer_task
            if eou_timer_task and not eou_timer_task.done():
                eou_timer_task.cancel()
            eou_timer_task = None

        def _eou_delay_ms() -> int:
            """Base debounce + a bonus when the buffer trails off on a
            function word (continuation cue)."""
            if not utterance_segments:
                return EOU_DEBOUNCE_MS
            tail = utterance_segments[-1].strip().lower()
            # Strip trailing punctuation if any sneaks through.
            tail = tail.rstrip(".,!?;:'\"`")
            last_word = tail.split()[-1] if tail else ""
            if last_word in _CONTINUATION_TAIL_WORDS:
                return EOU_DEBOUNCE_MS + EOU_CONTINUATION_BONUS_MS
            return EOU_DEBOUNCE_MS

        def _restart_eou_timer() -> None:
            nonlocal eou_timer_task
            _cancel_eou_timer()
            delay_ms = _eou_delay_ms()

            async def _timer() -> None:
                try:
                    await asyncio.sleep(delay_ms / 1000)
                    await _fire_turn()
                except asyncio.CancelledError:
                    pass

            eou_timer_task = asyncio.create_task(_timer())

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
                try:
                    async for raw in stt_ws:
                        parsed = SarvamVoiceService.parse_stt_message(raw)
                        if not parsed:
                            continue
                        event_type = parsed.get("type")
                        if event_type == "speech_start":
                            turn_alive = current_turn is not None and not current_turn.done()
                            agent_speaking = turn_alive and bool((turn_state or {}).get("speaking"))
                            if agent_speaking:
                                # True barge-in: agent is already playing audio,
                                # the caller is talking over it.
                                current_turn.cancel()
                                _cancel_eou_timer()
                                utterance_segments.clear()
                                utterance_audio.clear()
                                await websocket.send_json({"type": "barge_in_detected", "call_id": call_id})
                            else:
                                # Either the agent hasn't started speaking yet
                                # (still composing the previous answer) OR no
                                # turn is in flight. Don't cancel — the user
                                # might be saying "hello, are you there?" which
                                # we'll detect once finals arrive in _fire_turn.
                                _cancel_eou_timer()
                            continue
                        if event_type == "speech_end":
                            # NOT an authoritative end-of-turn — Sarvam VAD
                            # emits this on every pause. Treat as a hint:
                            # restart the debounce. We only fire when the
                            # user has actually been silent for EOU_DEBOUNCE_MS.
                            if utterance_segments:
                                _restart_eou_timer()
                            continue
                        text = str(parsed.get("text") or "").strip()
                        if not text:
                            continue
                        is_final = bool(parsed.get("is_final"))
                        raw_segment_language = parsed.get("language")
                        if raw_segment_language:
                            utterance_language_detected[0] = True
                        utterance_language[0] = SarvamVoiceService.normalize_language(
                            raw_segment_language or utterance_language[0]
                        )
                        await websocket.send_json(
                            {
                                "type": "stt_transcript",
                                "text": text,
                                "is_final": is_final,
                                "language": utterance_language[0],
                            }
                        )
                        if is_final:
                            utterance_segments.append(text)
                            # Restart debounce; we'll fire if speech_end never arrives.
                            _restart_eou_timer()
                except Exception as exc:
                    print(f"[NOKVO-VOICE] Sarvam reader exception: {exc!r}")

            stt_reader_task = asyncio.create_task(_reader())

        await NokvoOneVoiceStreamService._emit_runtime_status(websocket, tenant_res)
        # Surface = inbound vs outbound. We use this to route created records:
        # inbound voice → ticket (someone reaching out for help), outbound voice
        # → lead (we reached out to them). The pipeline reads this when the
        # tool returns its result IDs.
        call_surface = "voice_outbound" if (campaign_context or {}).get("campaign_id") else "voice_inbound"
        await AgentSessionStore.set_state(
            tenant_res,
            call_id,
            {
                "status": "connected",
                "language": language,
                "campaign_id": (campaign_context or {}).get("campaign_id"),
                "call_surface": call_surface,
            },
        )
        await websocket.send_json({"type": "voice_session_ready", "call_id": call_id})

        opening = (campaign_context or {}).get("opening_message")
        if opening:
            await NokvoOneVoiceStreamService._play_opener(
                websocket,
                tenant_res,
                opening,
                language=language,
                call_id=call_id,
                campaign_context=campaign_context,
            )
        else:
            async def _delayed_inbound_opener() -> None:
                try:
                    await asyncio.sleep(0.35)
                    await _play_default_inbound_opener()
                except asyncio.CancelledError:
                    pass

            inbound_opener_task = asyncio.create_task(_delayed_inbound_opener())

        try:
            while True:
                message = await websocket.receive()
                if message.get("type") == "websocket.disconnect":
                    break
                if message.get("bytes") is not None:
                    chunk = message.get("bytes") or b""
                    if capture_mode[0] == "vad_blob":
                        # Frontend already segmented the utterance with its own
                        # VAD — each binary frame is ONE complete utterance,
                        # not a streaming PCM chunk. We hand off both the new
                        # blob *and* a reference to the previous turn so
                        # _process_blob_utterance can transcribe first and
                        # decide: a check-in ("hello, are you there?") arriving
                        # while the prior answer is still composing should be
                        # acknowledged with a quick "yes" without cancelling
                        # the queued reply.
                        prev_turn = current_turn
                        prev_state = turn_state
                        new_state: dict[str, Any] = {"speaking": False}
                        turn_state = new_state
                        current_turn = asyncio.create_task(
                            NokvoOneVoiceStreamService._process_blob_utterance(
                                websocket,
                                tenant_res,
                                bytes(chunk),
                                db=db,
                                fallback_language=session_locked_language[0] or language,
                                call_id=call_id,
                                company_name=company_name,
                                campaign_context=campaign_context,
                                session_locked_language=session_locked_language,
                                prev_turn=prev_turn,
                                prev_turn_state=prev_state,
                                turn_state=new_state,
                            )
                        )
                        continue
                    audio_buffer.extend(chunk)
                    # Side-buffer the same chunk for the per-utterance translate-STT
                    # path. Cleared on speech_start (caller starts new utterance) and
                    # after _fire_turn consumes it.
                    if settings.AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED:
                        utterance_audio.extend(chunk)
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
                    requested_mode = str(payload.get("mode") or "").strip().lower()
                    if requested_mode in {"vad_blob", "stream"}:
                        capture_mode[0] = requested_mode
                        print(f"[NOKVO-VOICE] capture_mode set to {capture_mode[0]} for call {call_id}")
                    await NokvoOneVoiceStreamService._emit_runtime_status(websocket, tenant_res)
                    if inbound_opener_task and not inbound_opener_task.done():
                        inbound_opener_task.cancel()
                    await _play_default_inbound_opener()
                    continue
                if event_type == "interrupt":
                    # Client-side barge-in: user started speaking while agent
                    # was playing audio. Cancel the in-flight turn.
                    if current_turn and not current_turn.done():
                        current_turn.cancel()
                    _cancel_eou_timer()
                    utterance_segments.clear()
                    utterance_audio.clear()
                    continue
                if event_type in {"text_query", "transcript"}:
                    if current_turn and not current_turn.done():
                        current_turn.cancel()
                    turn_state = {"speaking": False}
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
                            turn_state=turn_state,
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
                                turn_state = {"speaking": False}
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
                                    turn_state=turn_state,
                                )
                        except Exception as exc:
                            await websocket.send_json({"type": "stt_error", "error_message": str(exc)[:220]})
                    if event_type == "stop":
                        break
        finally:
            if inbound_opener_task and not inbound_opener_task.done():
                inbound_opener_task.cancel()
            _cancel_eou_timer()
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
            await NokvoOneVoiceStreamService._log_voice_call(
                db,
                tenant_res,
                call_id,
                duration_seconds=int(perf_counter() - session_started),
                campaign_context=campaign_context,
            )
