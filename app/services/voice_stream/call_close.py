"""Deterministic call closes: voicemail drop, silent-caller goodbye, and the
questionnaire outro (speak-then-hang-up paths).

Extracted from nokvo_one_voice_stream_service.py (turn_router helpers
pattern: functions taking ``helpers`` receive ``NokvoOneVoiceStreamService``
and call sibling statics through it, so class-attribute monkeypatches keep
working). The service class keeps delegating wrappers - no API change.
"""
from __future__ import annotations

import logging
from typing import Any
import asyncio
import contextlib
import uuid

from fastapi import WebSocket

from app.models.tenant_resources import TenantResources
from app.services.agent_outbound_context import OutboundCampaignContext, generate_outbound_opener_text
from app.services.agent_robustness import TurnArbiter
from app.services.agent_session_store import AgentSessionStore
from app.services.prosody import DEFAULT_TONE, ProsodyChunk, prosody_for, stream_prosody_chunks, style_prosody
from app.services.sarvam_voice_service import SarvamVoiceService
from app.services.voice_stream.call_texts import (
    _OUTRO_DRAIN_SECONDS,
    _no_response_goodbye_text,
    _voicemail_message,
)
from app.services.voice_stream.eou import _question_answer_kind, _verbatim_prespeech_delay_s

logger = logging.getLogger(__name__)


async def _leave_voicemail_and_end(
    websocket: WebSocket,
    tenant_res: TenantResources,
    *,
    language: str,
    call_id: str | None,
    campaign_context: dict[str, Any] | None = None,
    outbound_context: OutboundCampaignContext | None = None,
    arbiter: TurnArbiter | None = None,
    turn_state: dict[str, Any] | None = None,
) -> None:
    """Outbound voicemail reached: speak ONE short on-brand line, then hang up.

    Closing the media WS ends the Plivo ``<Stream>`` and drops the call; the
    ``call.hangup`` status webhook then closes the contact out as usual. Never
    raises — a TTS hiccup must still let us hang up.
    """
    caller = ""
    company = ""
    if outbound_context is not None:
        caller = (getattr(outbound_context, "caller_name", "") or "").strip()
        company = (getattr(outbound_context, "company_name", "") or "").strip()
    if not company:
        company = str((campaign_context or {}).get("company_name") or "").strip()
    line = _voicemail_message(language, caller_name=caller, company_name=company)

    # Mark speaking so a racing check-in can't barge our final line.
    if turn_state is not None:
        turn_state["speaking"] = True
    if arbiter is not None:
        arbiter.mark_speaking()

    turn_id = str(uuid.uuid4())[:8]
    try:
        await websocket.send_json(
            {
                "type": "agent_sentence",
                "turn_id": turn_id,
                "sentence": line,
                "tone": "warm",
                "cache_hit": False,
                "source": "voicemail_drop",
            }
        )
        await SarvamVoiceService.stream_sentence_tts(
            websocket,
            tenant_res,
            line,
            language=language,
            purpose="voicemail",
        )
    except Exception:
        logger.debug("NOKVO-VOICE: voicemail drop TTS failed", exc_info=True)
    try:
        await AgentSessionStore.append_turn(
            tenant_res, call_id, "(voicemail detected)", line
        )
    except Exception:
        pass
    logger.info(
        "NOKVO-VOICE: answering machine detected call=%s — left message, ending call",
        call_id,
    )
    try:
        await websocket.close()
    except Exception:
        pass


