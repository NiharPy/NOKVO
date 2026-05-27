from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import asyncio
import contextlib
import json
import struct
import uuid
from datetime import datetime, timezone
from time import perf_counter
from typing import Any


def _extract_pcm_from_wav(wav: bytes) -> tuple[bytes, int] | None:
    """Return (pcm_data, sample_rate) for a RIFF/WAVE PCM16 mono blob, or
    ``None`` if the blob is not a parseable PCM16-mono WAV.

    Used by the audio-quality probe — Sarvam can also accept WebM/Opus
    but those need ffmpeg to decode, so we only score WAV blobs locally
    and let other formats pass through unscored.
    """
    if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        return None
    pos = 12
    fmt: tuple[int, int, int] | None = None
    data: bytes | None = None
    while pos + 8 <= len(wav):
        chunk_id = wav[pos : pos + 4]
        size = int.from_bytes(wav[pos + 4 : pos + 8], "little")
        body_start = pos + 8
        body_end = body_start + size
        if body_end > len(wav):
            break
        if chunk_id == b"fmt ":
            if size >= 16:
                audio_format = int.from_bytes(wav[body_start : body_start + 2], "little")
                channels = int.from_bytes(wav[body_start + 2 : body_start + 4], "little")
                sample_rate = int.from_bytes(wav[body_start + 4 : body_start + 8], "little")
                bps = int.from_bytes(wav[body_start + 14 : body_start + 16], "little")
                fmt = (audio_format, channels, sample_rate, bps)
        elif chunk_id == b"data":
            data = wav[body_start:body_end]
        pos = body_end + (size & 1)  # word-align
        if fmt is not None and data is not None:
            break
    if not fmt or data is None:
        return None
    audio_format, channels, sample_rate, bps = fmt
    if audio_format != 1 or channels != 1 or bps != 16:
        return None
    return data, sample_rate


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
from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    PROACTIVE_NUDGE_PROMPT,
    PROACTIVE_OPENER_PROMPT,
    ProactiveSilenceWatchdog,
    generate_outbound_opener_text,
    infer_covered_objectives,
    load_outbound_context,
    update_outbound_memory,
)
from app.services.agent_robustness import (
    AudioQualityProbe,
    ClarificationState,
    LanguageState,
    QUALITY_UNUSABLE,
    RobustnessContext,
    TURN_SPEAKING,
    TurnArbiter,
    clarification_prompt,
    is_turn_vague,
    repeat_prompt,
    CLARIFY_RESET,
    CLARIFY_NUDGE,
    CLARIFY_OFFER_OPTIONS,
    CLARIFY_ESCALATE,
)
from app.services.agent_session_store import AgentSessionStore
from app.services.conversational_memory import (
    ConversationalMemory,
    bootstrap_caller_memory,
    load_memory,
    promote_to_caller_memory,
    save_memory,
)
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


# How many sentences may be combined into a single TTS call after the first
# one has been spoken. The first sentence is ALWAYS dispatched on its own so
# first-audio latency stays minimal; sentences that arrive while the worker
# is busy synthesising are coalesced up to this size to amortise the Sarvam
# REST roundtrip across multiple sentences.
_TTS_BATCH_MAX = 2


async def _drain_turn(task: asyncio.Task | None) -> None:
    """Cancel ``task`` and wait for it to fully exit.

    The voice session shares a single AsyncSession across the WebSocket's
    lifetime. ``AsyncSession`` is *not* safe for concurrent use — if a
    cancelled turn is still mid-``await db.execute(...)`` when the next
    turn starts its own ``db.execute(...)``, SQLAlchemy raises
    ``greenlet_spawn has not been called; can't call await_only() here``.
    Awaiting the cancellation lets the asyncpg connection unwind before
    any new code path touches ``db``.
    """
    if task is None or task.done():
        return
    task.cancel()
    try:
        await task
    except BaseException:
        pass


