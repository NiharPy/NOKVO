"""Verbatim questionnaire delivery: ack + question playback and the
authoritative delivered-question persistence.

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
import random
import uuid

from fastapi import WebSocket

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.agent_outbound_context import OutboundCampaignContext, generate_outbound_opener_text
from app.services.agent_robustness import TurnArbiter
from app.services.agent_session_store import AgentSessionStore
from app.services.prosody import DEFAULT_TONE, ProsodyChunk, prosody_for, stream_prosody_chunks, style_prosody
from app.services.sarvam_voice_service import SarvamVoiceService
from app.services.voice_stream.eou import _question_answer_kind, _verbatim_prespeech_delay_s
from app.services.voice_stream.tts_pump import _campaign_voice_style

logger = logging.getLogger(__name__)


async def _deliver_verbatim_question(
    helpers: Any,
    websocket: WebSocket,
    tenant_res: TenantResources,
    *,
    cleaned: str,
    language: str,
    call_id: str,
    outbound_context: OutboundCampaignContext,
    arbiter: TurnArbiter | None = None,
    turn_state: dict[str, Any] | None = None,
    campaign_context: dict[str, Any] | None = None,
    eou_fired_at: float | None = None,
) -> bool:
    """APEX Phase 3: speak the next questionnaire question VERBATIM from its
    pre-translated per-language string (cache=True → hits the TTS cache), skipping
    the LLM for this turn. Returns True when it delivered a question; False to
    fall through to the normal LLM turn (re-ask / non-answer / off-script / a
    question without a translation). Never raises. Does NOT close the call — the
    caller answers next; mirrors the opener/outro delivery but keeps the session
    open and re-arms turn-taking (mark_done + speaking=False).

    Humanization on this deterministic path (all flag-gated, see config):
    a gap-target pre-speech pause (the cached reply otherwise lands with
    machine-gun regularity), an optional seeded micro-ack spoken before the
    question, a shaped silence gap between ack and question on telephony,
    and a per-call rendition variant so the same line isn't the identical
    waveform on every call. It also stashes the expected-answer shape on
    ``campaign_context`` for the EOU endpointing hint."""
    from app.services.agent_outbound_context import (
        get_delivered_questions,
        next_verbatim_question,
        verbatim_line_for_language,
    )
    from app.services.apex_micro_acks import choose_ack, tts_variant_for_call

    try:
        qs = list(getattr(outbound_context, "questions", []) or [])
        history = await AgentSessionStore.get_history(tenant_res, call_id)
        plan = next_verbatim_question(qs, history, cleaned)
        if plan is None:
            return False
        idx, q = plan
        line = verbatim_line_for_language(q.get("text_i18n"), str(q.get("text") or ""), language)
        if not line:
            return False

        # Ack decision BEFORE the pause: when an ack fires, the ACK is the
        # gap-filler, so the pre-speech sleep clamps hard (ACK_MAX) and the
        # ack voice lands almost immediately after the EOU fired.
        last_ack = (
            campaign_context.get("_last_ack")
            if isinstance(campaign_context, dict)
            else None
        )
        ack = choose_ack(
            call_id=call_id,
            question_idx=idx,
            language=language,
            last_ack=last_ack,
            delivered_count=len(get_delivered_questions()),
            style=str(getattr(outbound_context, "questionnaire_style", "") or ""),
        )
        # Humanized pre-speech pause. Deliberately BEFORE mark_speaking: a
        # caller who resumes talking during the sleep lands as a NEW
        # utterance (turn cancel/replace via the arbiter), never as a
        # barge-in against silence — the pause itself adds listen-safety.
        delay_s = _verbatim_prespeech_delay_s(
            eou_fired_at=eou_fired_at, ack_will_fire=bool(ack)
        )
        if delay_s > 0:
            await asyncio.sleep(delay_s)

        variant = tts_variant_for_call(call_id)
        if turn_state is not None:
            turn_state["speaking"] = True
        if arbiter is not None:
            arbiter.mark_speaking()
        turn_id = str(uuid.uuid4())[:8]
        _vstyle = _campaign_voice_style(outbound_context)
        prosody = prosody_for("question", _vstyle)
        try:
            if ack:
                warm = prosody_for("warm", _vstyle)
                await websocket.send_json(
                    {
                        "type": "agent_sentence",
                        "turn_id": turn_id,
                        "sentence": ack,
                        "tone": "warm",
                        "cache_hit": False,
                        "source": "questionnaire_ack",
                    }
                )
                await SarvamVoiceService.stream_sentence_tts(
                    websocket,
                    tenant_res,
                    ack,
                    language=language,
                    purpose="verbatim_ack",
                    pace=warm.pace,
                    pitch=warm.pitch,
                    loudness=warm.loudness,
                    cache=True,  # tiny global pool — prewarmed, shared across campaigns
                    variant=variant,
                )
                # Breathing room between ack and question. Telephony
                # adapters queue real silence frames (flushed by barge-in's
                # clearAudio); the web test call's raw WS has no such
                # method and skips it — the browser's event gap suffices.
                if hasattr(websocket, "send_silence_ms"):
                    jitter = int(settings.APEX_ACK_GAP_JITTER_MS or 0)
                    gap_ms = int(settings.APEX_ACK_GAP_MS or 0) + (
                        random.randint(-jitter, jitter) if jitter > 0 else 0
                    )
                    if gap_ms > 0:
                        await websocket.send_silence_ms(gap_ms)
            await websocket.send_json(
                {
                    "type": "agent_sentence",
                    "turn_id": turn_id,
                    "sentence": line,
                    "tone": "question",
                    "cache_hit": False,
                    "source": "questionnaire_verbatim",
                }
            )
            await SarvamVoiceService.stream_sentence_tts(
                websocket,
                tenant_res,
                line,
                language=language,
                purpose="question",
                pace=prosody.pace,
                pitch=prosody.pitch,
                loudness=prosody.loudness,
                cache=True,  # verbatim, pre-translated → same audio every call
                variant=variant,
            )
        finally:
            if arbiter is not None:
                arbiter.mark_done()
            if turn_state is not None:
                turn_state["speaking"] = False
        # Record it as the assistant turn so asked-tracking advances next
        # turn. ONE line including the ack — the transcript mirrors exactly
        # what the caller heard, and the LLM sees the ack in history so it
        # won't double-ack the next off-script turn. (Prepending only ADDS
        # tokens to the assistant side of the token-overlap asked matcher,
        # and the persisted delivered set below is authoritative anyway.)
        spoken = f"{ack} {line}" if ack else line
        with contextlib.suppress(Exception):
            await AgentSessionStore.append_turn(tenant_res, call_id, (cleaned or "").strip(), spoken)
        # AUTHORITATIVE progress: persist "Q{idx} delivered" so this question
        # can never read as unasked again (paraphrase-, language-, and
        # history-eviction-proof — the questionnaire-loop killer).
        with contextlib.suppress(Exception):
            await helpers._persist_question_delivered(tenant_res, call_id, idx)
        if isinstance(campaign_context, dict):
            if ack:
                campaign_context["_last_ack"] = ack
            # Expected-answer shape for the EOU endpointing hint (read by
            # _eou_decision; cleared when a turn falls through to the LLM).
            campaign_context["_awaiting_answer_kind"] = _question_answer_kind(q)
        logger.info(
            "NOKVO-VERBATIM: asked Q%d verbatim call=%s lang=%s ack=%s variant=%d delay_ms=%d",
            idx, call_id, language, bool(ack), variant, int(delay_s * 1000),
        )
        return True
    except Exception:
        logger.exception("NOKVO-VERBATIM: verbatim question delivery failed")
        return False


async def _persist_question_delivered(
    tenant_res: TenantResources, call_id: str | None, number: int | None
) -> None:
    """Merge question ``number`` into the call's authoritative delivered set
    (session state ``questionnaire_progress.delivered``) and refresh the
    ambient contextvar so the SAME turn's later reads see it too."""
    if not call_id or not number:
        return
    from app.services.agent_outbound_context import (
        get_delivered_questions,
        set_delivered_questions,
    )

    merged = sorted(set(get_delivered_questions()) | {int(number)})
    set_delivered_questions(merged)
    await AgentSessionStore.merge_state(
        tenant_res, call_id, {"questionnaire_progress": {"delivered": merged}}
    )