async def _end_call_no_response(
    websocket: WebSocket,
    tenant_res: TenantResources,
    *,
    language: str,
    call_id: str | None,
    campaign_context: dict[str, Any] | None = None,
    arbiter: TurnArbiter | None = None,
    turn_state: dict[str, Any] | None = None,
) -> None:
    """Picked-up caller went silent through a nudge: speak ONE short goodbye,
    then hang up. Closing the media WS ends the Plivo ``<Stream>`` and drops
    the call; the ``call.hangup`` status webhook closes the contact out as
    usual. Mirrors :meth:`_leave_voicemail_and_end`. Never raises — a TTS
    hiccup must still let us hang up."""
    line = _no_response_goodbye_text(language)
    # Mark speaking so a racing check-in can't barge our final line.
    if turn_state is not None:
        turn_state["speaking"] = True
    if arbiter is not None:
        arbiter.mark_speaking()
    turn_id = str(uuid.uuid4())[:8]
    try:
        await websocket.send_json(
            {
                "type": "agent_sentence",
                "turn_id": turn_id,
                "sentence": line,
                "tone": "warm",
                "cache_hit": False,
                "source": "no_response_end",
            }
        )
        await SarvamVoiceService.stream_sentence_tts(
            websocket, tenant_res, line, language=language, purpose="answer"
        )
    except Exception:
        logger.debug("NOKVO-VOICE: no-response goodbye TTS failed", exc_info=True)
    # Leave a clear marker so the post-call note reflects a silent abandon
    # (caller answered but never engaged) rather than a stated disinterest.
    with contextlib.suppress(Exception):
        await AgentSessionStore.append_turn(
            tenant_res, call_id, "(no response — caller silent)", line
        )
    logger.info(
        "NOKVO-VOICE: no caller response after nudge call=%s — ending call", call_id
    )
    try:
        await websocket.close()
    except Exception:
        pass


async def _speak_outro_and_end(
    websocket: WebSocket,
    tenant_res: TenantResources,
    *,
    outro: str,
    language: str,
    call_id: str | None,
    last_user_text: str = "",
    campaign_context: dict[str, Any] | None = None,
    arbiter: TurnArbiter | None = None,
    turn_state: dict[str, Any] | None = None,
    eou_fired_at: float | None = None,
    style: str = "",
) -> None:
    """Deterministic questionnaire CLOSE: speak the campaign's closing line
    verbatim, then hang up.

    Used once every question has been asked and the caller has given a real
    answer to the last one. The model is unreliable about delivering the
    closing line itself (it re-asks the last question, invents a name
    question, or loops back to an earlier one), so we close deterministically
    — no LLM turn. Mirrors :meth:`_leave_voicemail_and_end`. Never raises."""
    from app.services.apex_micro_acks import tts_variant_for_call

    line = (outro or "").strip()
    if not line:
        with contextlib.suppress(Exception):
            await websocket.close()
        return
    # Same humanized pre-speech pause as the verbatim questions — the outro
    # also follows a caller answer and otherwise fires unnaturally fast.
    _delay_s = _verbatim_prespeech_delay_s(eou_fired_at=eou_fired_at)
    if _delay_s > 0:
        with contextlib.suppress(Exception):
            await asyncio.sleep(_delay_s)
    # Mark speaking so a racing check-in can't barge our final line.
    if turn_state is not None:
        turn_state["speaking"] = True
    if arbiter is not None:
        arbiter.mark_speaking()
    turn_id = str(uuid.uuid4())[:8]
    try:
        await websocket.send_json(
            {
                "type": "agent_sentence",
                "turn_id": turn_id,
                "sentence": line,
                "tone": "warm",
                "cache_hit": False,
                "source": "questionnaire_outro",
            }
        )
        # Style voice overlay only — None values for an unstyled campaign
        # keep the request body (and thus the warmed cache keys) identical
        # to the historical no-prosody close.
        _sp = style_prosody(style)
        await SarvamVoiceService.stream_sentence_tts(
            websocket,
            tenant_res,
            line,
            language=language,
            purpose="outro",
            pace=_sp.pace if _sp else None,
            pitch=_sp.pitch if _sp else None,
            loudness=_sp.loudness if _sp else None,
            cache=True,  # deterministic closing line — same audio every call
            variant=tts_variant_for_call(call_id),
        )
    except Exception:
        logger.debug("NOKVO-OUTRO: outro TTS failed", exc_info=True)
    # Record the caller's final answer + the outro so the post-call scorer
    # still sees the last question's answer (we paired the real reply, not a
    # synthetic marker).
    try:
        await AgentSessionStore.append_turn(
            tenant_res, call_id, (last_user_text or "").strip() or "(questionnaire complete)", line
        )
    except Exception:
        pass
    logger.info(
        "NOKVO-OUTRO: questionnaire complete call=%s — spoke closing line, ending call",
        call_id,
    )
    with contextlib.suppress(Exception):
        await asyncio.sleep(_OUTRO_DRAIN_SECONDS)
        await websocket.close()