class _TtsPump:
    """Background TTS dispatcher that batches sentences after the first one.

    Calling ``submit(sentence, tone)`` is non-blocking — the sentence is
    enqueued and the LLM stream loop continues to read the next token
    without waiting for TTS network latency. A single worker drains the
    queue in order, firing the first sentence as soon as it lands and then
    coalescing any sentences that piled up while TTS was in flight into a
    single batched Sarvam call (up to :data:`_TTS_BATCH_MAX`).

    Ordering is preserved end-to-end: the worker awaits each TTS call
    before pulling the next, so the audio packets emitted on the websocket
    arrive in sentence order.
    """

    def __init__(
        self,
        *,
        websocket: WebSocket,
        tenant_res: TenantResources,
        language: str,
        turn_id: str,
        purpose: str = "answer",
        speaking_mark: Any | None = None,
    ) -> None:
        self._websocket = websocket
        self._tenant_res = tenant_res
        self._language = language
        self._turn_id = turn_id
        self._purpose = purpose
        self._speaking_mark = speaking_mark
        self._queue: asyncio.Queue[tuple[str, str, bool] | None] = asyncio.Queue()
        self._worker: asyncio.Task[None] | None = None
        self._first_audio_fired = False

    def start(self) -> None:
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())

    async def submit(self, sentence: str, tone: str, *, cacheable_tts: bool = False) -> None:
        if not sentence:
            return
        await self._queue.put((sentence, tone, cacheable_tts))

    async def close(self) -> None:
        """Send the end-of-stream sentinel and wait for the worker to flush
        all buffered sentences. Safe to call multiple times."""
        if self._worker is None:
            return
        await self._queue.put(None)
        try:
            await self._worker
        except asyncio.CancelledError:
            raise
        finally:
            self._worker = None

    async def cancel(self) -> None:
        if self._worker is None:
            return
        self._worker.cancel()
        try:
            await self._worker
        except (asyncio.CancelledError, Exception):
            pass
        self._worker = None

    async def _run(self) -> None:
        while True:
            item = await self._queue.get()
            if item is None:
                return
            batch: list[tuple[str, str, bool]] = [item]
            # First sentence: dispatch alone so first audio lands as fast
            # as possible. After that, opportunistically drain any extra
            # sentences that piled up while the previous TTS call was in
            # flight — they collapse into one Sarvam round trip.
            if self._first_audio_fired:
                while len(batch) < _TTS_BATCH_MAX:
                    try:
                        more = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if more is None:
                        await self._flush(batch)
                        return
                    batch.append(more)
            await self._flush(batch)
            self._first_audio_fired = True

    async def _flush(self, batch: list[tuple[str, str, bool]]) -> None:
        if not batch:
            return
        text = " ".join(s for s, _, _ in batch).strip()
        if not text:
            return
        # Use the tone of the first sentence in the batch for prosody —
        # adjacent sentences from the same LLM completion almost always
        # carry the same emotional register.
        prosody = None if not self._first_audio_fired else prosody_for(batch[0][1] or DEFAULT_TONE)
        if self._speaking_mark is not None:
            try:
                self._speaking_mark()
            except Exception:
                pass
        try:
            await SarvamVoiceService.stream_sentence_tts(
                self._websocket,
                self._tenant_res,
                text,
                language=self._language,
                purpose=self._purpose,
                pace=prosody.pace if prosody else None,
                pitch=prosody.pitch if prosody else None,
                loudness=prosody.loudness if prosody else None,
                enable_cached_responses=all(cacheable for _, _, cacheable in batch),
            )
        except Exception as exc:
            try:
                await self._websocket.send_json(
                    {
                        "type": "tts_error",
                        "turn_id": self._turn_id,
                        "error_message": str(exc)[:240],
                        "provider": "sarvam",
                    }
                )
            except Exception:
                pass


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


def _latency_guard_text(language: str | None) -> str:
    return {
        "hi": "एक पल, मैं देख रहा हूँ।",
        "ta": "ஒரு நிமிடம், பார்த்துக்கிறேன்.",
        "te": "ఒక్క క్షణం, చూస్తున్నాను.",
        "bn": "একটু সময় দিন, আমি দেখছি।",
        "kn": "ಒಂದು ಕ್ಷಣ, ನೋಡುತ್ತಿದ್ದೇನೆ.",
        "ml": "ഒരു നിമിഷം, ഞാൻ നോക്കുകയാണ്.",
        "mr": "एक क्षण, मी पाहतोय.",
        "gu": "એક ક્ષણ, હું જોઈ રહ્યો છું.",
        "pa": "ਇੱਕ ਪਲ, ਮੈਂ ਵੇਖ ਰਿਹਾ ਹਾਂ।",
    }.get(language or "en", "One moment, I'm checking that.")


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
    def _campaign_context_with_adapter_call_details(
        campaign_context: dict[str, Any] | None,
        websocket: WebSocket,
    ) -> dict[str, Any] | None:
        adapter_context = getattr(websocket, "call_context", None)
        if not isinstance(adapter_context, dict) or not adapter_context:
            return campaign_context
        merged = dict(campaign_context or {})
        for key in ("from_phone", "to_phone", "provider_call_id"):
            if adapter_context.get(key) and not merged.get(key):
                merged[key] = adapter_context[key]
        return merged

    @staticmethod
    async def _dispatch_quality_recovery(
        websocket: WebSocket,
        tenant_res: TenantResources,
        *,
        language: str,
        source: str = "audio_quality_recovery",
    ) -> None:
        """Emit a multilingual "could you say that again" prompt and
        speak it via TTS. Used by both the vad_blob and streaming-STT
        paths when the :class:`AudioQualityProbe` returns UNUSABLE.
        Previously both call sites inlined ~30 lines of identical
        websocket / TTS plumbing."""
        recover_text = repeat_prompt(language)
        recover_turn_id = f"recover-{uuid.uuid4().hex[:8]}"
        try:
            await websocket.send_json(
                {
                    "type": "agent_sentence",
                    "turn_id": recover_turn_id,
                    "sentence": recover_text,
                    "tone": "warm",
                    "cache_hit": False,
                    "source": source,
                }
            )
        except Exception:
            pass
        try:
            await SarvamVoiceService.stream_sentence_tts(
                websocket,
                tenant_res,
                recover_text,
                language=language,
                purpose=source,
            )
        except Exception:
            pass
        try:
            await websocket.send_json(
                {
                    "type": "turn_complete",
                    "turn_id": recover_turn_id,
                    "context_source": source,
                }
            )
        except Exception:
            pass

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
            from app.services.voice_data_audit_service import VoiceDataAuditService

            await VoiceDataAuditService.log_tenant_access(
                db,
                tenant_res,
                actor_type="system",
                access_type="summarize",
                resource_type="session_history",
                resource_id=call_id,
                call_id=call_id,
                reason="voice_call_log_create",
                metadata={"duration_seconds": max(0, duration_seconds), "turn_count": len(history)},
            )
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
        arbiter: TurnArbiter | None = None,
        language_state: LanguageState | None = None,
        outbound_context: OutboundCampaignContext | None = None,
        after_turn=None,
    ) -> None:
        cleaned = " ".join((text or "").split())
        if not cleaned:
            return
        if outbound_context is not None and call_id and source != "proactive_silence":
            await AgentSessionStore.merge_state(
                tenant_res,
                call_id,
                {"proactive_silence_nudges": 0},
            )
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
            if arbiter is not None:
                arbiter.mark_speaking()

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

        # Proactive-silence turns synthesize a "(no caller response — ...)"
        # prompt that the LLM consumes as guidance. It is internal scaffolding,
        # NOT something the caller actually said — don't render it as a user
        # transcript or thinking-query bubble in the UI. agent_sentence /
        # agent_answer events still flow so the response is voiced normally.
        if source == "proactive_silence":
            await websocket.send_json({"type": "agent_thinking", "turn_id": turn_id, "query": "(proactive nudge)"})
        else:
            await websocket.send_json({"type": "stt_finished", "text": cleaned, "turn_id": turn_id, "source": source})
            await websocket.send_json({"type": "agent_thinking", "turn_id": turn_id, "query": cleaned})
        answer_parts: list[str] = []
        final_payload: dict[str, Any] | None = None
        first_sentence_ms: int | None = None
        # Decouple LLM stream consumption from TTS roundtrips: the pump
        # owns the Sarvam calls so the LLM token loop never blocks waiting
        # on TTS, and adjacent sentences are batched into a single REST
        # call after the first one has been dispatched.
        tts_pump = _TtsPump(
            websocket=websocket,
            tenant_res=tenant_res,
            language=language,
            turn_id=turn_id,
            purpose="answer",
            speaking_mark=_mark_speaking,
        )
        tts_pump.start()
        if arbiter is not None:
            arbiter.attach_pump(tts_pump)
        # Hand the code-switch flag to the pipeline so dual retrieval
        # fires when the call has been mixing languages turn-to-turn or
        # the current transcript itself is script-mixed.
        is_code_switching = bool(language_state and language_state.needs_dual_retrieval())
        # Hydrate the per-call objective-progress list from the session
        # state so the LLM sees which campaign objectives are still
        # outstanding on this turn. The clarification / progress writer
        # lives below — for now we read what's already been recorded.
        covered_objectives: list[str] = []
        stored_outbound_memory: dict[str, Any] = {}
        prompt_outbound_memory: dict[str, str] | None = None
        # ── Conversational memory ────────────────────────────────────
        # Universal layer: applies to inbound AND outbound, drives the
        # "don't re-ask" prompt block, and feeds tool_flow_policy so the
        # structured booking flow skips slots whose values the caller
        # already gave us mid-conversation.
        conv_memory = await load_memory(tenant_res, call_id)
        turn_index = len(await AgentSessionStore.get_history(tenant_res, call_id))
        # First turn? Bootstrap from cross-call caller memory if we
        # have a phone. Outbound campaigns carry it on
        # ``campaign_context.contact.phone``; inbound webhooks stash
        # ``from_phone``. Failures are silent — a cold call is the
        # default and always works.
        if turn_index == 0 and not conv_memory.facts:
            caller_phone = None
            try:
                contact = (campaign_context or {}).get("contact")
                if isinstance(contact, dict):
                    caller_phone = contact.get("phone") or contact.get("phone_e164")
                caller_phone = caller_phone or (campaign_context or {}).get("from_phone")
            except Exception:
                caller_phone = None
            if caller_phone:
                try:
                    await bootstrap_caller_memory(
                        tenant_res, phone=caller_phone, memory=conv_memory
                    )
                except Exception:
                    logger.debug("NOKVO-MEMORY: bootstrap failed", exc_info=True)
        if source != "proactive_silence":
            conv_memory.merge_text(
                cleaned,
                turn_index=turn_index,
                language=language,
                role="user",
            )
        # Don't await the save yet — we'll fold in the agent's answer
        # extraction later in the same turn and commit once. Persist
        # immediately though as a safety net in case the LLM call hangs.
        await save_memory(tenant_res, call_id, conv_memory)

        if outbound_context is not None:
            try:
                session_state = await AgentSessionStore.get_state(tenant_res, call_id)
                covered_objectives = list(
                    (session_state or {}).get("campaign_objectives_covered") or []
                )
                stored_outbound_memory = dict((session_state or {}).get("outbound_memory") or {})
                prompt_outbound_memory = update_outbound_memory(
                    stored_outbound_memory,
                    caller_text=cleaned,
                )
            except Exception:
                covered_objectives = []
                stored_outbound_memory = {}
                prompt_outbound_memory = update_outbound_memory({}, caller_text=cleaned)
        stream_done = object()
        event_queue: asyncio.Queue[Any] = asyncio.Queue()

        async def _produce_events() -> None:
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
                    code_switching=is_code_switching,
                    outbound_context=outbound_context,
                    covered_objectives=covered_objectives,
                    outbound_memory=prompt_outbound_memory,
                    conversational_memory=conv_memory,
                ):
                    await event_queue.put(event)
            except Exception as exc:
                await event_queue.put(exc)
            finally:
                await event_queue.put(stream_done)

        producer_task = asyncio.create_task(_produce_events())

        async def _handle_stream_event(event: dict[str, Any]) -> None:
            nonlocal first_sentence_ms, final_payload
            if event.get("type") == "sentence":
                sentence = str(event.get("text") or "").strip()
                if not sentence:
                    return
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
                await tts_pump.submit(sentence, tone)
            elif event.get("type") == "final":
                final_payload = event

        latency_guard_sent = False
        # The spoken latency guard is useful for inbound support because it
        # reassures the caller during retrieval. On outbound calls it sounds
        # like an extra agent turn ("one moment...") and can stack in front of
        # the actual sales reply, which prevents natural interruption.
        latency_guard_enabled = outbound_context is None
        try:
            while True:
                timeout_s = (
                    max(0.05, settings.VOICE_FIRST_SENTENCE_TIMEOUT_MS / 1000)
                    if first_sentence_ms is None and latency_guard_enabled
                    else None
                )
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    if latency_guard_sent:
                        continue
                    latency_guard_sent = True
                    first_sentence_ms = int((perf_counter() - started) * 1000)
                    guard_sentence = _latency_guard_text(language)
                    await websocket.send_json(
                        {
                            "type": "agent_sentence",
                            "turn_id": turn_id,
                            "sentence": guard_sentence,
                            "tone": "warm",
                            "first_sentence_ms": first_sentence_ms,
                            "cache_hit": False,
                            "source": "latency_guard",
                        }
                    )
                    await tts_pump.submit(guard_sentence, "warm", cacheable_tts=True)
                    continue
                if event is stream_done:
                    break
                if isinstance(event, Exception):
                    raise event
                await _handle_stream_event(event)
            await producer_task
            await tts_pump.close()
        except asyncio.CancelledError:
            producer_task.cancel()
            with contextlib.suppress(BaseException):
                await producer_task
            await tts_pump.cancel()
            await websocket.send_json({"type": "turn_cancelled", "turn_id": turn_id})
            raise
        except Exception as exc:
            producer_task.cancel()
            with contextlib.suppress(BaseException):
                await producer_task
            await tts_pump.cancel()
            fallback = NokvoOneVoicePipeline._refusal(language)
            answer_parts = [fallback]
            final_payload = {"answer": fallback, "refused": True, "chunks": [], "citations": [], "runtime": {"error": str(exc)[:240]}}
            await websocket.send_json({"type": "agent_error", "turn_id": turn_id, "error": str(exc)[:240]})

        answer = str((final_payload or {}).get("answer") or " ".join(answer_parts)).strip()
        # Mine the agent's answer for fact confirmations the user
        # implicitly accepted (the agent often echoes "Got it — 3BHK
        # in Kompally"). Lower confidence than the user's own turn so
        # a later user correction still wins.
        if answer:
            conv_memory.merge_text(
                answer,
                turn_index=turn_index + 1,
                language=language,
                role="assistant",
            )
        await save_memory(tenant_res, call_id, conv_memory)
        outbound_state_patch: dict[str, Any] = {}
        if outbound_context is not None and call_id:
            updated_memory = update_outbound_memory(
                prompt_outbound_memory or stored_outbound_memory,
                caller_text=cleaned,
                agent_answer=answer,
            )
            if updated_memory != stored_outbound_memory:
                outbound_state_patch["outbound_memory"] = updated_memory
            updated_objectives = infer_covered_objectives(
                outbound_context,
                caller_text=cleaned,
                agent_answer=answer,
                already_covered=covered_objectives,
            )
            if updated_objectives != covered_objectives:
                outbound_state_patch["campaign_objectives_covered"] = updated_objectives
            if outbound_state_patch:
                await AgentSessionStore.merge_state(tenant_res, call_id, outbound_state_patch)
            if updated_objectives != covered_objectives:
                try:
                    await websocket.send_json(
                        {
                            "type": "campaign_objective_progress",
                            "covered": updated_objectives,
                            "remaining": outbound_context.remaining_objectives(updated_objectives),
                        }
                    )
                except Exception:
                    pass
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
        if arbiter is not None:
            arbiter.mark_done()
        if after_turn is not None:
            result = after_turn()
            if asyncio.iscoroutine(result):
                await result

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
        robustness: RobustnessContext | None = None,
        outbound_context: OutboundCampaignContext | None = None,
        after_turn=None,
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

        # Robustness layer — audio quality probe. We only score WAV
        # blobs (PCM16 mono); other formats need ffmpeg to decode and
        # we'd rather pay STT and let it return empty than block on a
        # decode here. When the verdict is "unusable" we short-circuit
        # the turn with a multilingual "could you repeat that" prompt
        # instead of running STT → LLM → TTS on garbage.
        if is_wav:
            extracted = _extract_pcm_from_wav(audio_bytes)
            if extracted is not None:
                pcm, pcm_sample_rate = extracted
                quality = AudioQualityProbe.score(pcm, sample_rate=pcm_sample_rate)
                try:
                    await websocket.send_json(
                        {
                            "type": "audio_quality",
                            "verdict": quality.verdict,
                            "reason": quality.reason,
                            "rms": round(quality.rms, 4),
                            "clip_ratio": round(quality.clip_ratio, 4),
                            "silence_ratio": round(quality.silence_ratio, 4),
                            "duration_ms": quality.duration_ms,
                        }
                    )
                except Exception:
                    pass
                if quality.verdict == QUALITY_UNUSABLE:
                    recover_lang = (
                        session_locked_language[0]
                        if session_locked_language and session_locked_language[0]
                        else fallback_language
                    )
                    await NokvoOneVoiceStreamService._dispatch_quality_recovery(
                        websocket, tenant_res, language=recover_lang,
                    )
                    return

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
                logger.warning("NOKVO-TRANSLATE: timeout in vad_blob mode; using native only")
                return ""
            except Exception as exc:
                logger.warning(f"NOKVO-TRANSLATE: failed: {exc!r}")
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
                logger.warning(f"NOKVO-VOICE: STT rate-limited after retries: {err_text[:200]!r}")
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

        # Turn arbitration. Prefer the explicit arbiter when available —
        # its phase tracking + atomic cancellation replaces the legacy
        # ``prev_turn`` + ``prev_turn_state["speaking"]`` pair. The
        # legacy path is still honoured so callers that haven't migrated
        # to the arbiter keep their existing behaviour.
        is_check_in = _is_check_in_utterance(transcript)
        if arbiter := (robustness.arbiter if robustness else None):
            verdict = arbiter.classify_incoming(is_check_in=is_check_in)
        else:
            prev_alive = (
                prev_turn is not None
                and not prev_turn.done()
                and not (prev_turn_state or {}).get("speaking")
            )
            if prev_alive and is_check_in:
                verdict = "check_in"
            elif prev_turn is not None and not prev_turn.done():
                verdict = "barge_in"
            else:
                verdict = "proceed"

        if verdict == "check_in":
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
        if verdict == "barge_in":
            await websocket.send_json({"type": "barge_in_detected", "call_id": call_id})
            if arbiter is not None:
                await arbiter.cancel()
            elif prev_turn is not None and not prev_turn.done():
                await _drain_turn(prev_turn)

        # Real new turn — make sure the prior one is fully cancelled.
        # The arbiter path already handled this above when verdict was
        # barge_in; the legacy ``prev_turn`` fallback handles the rest.
        if (
            (robustness is None or robustness.arbiter is None)
            and prev_turn is not None
            and not prev_turn.done()
        ):
            await _drain_turn(prev_turn)

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

        # Track the language history for code-switch awareness.
        if robustness is not None:
            robustness.language_state.observe(detected_lang, transcript)
            if requested:
                robustness.language_state.lock(turn_language)

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
            arbiter=(robustness.arbiter if robustness else None),
            language_state=(robustness.language_state if robustness else None),
            outbound_context=outbound_context,
            after_turn=after_turn,
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
        outbound_context_override: OutboundCampaignContext | None = None,
    ) -> None:
        await websocket.accept()
        session_started = perf_counter()
        # Wall-clock anchor for the billing ledger. ``perf_counter`` gives us
        # an accurate elapsed-time delta for runtime metrics, but the cost
        # row needs a real UTC timestamp the dashboard can render and an
        # ``ended_at`` set from the same clock at teardown.
        session_started_at = datetime.now(timezone.utc)
        language = SarvamVoiceService.normalize_language(language)
        call_id = call_id or str(uuid.uuid4())
        company_name = await NokvoOneVoiceStreamService._company_name(db, tenant_res)
        current_turn: asyncio.Task | None = None
        # Mutable state shared with the in-flight _run_text_turn so the dispatcher
        # can tell whether the answer has begun streaming TTS. Used to decide
        # whether a fresh utterance is a barge-in or a "hello, are you there?"
        # check-in arriving during the agent's composing latency.
        turn_state: dict[str, Any] = {"speaking": False}
        # Centralised robustness context — owns the turn arbiter (atomic
        # cancellation of the LLM stream + TTS pump on barge-in), the
        # language-state history (for code-switch detection), and the
        # clarification escalation FSM (hydrated from the Redis session
        # blob on each turn).
        robustness = RobustnessContext()
        # Outbound campaign context. Loaded once per call when the
        # session is initiated as part of a campaign — drives the
        # proactive system prompt + objective tracking. None for plain
        # inbound calls.
        outbound_context: OutboundCampaignContext | None = outbound_context_override
        # If the caller built a synthetic outbound context (e.g., the in-app
        # tester needs an outbound persona without a saved campaign row), we
        # trust it as-is and skip the DB lookup below.
        campaign_id_for_session = (campaign_context or {}).get("campaign_id")
        if outbound_context is None and campaign_id_for_session:
            try:
                outbound_context = await load_outbound_context(
                    db,
                    campaign_id_for_session,
                    goal=(campaign_context or {}).get("goal"),
                )
            except Exception as exc:
                # Pipeline still works without the proactive config —
                # it falls back to the legacy goal-only behaviour.
                logger.warning(f"NOKVO-OUTBOUND: load_outbound_context failed: {exc!r}")
                outbound_context = None
        proactive_watchdog: ProactiveSilenceWatchdog | None = None

        async def _arm_proactive_watchdog() -> None:
            if proactive_watchdog is not None:
                proactive_watchdog.arm()

        async def _fire_proactive_nudge() -> None:
            nonlocal current_turn, turn_state
            if outbound_context is None or not outbound_context.is_proactive:
                return
            if current_turn is not None and not current_turn.done():
                return
            state = await AgentSessionStore.get_state(tenant_res, call_id) or {}
            nudge_count = int(state.get("proactive_silence_nudges") or 0)
            if nudge_count >= 1:
                return
            await AgentSessionStore.merge_state(
                tenant_res,
                call_id,
                {"proactive_silence_nudges": nudge_count + 1},
            )
            turn_state = {"speaking": False}
            new_state = turn_state
            current_turn = asyncio.create_task(
                NokvoOneVoiceStreamService._run_text_turn(
                    websocket,
                    tenant_res,
                    PROACTIVE_NUDGE_PROMPT,
                    db=db,
                    language=session_locked_language[0] or language,
                    call_id=call_id,
                    company_name=company_name,
                    campaign_context=campaign_context,
                    source="proactive_silence",
                    turn_state=new_state,
                    arbiter=robustness.arbiter,
                    language_state=robustness.language_state,
                    outbound_context=outbound_context,
                    after_turn=_arm_proactive_watchdog,
                )
            )
            robustness.arbiter.begin(turn_id="proactive-silence", task=current_turn)

        if outbound_context is not None and outbound_context.is_proactive:
            proactive_watchdog = ProactiveSilenceWatchdog(
                timeout_seconds=outbound_context.silence_timeout_seconds,
                on_fire=_fire_proactive_nudge,
            )
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
        EOU_DEBOUNCE_MS = max(500, int(settings.VOICE_EOU_DEBOUNCE_MS))
        EOU_CONTINUATION_BONUS_MS = max(0, int(settings.VOICE_EOU_CONTINUATION_BONUS_MS))
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
            # Outbound proactive sessions own their opening line. Never
            # play the inbound "How can I help?" greeting on an outgoing
            # call — that's an inbound-only utterance.
            if outbound_context is not None and outbound_context.is_proactive:
                inbound_opener_played[0] = True
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

            # Audio-quality probe on the streaming path. The vad_blob
            # branch already scores its WAV input; this brings the same
            # safety net to the WebSocket-streaming branch using the raw
            # PCM accumulated in ``utterance_audio``. When the probe is
            # confidently UNUSABLE we ask the caller to repeat instead
            # of running STT/LLM/TTS on a noisy buffer that already
            # produced a transcript.
            if utterance_audio:
                quality = AudioQualityProbe.score(bytes(utterance_audio), sample_rate=sample_rate)
                try:
                    await websocket.send_json(
                        {
                            "type": "audio_quality",
                            "verdict": quality.verdict,
                            "reason": quality.reason,
                            "rms": round(quality.rms, 4),
                            "clip_ratio": round(quality.clip_ratio, 4),
                            "silence_ratio": round(quality.silence_ratio, 4),
                            "duration_ms": quality.duration_ms,
                            "source": "stream_pcm",
                        }
                    )
                except Exception:
                    pass
                # Only short-circuit when the transcript is also short
                # (< 4 words). A long, intelligible transcript that
                # happens to ride on low-SNR audio is fine — Sarvam
                # already proved it could read it.
                if (
                    quality.verdict == QUALITY_UNUSABLE
                    and len(text.split()) < 4
                ):
                    recover_lang = session_locked_language[0] or utterance_language[0]
                    await NokvoOneVoiceStreamService._dispatch_quality_recovery(
                        websocket, tenant_res, language=recover_lang,
                    )
                    utterance_audio.clear()
                    return

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
            # Outbound mode does not consult Qdrant retrieval; English
            # translation is therefore wasted work (and adds up to 1.5s of
            # turn latency). Inbound keeps the translate path so cross-
            # lingual recall against the tenant KB still works.
            _outbound_skip_translate = bool(
                outbound_context
                and outbound_context.is_proactive
            )
            if (
                settings.AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED
                and turn_language != "en"
                and utterance_audio
                and not _outbound_skip_translate
                and not NokvoOneVoicePipeline.should_skip_translate_for_native_query(text)
            ):
                translate_audio = bytes(utterance_audio)
            utterance_audio.clear()

            await _drain_turn(current_turn)
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
                        logger.warning(f"NOKVO-TRANSLATE: timeout after {TRANSLATE_TIMEOUT_S}s; falling back to native")
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
                    # Record the per-turn language history for code-switch
                    # detection (driven from the streaming-STT path's
                    # detected language).
                    robustness.language_state.observe(turn_language, text)
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
                        arbiter=robustness.arbiter,
                        language_state=robustness.language_state,
                        outbound_context=outbound_context,
                        after_turn=_arm_proactive_watchdog,
                    )

                current_turn = asyncio.create_task(_run_with_translate())
                robustness.arbiter.begin(turn_id="stream-translate", task=current_turn)
            else:
                robustness.language_state.observe(turn_language, text)
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
                        arbiter=robustness.arbiter,
                        language_state=robustness.language_state,
                        outbound_context=outbound_context,
                        after_turn=_arm_proactive_watchdog,
                    )
                )
                robustness.arbiter.begin(turn_id="stream-direct", task=current_turn)

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
                            # Arbiter classifies speech_start without a
                            # transcript yet. If the agent is already in
                            # the SPEAKING phase this is a real barge-in
                            # and we cancel atomically (LLM stream + TTS
                            # pump). Otherwise we just rewind the EOU
                            # timer and wait for the transcript to come
                            # in so _fire_turn can do check-in vs
                            # barge-in classification.
                            verdict = robustness.arbiter.classify_incoming(is_check_in=False)
                            if verdict == "barge_in" and robustness.arbiter.phase == TURN_SPEAKING:
                                await robustness.arbiter.cancel()
                                _cancel_eou_timer()
                                utterance_segments.clear()
                                utterance_audio.clear()
                                await websocket.send_json({"type": "barge_in_detected", "call_id": call_id})
                            else:
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
                    logger.warning(f"NOKVO-VOICE: Sarvam reader exception: {exc!r}")

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
        # Outbound sessions (campaign-launched OR tester) must NOT play the
        # inbound "How can I help?" greeting — they're outgoing calls. When
        # the campaign pre-generated an ``opening_message`` we play it
        # verbatim (zero LLM latency). Otherwise we kick the outbound agent
        # off with PROACTIVE_OPENER_PROMPT so it generates a campaign-aware
        # opener from the system fragment + brief.
        _outbound_proactive = bool(outbound_context) and outbound_context.is_proactive
        if opening:
            await NokvoOneVoiceStreamService._play_opener(
                websocket,
                tenant_res,
                opening,
                language=language,
                call_id=call_id,
                campaign_context=campaign_context,
            )
            await _arm_proactive_watchdog()
        elif _outbound_proactive:
            # Use the deterministic, template-filled opener — no LLM call,
            # ~150ms faster first audio. The LLM takes over from turn 2.
            outbound_opening_text = generate_outbound_opener_text(outbound_context, language=language)
            await NokvoOneVoiceStreamService._play_opener(
                websocket,
                tenant_res,
                outbound_opening_text,
                language=language,
                call_id=call_id,
                campaign_context=campaign_context,
            )
            await _arm_proactive_watchdog()
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
                if proactive_watchdog is not None:
                    proactive_watchdog.cancel()
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
                                robustness=robustness,
                                outbound_context=outbound_context,
                                after_turn=_arm_proactive_watchdog,
                            )
                        )
                        robustness.arbiter.begin(turn_id="vad-blob", task=current_turn)
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
                        logger.warning(f"NOKVO-VOICE: capture_mode set to {capture_mode[0]} for call {call_id}")
                    await NokvoOneVoiceStreamService._emit_runtime_status(websocket, tenant_res)
                    # The delayed-opener task may still be mid-``db.execute``
                    # (it looks up returning-caller history). Drain it before
                    # we run the opener inline so the two paths don't race
                    # on the shared ``db`` AsyncSession.
                    await _drain_turn(inbound_opener_task)
                    inbound_opener_task = None
                    # Defensive — never overlay the inbound greeting on an
                    # outbound proactive session even if the lazy path is
                    # somehow reached. The outbound opener has already been
                    # dispatched above (or will be by the proactive watchdog).
                    if not (outbound_context and outbound_context.is_proactive):
                        await _play_default_inbound_opener()
                    continue
                if event_type == "interrupt":
                    # Client-side barge-in: user started speaking while agent
                    # was playing audio. Drain the in-flight turn so the
                    # next message handler doesn't race it on the shared
                    # ``db`` AsyncSession (see ``_drain_turn``).
                    await _drain_turn(current_turn)
                    current_turn = None
                    _cancel_eou_timer()
                    utterance_segments.clear()
                    utterance_audio.clear()
                    continue
                if event_type == "end_of_utterance":
                    # Client VAD signalled real end-of-speech. In streaming
                    # mode we let Sarvam's STT WS race finalization while
                    # the server-side EOU debounce is already running, but
                    # the CLIENT VAD is more accurate than Sarvam's pause
                    # detector (it sees the actual mic signal). Treat this
                    # as authoritative: flush the Sarvam socket and fire
                    # the turn immediately, skipping the rest of the
                    # debounce. Saves 200-400ms per turn vs waiting for
                    # the EOU_DEBOUNCE_MS window.
                    if capture_mode[0] == "stream":
                        if stt_ws is not None:
                            try:
                                await SarvamVoiceService.flush_stt(stt_ws)
                            except Exception:
                                pass
                        if utterance_segments:
                            _cancel_eou_timer()
                            await _drain_turn(current_turn)
                            await _fire_turn()
                    continue
                if event_type in {"text_query", "transcript"}:
                    await _drain_turn(current_turn)
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
                            arbiter=robustness.arbiter,
                            language_state=robustness.language_state,
                            outbound_context=outbound_context,
                            after_turn=_arm_proactive_watchdog,
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
                                    arbiter=robustness.arbiter,
                                    language_state=robustness.language_state,
                                    outbound_context=outbound_context,
                                    after_turn=_arm_proactive_watchdog,
                                )
                        except Exception as exc:
                            await websocket.send_json({"type": "stt_error", "error_message": str(exc)[:220]})
                    if event_type == "stop":
                        break
        finally:
            # Drain every task that may still be touching the shared
            # ``db`` AsyncSession before ``_log_voice_call`` runs its own
            # query against it.
            await _drain_turn(inbound_opener_task)
            if proactive_watchdog is not None:
                proactive_watchdog.cancel()
            _cancel_eou_timer()
            await _drain_turn(current_turn)
            if stt_reader_task and not stt_reader_task.done():
                stt_reader_task.cancel()
            if stt_ws is not None:
                try:
                    await stt_ws.close()
                except Exception:
                    pass
            final_campaign_context = NokvoOneVoiceStreamService._campaign_context_with_adapter_call_details(
                campaign_context,
                websocket,
            )
            try:
                await NokvoOneVoicePipeline.maybe_create_real_estate_lead_from_call(
                    tenant_res,
                    db,
                    call_id,
                    campaign_context=final_campaign_context,
                    outbound_context=outbound_context,
                )
            except Exception as exc:
                logger.warning(f"NOKVO-VOICE: auto real-estate lead creation failed: {exc!r}")
            await NokvoOneVoiceStreamService._log_voice_call(
                db,
                tenant_res,
                call_id,
                duration_seconds=int(perf_counter() - session_started),
                campaign_context=final_campaign_context,
            )
            # Billing ledger — one row per call. Tester sessions and
            # campaign calls are tagged so the dashboard can split totals.
            # Failures here are logged and swallowed; they must never block
            # WS teardown.
            try:
                if str(call_id or "").startswith("tester:"):
                    cost_kind = "tester"
                elif outbound_context is not None:
                    cost_kind = "outbound"
                else:
                    cost_kind = "inbound"
                cost_campaign_id: Any = None
                if outbound_context is not None:
                    raw_cid = getattr(outbound_context, "campaign_id", None)
                    # Synthetic tester contexts use ``tester-<uuid>`` strings;
                    # only real UUIDs go into the ``campaign_id`` column.
                    if raw_cid and not str(raw_cid).startswith("tester-"):
                        cost_campaign_id = raw_cid
                from app.services.call_cost_recorder import record_call_cost

                await record_call_cost(
                    db,
                    organization_id=tenant_res.organization_id,
                    tenant_id=tenant_res.tenant_id,
                    call_id=str(call_id),
                    started_at=session_started_at,
                    ended_at=datetime.now(timezone.utc),
                    kind=cost_kind,
                    campaign_id=cost_campaign_id,
                )
            except Exception:
                logger.exception("NOKVO-VOICE: failed to record call cost")
            # Promote durable facts from this call's conversational
            # memory into the per-phone caller-memory blob so a future
            # call from the same number opens warm. Best-effort.
            try:
                final_memory = await load_memory(tenant_res, call_id)
                contact = (final_campaign_context or {}).get("contact") if isinstance(final_campaign_context, dict) else None
                promote_phone = None
                if isinstance(contact, dict):
                    promote_phone = contact.get("phone") or contact.get("phone_e164")
                if not promote_phone and final_memory.has("phone"):
                    promote_phone = final_memory.get("phone")
                if not promote_phone and isinstance(final_campaign_context, dict):
                    promote_phone = final_campaign_context.get("from_phone")
                if promote_phone:
                    await promote_to_caller_memory(
                        tenant_res, phone=promote_phone, memory=final_memory
                    )
            except Exception:
                logger.exception("NOKVO-MEMORY: promote_to_caller_memory failed at session end")
