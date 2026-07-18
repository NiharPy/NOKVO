from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

import asyncio
import contextlib
import json
import random
import re
import struct
import uuid
from datetime import datetime, timezone
from time import perf_counter
from typing import Any

# Module-level retention set for fire-and-forget post-call background work
# (the handoff-note condenser, etc.). Python's asyncio will silently drop a
# task that isn't kept reachable by *something*, so we hold a strong ref
# here and discard it on completion via add_done_callback. Without this the
# task can vanish mid-run with no error.
_background_tasks: set[asyncio.Task] = set()


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
    strip_leading_fillers,
    strip_leading_right_so,
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
from app.services.language_intent import (
    detect_language_switch,
    detect_spoken_language_switch,
)
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.predefined_tools_service import PredefinedToolsService, get_tool
from app.services.prosody import (
    DEFAULT_TONE,
    ProsodyChunk,
    prosody_for,
    stream_prosody_chunks,
    style_prosody,
)
from app.services.sarvam_voice_service import SarvamVoiceService

# Re-export shims: these helpers were defined in this module before the
# voice_stream extraction; tests and services import them from this path,
# which stays canonical. The remaining code below refers to them by bare
# name through these imports.
from app.services.voice_stream.audio import (  # noqa: F401
    _extract_pcm_from_wav,
    _pcm16le_to_wav,
)
from app.services.voice_stream.call_texts import (  # noqa: F401
    _BUSY_OUTROS,
    _DEFAULT_QUESTIONNAIRE_OUTROS,
    _LATENCY_GUARD_INBOUND,
    _LATENCY_GUARD_OUTBOUND,
    _OUTBOUND_OPENER_DELAY_SECONDS,
    _OUTRO_DRAIN_SECONDS,
    _answer_is_outro,
    _busy_outro,
    _default_questionnaire_outro,
    _latency_guard_text,
    _no_response_goodbye_text,
    _quick_ack_text,
    _site_visit_confirm_text,
    _voicemail_message,
)
from app.services.voice_stream.eou import (  # noqa: F401
    _EOU_CLOSER_TAILS,
    _EOU_DISCOURSE_PARTICLES,
    _EOU_NUMBER_CONTEXT_WORDS,
    _EOU_PRONOUN_CONTRACTION_ROOTS,
    _EOU_STRONG_CONTINUATION,
    _EOU_TIME_CONNECTORS,
    _EOU_TIME_WORDS,
    _EOU_WEAK_CONTINUATION,
    _EOU_WEAK_FRAGMENT_MAXWORDS,
    _EOU_YESNO_WORDS,
    _eou_completeness_tier,
    _eou_token_is_timeish,
    _question_answer_kind,
    _verbatim_prespeech_delay_s,
)
from app.services.voice_stream.tts_pump import (  # noqa: F401
    _TTS_BATCH_MAX,
    _TtsPump,
    _campaign_voice_style,
    _scaled_pace,
)
from app.services.voice_stream.utterance_checks import (  # noqa: F401
    _BACKCHANNEL_WORDS,
    _CHECK_IN_CONTAINS,
    _CHECK_IN_EXACT,
    _INV_NOUN,
    _VOICEMAIL_PHRASES,
    _is_backchannel_utterance,
    _is_check_in_utterance,
    _is_project_inventory_question,
    _is_site_visit_confirmation_turn,
    _is_voicemail_utterance,
)
# _resolve_business_type moved to voice_stream/openers.py (used by the
# extracted opener flow too); re-exported here for the remaining callers.
from app.services.voice_stream.openers import _resolve_business_type  # noqa: F401, E402
# _drain_turn moved to voice_stream/text_turn.py; re-exported for remaining callers.
from app.services.voice_stream.text_turn import _drain_turn  # noqa: F401, E402





def _outbound_post_call_targets(
    contact: dict[str, Any] | None,
    *,
    has_outbound_ctx: bool,
    campaign_id: Any,
) -> tuple[bool, bool]:
    """Decide what post-call work an outbound call's contact needs.

    Returns ``(run_post_call_block, has_followup_target)``:
      * ``has_followup_target`` — the contact points at a lead / customer ROW
        (``lead_id`` or ``customer_id``), so the condenser can write a handoff
        note onto it and schedule a follow-up.
      * ``run_post_call_block`` — whether to run the post-call block at all. True
        for a follow-up target OR any real campaign DIAL (it has a
        ``call_link_id`` AND a ``campaign_id``). The second arm is what lets bulk
        CSV lead-capture contacts — which carry NEITHER a ``lead_id`` nor a
        ``customer_id`` — still get SCORED post-call (the Lead Score that powers
        the Qualified Leads tab). Without it those campaigns were never scored,
        so no contact was ever marked qualified and the tab stayed empty.

    Pure + unit-testable: this is exactly the gate that used to drop bulk
    campaigns on the floor. A live tester call has no ``call_link_id`` and so is
    (correctly) excluded — it has its own classifier path.
    """
    if not (has_outbound_ctx and isinstance(contact, dict)):
        return False, False
    has_followup_target = bool(contact.get("lead_id") or contact.get("customer_id"))
    is_campaign_call = bool(contact.get("call_link_id") and campaign_id)
    return (has_followup_target or is_campaign_call), has_followup_target
# _site_visit_out_of_hours_reply moved to voice_stream/text_turn.py; re-exported for remaining callers.
from app.services.voice_stream.text_turn import _site_visit_out_of_hours_reply  # noqa: F401, E402


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
        # Body extracted to app.services.voice_stream.openers._inbound_opening_text
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.openers import _inbound_opening_text

        return _inbound_opening_text(language)

    @staticmethod
    async def _load_recent_record_for_phone(
        db: AsyncSession | None,
        organization_id: Any,
        phone: str | None,
    ) -> dict[str, Any] | None:
        # Body extracted to app.services.voice_stream.openers._load_recent_record_for_phone
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.openers import _load_recent_record_for_phone

        return await _load_recent_record_for_phone(db, organization_id, phone)

    @staticmethod
    def _returning_caller_opener(
        record: dict[str, Any],
        language: str | None,
        *,
        outcome_history: list[dict[str, Any]] | None = None,
    ) -> str:
        # Body extracted to app.services.voice_stream.openers._returning_caller_opener
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.openers import _returning_caller_opener

        return _returning_caller_opener(record, language, outcome_history=outcome_history)

    @staticmethod
    async def _outbound_opener_known_facts(
        db: AsyncSession | None,
        tenant_res: TenantResources,
        campaign_context: dict[str, Any] | None,
    ) -> dict[str, Any]:
        # Body extracted to app.services.voice_stream.openers._outbound_opener_known_facts
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.openers import _outbound_opener_known_facts

        return await _outbound_opener_known_facts(db, tenant_res, campaign_context)

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
        # Persist the transcript for the Transcripts page (best-effort; the store
        # swallows its own errors so a transcript failure never affects the call
        # log below). A rolling 1-month retention purges it later.
        try:
            from app.services.transcript_service import TranscriptService

            _contact = (campaign_context or {}).get("contact") or {}
            _phone = _contact.get("phone") if isinstance(_contact, dict) else None
            await TranscriptService.store(
                db,
                organization_id=tenant_res.organization_id,
                tenant_id=tenant_res.tenant_id,
                call_id=call_id,
                history=history,
                duration_seconds=duration_seconds,
                kind="outbound" if campaign_context else "inbound",
                caller_phone=str(_phone) if _phone else None,
            )
        except Exception:
            logger.debug("NOKVO-TRANSCRIPT: teardown persist failed", exc_info=True)
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
                organization_id_uuid,
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
        eou_fired_at: float | None = None,
        eou_tier: str | None = None,
    ) -> None:
        # ``eou_fired_at`` is a ``perf_counter()`` reading anchored at the
        # caller's end-of-speech (the EOU silence-countdown start). When present
        # it lets the latency guard size its wait against the strict sub-1s
        # budget — see the guard loop below. ``None`` on manual / proactive
        # turns, which fall back to the fixed VOICE_FIRST_SENTENCE_TIMEOUT_MS
        # ceiling.
        # Body extracted to app.services.voice_stream.text_turn._run_text_turn
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.text_turn import _run_text_turn

        return await _run_text_turn(NokvoOneVoiceStreamService, websocket, tenant_res, text, db=db, language=language, call_id=call_id, company_name=company_name, campaign_context=campaign_context, source=source, retrieval_text=retrieval_text, turn_state=turn_state, arbiter=arbiter, language_state=language_state, outbound_context=outbound_context, after_turn=after_turn, eou_fired_at=eou_fired_at, eou_tier=eou_tier)

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
        # Body extracted to app.services.voice_stream.text_turn._process_blob_utterance
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.text_turn import _process_blob_utterance

        return await _process_blob_utterance(NokvoOneVoiceStreamService, websocket, tenant_res, audio_bytes, db=db, fallback_language=fallback_language, call_id=call_id, company_name=company_name, campaign_context=campaign_context, session_locked_language=session_locked_language, prev_turn=prev_turn, prev_turn_state=prev_turn_state, turn_state=turn_state, robustness=robustness, outbound_context=outbound_context, after_turn=after_turn)

    @staticmethod
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
        # Body extracted to app.services.voice_stream.call_close._leave_voicemail_and_end
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.call_close import _leave_voicemail_and_end

        return await _leave_voicemail_and_end(websocket, tenant_res, language=language, call_id=call_id, campaign_context=campaign_context, outbound_context=outbound_context, arbiter=arbiter, turn_state=turn_state)

    @staticmethod
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
        # Body extracted to app.services.voice_stream.call_close._end_call_no_response
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.call_close import _end_call_no_response

        return await _end_call_no_response(websocket, tenant_res, language=language, call_id=call_id, campaign_context=campaign_context, arbiter=arbiter, turn_state=turn_state)

    @staticmethod
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
        # Body extracted to app.services.voice_stream.call_close._speak_outro_and_end
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.call_close import _speak_outro_and_end

        return await _speak_outro_and_end(websocket, tenant_res, outro=outro, language=language, call_id=call_id, last_user_text=last_user_text, campaign_context=campaign_context, arbiter=arbiter, turn_state=turn_state, eou_fired_at=eou_fired_at, style=style)

    @staticmethod
    async def _deliver_verbatim_question(
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
        # Body extracted to app.services.voice_stream.verbatim._deliver_verbatim_question
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.verbatim import _deliver_verbatim_question

        return await _deliver_verbatim_question(NokvoOneVoiceStreamService, websocket, tenant_res, cleaned=cleaned, language=language, call_id=call_id, outbound_context=outbound_context, arbiter=arbiter, turn_state=turn_state, campaign_context=campaign_context, eou_fired_at=eou_fired_at)

    @staticmethod
    async def _persist_question_delivered(
        tenant_res: TenantResources, call_id: str | None, number: int | None
    ) -> None:
        # Body extracted to app.services.voice_stream.verbatim._persist_question_delivered
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.verbatim import _persist_question_delivered

        return await _persist_question_delivered(tenant_res, call_id, number)

    @staticmethod
    async def _play_opener(
        websocket: WebSocket,
        tenant_res: TenantResources,
        opening_text: str,
        *,
        language: str,
        call_id: str | None = None,
        campaign_context: dict[str, Any] | None = None,
        style: str = "",
    ) -> None:
        # Body extracted to app.services.voice_stream.openers._play_opener
        # (turn_router helpers pattern; wrapper preserves the class API
        # and class-attribute monkeypatches).
        from app.services.voice_stream.openers import _play_opener

        return await _play_opener(websocket, tenant_res, opening_text, language=language, call_id=call_id, campaign_context=campaign_context, style=style)

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
        on_session_end: Any | None = None,
    ) -> None:
        await websocket.accept()
        # Sticky LLM-pool routing: bind this call's id so every turn hashes to the
        # same pool box (→ prompt-cache hits). Turn tasks created below copy this
        # context, so all descendant LLM calls inherit it.
        from app.services.llm_pool import set_call_id
        set_call_id(call_id)
        # Per-call vendor-usage sink (STT seconds / LLM tokens / TTS chars).
        # Installed BEFORE any turn tasks spawn so they inherit the contextvar
        # and every increment lands on this one object; priced into the
        # CallCost COGS columns at teardown. See app/services/call_usage.py.
        from app.services.call_usage import begin_call_usage, end_call_usage
        call_usage, _usage_token = begin_call_usage()
        session_started = perf_counter()
        # Wall-clock anchor for the billing ledger. ``perf_counter`` gives us
        # an accurate elapsed-time delta for runtime metrics, but the cost
        # row needs a real UTC timestamp the dashboard can render and an
        # ``ended_at`` set from the same clock at teardown.
        session_started_at = datetime.now(timezone.utc)
        # Eagerly snapshot the tenant attributes used by end-of-call hooks
        # (call-cost ledger, lead persistence, follow-up enqueue). The DB
        # session lives the entire WS call and accumulates many commits
        # along the way; even with ``expire_on_commit=False`` on the
        # sessionmaker, ``tenant_res`` can end up detached / expired in
        # ways that make later attribute access raise MissingGreenlet from
        # an implicit lazy-load. Capturing scalars HERE (one synchronous
        # read while the instance is freshly attached) lets every late
        # consumer use the primitive instead of the ORM attribute.
        tenant_id_str = str(tenant_res.tenant_id)
        organization_id_uuid = tenant_res.organization_id
        language = SarvamVoiceService.normalize_language(language)
        call_id = call_id or str(uuid.uuid4())
        # ── LangSmith root trace for this voice call ─────────────────
        # Wraps the entire session. Inner per-turn / per-LLM spans
        # auto-attach via the tracing_context contextvar set inside
        # the helper. No-op when LANGSMITH_API_KEY is unset.
        from app.services.langsmith_tracer import trace_call as _ls_trace_call
        _ls_call_meta = {
            "language": language,
            "campaign_id": str((campaign_context or {}).get("campaign_id")) if isinstance(campaign_context, dict) and (campaign_context or {}).get("campaign_id") else None,
            "is_followup": bool(isinstance(campaign_context, dict) and (campaign_context or {}).get("is_followup")),
            "outbound": outbound_context_override is not None or bool(isinstance(campaign_context, dict) and (campaign_context or {}).get("campaign_id")),
        }
        # ── OpenTelemetry root span for this call ────────────────────
        # Outermost so its W3C trace id is active when the LangSmith run
        # opens (cross-linked via otel_trace_id) and so every log line
        # emitted during the call carries [trace=<id>] via the logging
        # filter. No-op when OTEL_ENABLED is false.
        from app.services.otel_tracer import trace_call_span, current_trace_id
        _caller_phone_for_trace = (campaign_context or {}).get("from_phone")
        _call_kind_for_trace = "outbound" if _ls_call_meta.get("outbound") else "inbound"
        async with trace_call_span(
            call_id=call_id,
            tenant_id=tenant_id_str,
            caller_phone=_caller_phone_for_trace,
            kind=_call_kind_for_trace,
        ), _ls_trace_call(
            call_id=call_id,
            tenant_id=tenant_id_str,
            organization_id=organization_id_uuid,
            otel_trace_id=(current_trace_id() or None),
            **_ls_call_meta,
        ) as _ls_call_run:
            # The single anchor line support greps for: phone+time → trace_id.
            _otel_trace_id = current_trace_id() or None
            logger.info(
                "NOKVO-CALL-START call_id=%s tenant=%s kind=%s caller=%s trace_id=%s",
                call_id, tenant_id_str, _call_kind_for_trace,
                _caller_phone_for_trace or "-", _otel_trace_id or "-",
            )
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
            elif (
                outbound_context is None
                and isinstance(campaign_context, dict)
                and campaign_context.get("is_followup")
                and campaign_context.get("customer_id")
            ):
                # Customer-targeted manual follow-up (clinic path): no
                # campaign row exists — synthesize the proactive context from
                # the admin's note + the clinic services catalog. Gated to
                # clinics; other verticals fall back to legacy goal-only.
                try:
                    _bt_for_outbound = await _resolve_business_type(db, tenant_res)
                    if _bt_for_outbound == "clinics":
                        from app.services.clinic_outbound_context import (
                            build_clinic_followup_context,
                        )

                        outbound_context = await build_clinic_followup_context(
                            db,
                            organization_id=organization_id_uuid,
                            admin_note=campaign_context.get("admin_note"),
                            customer_name=(campaign_context.get("contact") or {}).get("name"),
                            company_name=company_name,
                        )
                except Exception as exc:
                    logger.warning(
                        f"NOKVO-OUTBOUND: clinic followup context build failed: {exc!r}"
                    )
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
                # Escalation ladder: nudge ONCE, then CUT. The caller answered but
                # stayed silent through the nudge → speak a brief goodbye and hang
                # up rather than holding the line (and slot) open indefinitely.
                if nudge_count >= 1:
                    if campaign_context is not None and campaign_context.get("_no_response_ended"):
                        return  # already cut (one-shot)
                    if campaign_context is not None:
                        campaign_context["_no_response_ended"] = True
                    if proactive_watchdog is not None:
                        proactive_watchdog.cancel()
                    await NokvoOneVoiceStreamService._end_call_no_response(
                        websocket,
                        tenant_res,
                        language=session_locked_language[0] or language,
                        call_id=call_id,
                        campaign_context=campaign_context,
                        arbiter=robustness.arbiter,
                        turn_state=turn_state,
                    )
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
            # WS2: one-shot, fire-and-forget prompt-cache prime. Warm the static
            # system prefix for this call's language NOW (concurrently with the
            # greeting + caller's first utterance) so turn 1 hits the provider
            # cache too — biggest TTFT win on Hindi/Telugu's longer prefixes. The
            # task inherits the set_call_id() context above, so it reserves the
            # same sticky pool box the real turns use. Best-effort: own DB session,
            # all errors swallowed inside.
            asyncio.create_task(
                NokvoOneVoicePipeline.prime_prefix_cache(
                    tenant_res,
                    language=language,
                    call_id=call_id,
                    company_name=company_name,
                    outbound_context=outbound_context,
                )
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
            # Outbound (an active campaign) gets its own humanization knobs:
            # direction-tunable EOU tiers + barge-in immunity + a slower pace.
            # Inbound keeps the global behaviour unchanged.
            is_outbound = bool((campaign_context or {}).get("campaign_id"))
            # Adaptive endpointing tiers (see module-level _eou_completeness_tier):
            # fire fast on high-confidence-complete utterances, a moderate wait on
            # ambiguous declaratives, and keep DEBOUNCE+BONUS for trailing-off
            # speech. The continuation word list now lives at module level
            # (_EOU_CONTINUATION_TAIL_WORDS) so the classifier is unit-testable.
            # Outbound reads the *_OUTBOUND settings (default == global, so no
            # behaviour change until an operator tunes them).
            if is_outbound:
                EOU_DEBOUNCE_MS = max(500, int(settings.VOICE_EOU_DEBOUNCE_MS_OUTBOUND))
                EOU_CONTINUATION_BONUS_MS = max(0, int(settings.VOICE_EOU_CONTINUATION_BONUS_MS_OUTBOUND))
                EOU_COMPLETE_MS = max(200, int(settings.VOICE_EOU_COMPLETE_MS_OUTBOUND))
                EOU_NEUTRAL_MS = max(400, int(settings.VOICE_EOU_NEUTRAL_MS_OUTBOUND))
            else:
                EOU_DEBOUNCE_MS = max(500, int(settings.VOICE_EOU_DEBOUNCE_MS))
                EOU_CONTINUATION_BONUS_MS = max(0, int(settings.VOICE_EOU_CONTINUATION_BONUS_MS))
                EOU_COMPLETE_MS = max(200, int(settings.VOICE_EOU_COMPLETE_MS))
                EOU_NEUTRAL_MS = max(400, int(settings.VOICE_EOU_NEUTRAL_MS))
            utterance_segments: list[str] = []
            utterance_language: list[str] = [language]
            eou_timer_task: asyncio.Task | None = None
            # Outbound barge-in immunity: a pending "is this a real interruption?"
            # confirm timer armed on speech_start while the agent is speaking. It
            # fires the cancel only if speech is sustained past
            # VOICE_BARGE_IN_MIN_MS; a speech_end blip (cough) aborts it.
            pending_barge_task: asyncio.Task | None = None
            # End-of-speech anchor for the sub-1s budget. Set on every EOU-timer
            # (re)start — i.e. each time the caller's silence-countdown begins —
            # so the LAST value before the timer actually fires is the true end
            # of caller speech. Threaded into the turn so the latency guard can
            # size its wait from how much of the budget the EOU silence already
            # spent (see _run_text_turn). ``eou_last_tier`` tags the latency record.
            eou_anchor: list[float | None] = [None]
            eou_last_tier: list[str] = ["neutral"]
            # Text of the most recently dispatched turn. If the caller fires a
            # fresh utterance before that turn has begun speaking, draining it
            # would silently drop the caller's words — so _fire_turn folds this
            # text into the next turn instead (carry-forward). Holds the FINAL
            # (possibly already-folded) text so a burst accumulates correctly.
            last_turn_text: list[str | None] = [None]
            # Sticky language lock: when the user explicitly asks to switch
            # ("speak in Telugu", "Hindi please") we lock that choice for the
            # rest of the session. Without this, Sarvam's per-segment language
            # detection flaps the reply language mid-conversation, especially on
            # code-switched utterances.
            session_locked_language: list[str | None] = [None]
            utterance_language_detected: list[bool] = [False]
            # Per-utterance STT language confidence (Sarvam language_probability),
            # threaded into detect_spoken_language_switch so a low-confidence
            # label can't flip the call's reply language.
            utterance_language_conf: list[float | None] = [None]
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
                            db, organization_id_uuid, caller_phone
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
                            organization_id=organization_id_uuid,
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
                    style=_campaign_voice_style(outbound_context),
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
                                "agc_gain": round(float(getattr(getattr(websocket, "_enhancer", None), "gain", 0.0) or 0.0), 3),
                                "speech_prob": round(float(getattr(getattr(websocket, "_denoiser", None), "last_speech_prob", 0.0) or 0.0), 3),
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
                #   2) The caller simply STARTED speaking another language than
                #      the one locked — follow it for the rest of the call
                #   3) Previously-locked session language (sticky)
                #   4) Sarvam's per-segment STT language detection (first lock)
                requested = detect_language_switch(text)
                spoken_switch = None
                if not requested and session_locked_language[0]:
                    spoken_switch = detect_spoken_language_switch(
                        text,
                        utterance_language[0],
                        session_locked_language[0],
                        confidence=utterance_language_conf[0],
                    )
                if requested or spoken_switch:
                    normalized = SarvamVoiceService.normalize_language(requested or spoken_switch)
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

                # Cross-lingual retrieval translate-STT. RETIRED by default: the
                # only consumer was Qdrant/KB retrieval, which now always returns
                # empty (KB_RETIREMENT_REMAINING.md), so this whole branch was up
                # to 800ms of dead serial latency before the LLM on every non-
                # English inbound turn. Gated behind AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED
                # (default False) — with it off, translate_audio stays None and we
                # call _run_text_turn(retrieval_text=None) directly below. The flag
                # + plumbing are kept so retrieval can be re-armed without a code
                # change if it ever returns.
                retrieval_text: str | None = None
                translate_audio: bytes | None = None
                # Outbound mode never consulted retrieval either; the translate is
                # equally wasted there.
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
                    # Trim leading dead air (keep ~100 ms pre-roll) — STT
                    # anchors better when the clip starts near the speech.
                    from app.services.agent_robustness import trim_leading_silence

                    translate_audio = trim_leading_silence(
                        bytes(utterance_audio), sample_rate=sample_rate
                    )
                utterance_audio.clear()

                # Carry-forward fold: the previous turn is still in flight and
                # hasn't begun speaking, yet the caller already started a fresh
                # utterance. Draining it below cancels it and its text is lost —
                # which is how quick consecutive replies ("at around 2 PM",
                # "Alright, thank you.") got NO response. Fold the unspoken text
                # into this turn so the agent still hears it. A genuine barge-in
                # (turn already SPEAKING) is excluded by the `speaking` guard —
                # there the caller is deliberately interrupting.
                if (
                    current_turn is not None
                    and not current_turn.done()
                    and not (turn_state or {}).get("speaking")
                    and last_turn_text[0]
                ):
                    prior = last_turn_text[0].strip()
                    if prior and prior.lower() not in text.lower():
                        text = f"{prior} {text}".strip()

                await _drain_turn(current_turn)
                turn_state = {"speaking": False}
                new_state = turn_state
                last_turn_text[0] = text

                if translate_audio:
                    # Kick the translate call with a hard timeout. We'd rather
                    # use the native transcript than wait for translate to
                    # finish — first-sentence latency on a phone call has to stay
                    # low, and translate only sharpens cross-lingual retrieval.
                    TRANSLATE_TIMEOUT_S = max(0.2, settings.AGENT_TRANSLATE_TIMEOUT_MS / 1000)

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
                            eou_fired_at=eou_anchor[0],
                            eou_tier=eou_last_tier[0],
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
                            eou_fired_at=eou_anchor[0],
                            eou_tier=eou_last_tier[0],
                        )
                    )
                    robustness.arbiter.begin(turn_id="stream-direct", task=current_turn)

            def _cancel_eou_timer() -> None:
                nonlocal eou_timer_task
                if eou_timer_task and not eou_timer_task.done():
                    eou_timer_task.cancel()
                eou_timer_task = None

            # ── Outbound barge-in immunity (sustained-speech gate) ──────────
            def _cancel_pending_barge() -> None:
                nonlocal pending_barge_task
                if pending_barge_task and not pending_barge_task.done():
                    pending_barge_task.cancel()
                pending_barge_task = None

            async def _do_barge_cancel(reason: str) -> None:
                """Honour a real barge-in: cancel the agent's in-flight turn +
                TTS pump, reset the utterance buffers, tell the client."""
                nonlocal pending_barge_task
                # Drop the reference WITHOUT cancelling: this runs from inside the
                # confirm task itself, so .cancel() here would raise CancelledError
                # at the next await and abort the barge mid-way.
                pending_barge_task = None
                await robustness.arbiter.cancel()
                _cancel_eou_timer()
                utterance_segments.clear()
                utterance_audio.clear()
                logger.info("NOKVO-BARGEIN: confirmed (%s) call=%s", reason, call_id)
                await websocket.send_json({"type": "barge_in_detected", "call_id": call_id})

            def _arm_barge_confirm() -> None:
                """Outbound: don't cut on the first energy spike. Wait
                VOICE_BARGE_IN_MIN_MS of sustained speech before cancelling — a
                cough/'uh-huh' ends (speech_end) within the window and aborts."""
                nonlocal pending_barge_task
                if pending_barge_task and not pending_barge_task.done():
                    return  # already waiting on this interruption

                async def _confirm() -> None:
                    try:
                        await asyncio.sleep(settings.VOICE_BARGE_IN_MIN_MS / 1000)
                    except asyncio.CancelledError:
                        return
                    # Still speaking after the window → a genuine interruption.
                    if robustness.arbiter.phase == TURN_SPEAKING:
                        await _do_barge_cancel("sustained")

                pending_barge_task = asyncio.create_task(_confirm())

            def _eou_decision() -> tuple[str, int]:
                """Adaptive end-of-utterance wait → ``(tier, delay_ms)``. Fire fast
                on high-confidence-complete utterances (questions, time/yes-no
                answers), a moderate wait on ambiguous declaratives, and keep the
                long DEBOUNCE+BONUS wait when speech trails off — cutting latency
                without cutting callers off. See module-level _eou_completeness_tier."""
                if not utterance_segments:
                    return "neutral", EOU_NEUTRAL_MS
                full = " ".join(s for s in utterance_segments if s).strip()
                # Expected-answer hint set by the verbatim questionnaire path
                # (it knows the shape of reply the just-asked question invites);
                # cleared whenever a turn falls through to the LLM.
                _kind = (
                    campaign_context.get("_awaiting_answer_kind")
                    if isinstance(campaign_context, dict)
                    else None
                )
                tier = _eou_completeness_tier(full, answer_kind=_kind)
                if tier == "continuation":
                    return tier, EOU_DEBOUNCE_MS + EOU_CONTINUATION_BONUS_MS
                if tier == "fast":
                    return tier, EOU_COMPLETE_MS
                return "neutral", EOU_NEUTRAL_MS

            def _restart_eou_timer() -> None:
                nonlocal eou_timer_task
                _cancel_eou_timer()
                tier, delay_ms = _eou_decision()
                # Anchor the sub-1s clock at THIS moment: the caller just spoke
                # (a new/continued segment restarted the debounce), so the start
                # of this silence-wait is the latest candidate end-of-speech. The
                # value standing when the timer survives to fire is the true eos.
                eou_anchor[0] = perf_counter()
                eou_last_tier[0] = tier

                async def _timer() -> None:
                    try:
                        await asyncio.sleep(delay_ms / 1000)
                        # Latency telemetry: the EOU wait is the dominant pre-turn
                        # cost and is invisible to LangSmith (which spans only the
                        # turn itself). Log the chosen tier + delay so the sub-1s
                        # budget is attributable per turn — alongside LangSmith's
                        # LLM / retrieval / TTS latencies and the turn's
                        # first_sentence_ms / total_ms.
                        logger.info(
                            "NOKVO-LATENCY-EOU: fired tier=%s delay_ms=%d", tier, delay_ms
                        )
                        await _fire_turn()
                    except asyncio.CancelledError:
                        pass

                eou_timer_task = asyncio.create_task(_timer())

            async def _start_stt() -> None:
                nonlocal stt_ws, stt_reader_task
                if stt_ws is not None:
                    return
                # Auto-detect by default so the caller can switch languages
                # mid-call (Sarvam reports the spoken language per segment, and
                # detect_spoken_language_switch follows it). Pinning to the
                # seeded ``language`` would transcribe any other language as
                # garbage and lock the reply language forever.
                stt_ws = await SarvamVoiceService.connect_stt(
                    tenant_res,
                    language=None if settings.SARVAM_STT_AUTO_DETECT_LANGUAGE else language,
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
                                # the SPEAKING phase this is a (potential)
                                # barge-in. Otherwise we just rewind the EOU
                                # timer and wait for the transcript to come
                                # in so _fire_turn can do check-in vs
                                # barge-in classification.
                                verdict = robustness.arbiter.classify_incoming(is_check_in=False)
                                if verdict == "barge_in" and robustness.arbiter.phase == TURN_SPEAKING:
                                    if is_outbound:
                                        # Outbound immunity: a cough or a quick
                                        # "uh-huh" shouldn't cut the agent off.
                                        # Arm a sustained-speech confirm timer;
                                        # cancel only if they keep talking past
                                        # VOICE_BARGE_IN_MIN_MS (a speech_end
                                        # blip below aborts it). Leave the EOU
                                        # timer alone until the barge confirms.
                                        _arm_barge_confirm()
                                    else:
                                        await _do_barge_cancel("immediate")
                                else:
                                    _cancel_eou_timer()
                                continue
                            if event_type == "speech_end":
                                # NOT an authoritative end-of-turn — Sarvam VAD
                                # emits this on every pause. Treat as a hint:
                                # restart the debounce. We only fire when the
                                # user has actually been silent for EOU_DEBOUNCE_MS.
                                # A speech_end while a barge confirm is pending =
                                # a short blip (cough/backchannel) that ended
                                # inside the window → abort the cancel.
                                if pending_barge_task is not None:
                                    _cancel_pending_barge()
                                    logger.info("NOKVO-BARGEIN: suppressed:blip call=%s", call_id)
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
                                utterance_language_conf[0] = parsed.get("language_probability")
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
            if opening:
                # A pre-generated, ready-to-speak opener (real text, NOT an
                # instruction — see the call sites). Played verbatim, zero LLM.
                await NokvoOneVoiceStreamService._play_opener(
                    websocket,
                    tenant_res,
                    opening,
                    language=language,
                    call_id=call_id,
                    campaign_context=campaign_context,
                    style=_campaign_voice_style(outbound_context),
                )
                await _arm_proactive_watchdog()
            elif outbound_context is not None:
                # Use the deterministic, template-filled opener — no LLM call,
                # ~150ms faster first audio. The LLM takes over from turn 2.
                # Personalise from what we already know about this lead (enquiry
                # details + any prior call) so it opens warm, not one-size-fits-all.
                opener_facts = await NokvoOneVoiceStreamService._outbound_opener_known_facts(
                    db, tenant_res, campaign_context
                )
                outbound_opening_text = generate_outbound_opener_text(
                    outbound_context,
                    language=language,
                    known_facts=opener_facts,
                    # Per-call rotation (APEX_OPENER_VARIANTS): a re-dialed lead
                    # hears a different greeting/consent tail on each attempt.
                    variant_seed=call_id,
                )
                # Let the callee's audio path come up before we speak, so the
                # intro isn't clipped (see _OUTBOUND_OPENER_DELAY_SECONDS). The
                # opener still plays before the receive loop starts, so it always
                # leads — we just hold it a beat. Any media arriving during the
                # pause buffers and is drained once we start reading the socket.
                if _OUTBOUND_OPENER_DELAY_SECONDS > 0:
                    await asyncio.sleep(_OUTBOUND_OPENER_DELAY_SECONDS)
                await NokvoOneVoiceStreamService._play_opener(
                    websocket,
                    tenant_res,
                    outbound_opening_text,
                    language=language,
                    call_id=call_id,
                    campaign_context=campaign_context,
                    style=_campaign_voice_style(outbound_context),
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
                                    language=(
                                        None
                                        if settings.SARVAM_STT_AUTO_DETECT_LANGUAGE
                                        else language
                                    ),
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
                # Billing clock: the call ENDS when the media loop exits — stamp
                # it before the drains/lead-creation/logging below, whose
                # (variable, sometimes seconds-long) teardown time otherwise
                # inflated the wallet deduction for every connected call.
                _session_ended_at = datetime.now(timezone.utc)
                # Drain every task that may still be touching the shared
                # ``db`` AsyncSession before ``_log_voice_call`` runs its own
                # query against it.
                await _drain_turn(inbound_opener_task)
                if proactive_watchdog is not None:
                    proactive_watchdog.cancel()
                _cancel_eou_timer()
                _cancel_pending_barge()
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

                    # Prepaid balance deduction applies to CONNECTED inbound +
                    # NON-deterministic outbound calls. Deterministic / bulk-
                    # questionnaire outbound (has_questionnaire) is the operator-
                    # gated add-on billed separately, so it never depletes this
                    # balance; tester sessions never bill.
                    _is_questionnaire = bool(
                        outbound_context is not None
                        and getattr(outbound_context, "has_questionnaire", False)
                    )
                    _deducts_prepaid = cost_kind in ("inbound", "outbound") and not _is_questionnaire
                    await record_call_cost(
                        db,
                        organization_id=organization_id_uuid,
                        tenant_id=tenant_id_str,
                        call_id=str(call_id),
                        started_at=session_started_at,
                        ended_at=_session_ended_at,
                        kind=cost_kind,
                        campaign_id=cost_campaign_id,
                        trace_id=_otel_trace_id,
                        usage=call_usage,
                        deducts_prepaid=_deducts_prepaid,
                    )
                except Exception:
                    logger.exception("NOKVO-VOICE: failed to record call cost")
                finally:
                    end_call_usage(_usage_token)
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
                        promote_business_type = await _resolve_business_type(db, tenant_res)
                        await promote_to_caller_memory(
                            tenant_res,
                            phone=promote_phone,
                            memory=final_memory,
                            business_type=promote_business_type,
                            call_id=call_id,
                        )
                except Exception:
                    logger.exception("NOKVO-MEMORY: promote_to_caller_memory failed at session end")

                # ── Customer base upsert (inbound calls, all tenants) ─────
                # One row per (tenant, caller number): new callers insert,
                # repeat callers bump last_call_at/call_count. Name is
                # fill-only (COALESCE in the service) — a misheard STT name
                # must never overwrite one already on file. Outbound calls
                # are excluded here; customer-targeted follow-ups bump their
                # counters in the campaign status webhook instead.
                try:
                    _cb_outbound = locals().get("outbound_context")
                    _cb_phone = None
                    _cb_contact = None
                    if isinstance(final_campaign_context, dict):
                        _cb_phone = final_campaign_context.get("from_phone")
                        # Outbound calls (campaign or follow-up) always carry a
                        # contact dict; inbound never does. Gate on both so an
                        # outbound follow-up without a campaign context isn't
                        # miscounted as an inbound call.
                        _cb_contact = final_campaign_context.get("contact")
                    if _cb_outbound is None and not _cb_contact and _cb_phone:
                        from app.services.customer_base_service import (
                            upsert_customer_from_call,
                        )

                        _cb_name = None
                        try:
                            if final_memory.has("name"):
                                _cb_name = final_memory.get("name")
                        except Exception:
                            _cb_name = None
                        await upsert_customer_from_call(
                            db,
                            tenant_id=tenant_id_str,
                            phone=str(_cb_phone),
                            name=_cb_name,
                            call_id=str(call_id),
                        )
                except Exception:
                    logger.exception("NOKVO-CUSTOMER: customer_base upsert failed at session end")

                # ── Post-call handoff note (fire-and-forget) ──────────────
                # One cheap LLM call against the global gpt-5.4-mini deployment
                # produces a 3-sentence summary the next follow-up call's
                # preamble reads verbatim. The caller has already hung up so
                # there's no reason to block the WS teardown — schedule the
                # condenser as a background task with its own DB session.
                # Outbound-campaign calls only (inbound has no lead row).
                try:
                    outbound_ctx = locals().get("outbound_context")
                except Exception:
                    outbound_ctx = None
                contact_for_followup = (
                    (final_campaign_context or {}).get("contact")
                    if isinstance(final_campaign_context, dict)
                    else None
                )
                # A lead/customer-targeted call has a ROW to write the handoff
                # note back to (OutgoingLead / CustomerBase) and to schedule a
                # follow-up from. A bulk CSV campaign contact has NEITHER — it is
                # just {phone, name, call_link_id} — but it STILL needs post-call
                # SCORING (the Lead Score that powers the Qualified Leads tab) and
                # a Call Note. ``_run_post_call_block`` is True for both; the two
                # background tasks below self-select what each of them does.
                # (Previously this gate required lead_id/customer_id, so bulk
                # questionnaire campaigns were never scored and the Qualified
                # Leads tab stayed empty.)
                _run_post_call_block, _has_followup_target = _outbound_post_call_targets(
                    contact_for_followup,
                    has_outbound_ctx=outbound_ctx is not None,
                    campaign_id=(
                        getattr(outbound_ctx, "campaign_id", None)
                        if outbound_ctx is not None
                        else None
                    ),
                )
                if _run_post_call_block:
                    _lead_id_raw = contact_for_followup.get("lead_id")
                    # Customer-targeted follow-up (clinic manual path): the note
                    # goes to CustomerBase.last_call_summary, not a lead row.
                    _customer_id_raw = contact_for_followup.get("customer_id")
                    _customer_tenant_id = tenant_id_str
                    _lead_name = (contact_for_followup.get("name") or "").strip() or None
                    _campaign_name = None
                    if isinstance(final_campaign_context, dict):
                        _campaign_name = (
                            final_campaign_context.get("goal")
                            or final_campaign_context.get("campaign_name")
                        )
                    _bg_tenant_res = tenant_res
                    _bg_call_id = call_id
                    # Capture the call's LangSmith root run so the condenser
                    # (which fires after the WS closes and the contextvar is
                    # gone) still appears under the call's trace tree.
                    _bg_call_run = _ls_call_run
                    # Campaign id for the follow-up scheduler. OutgoingLead has
                    # NO campaign_id column (the link lives in
                    # OutboundCampaignContact) — read it off the live campaign
                    # context instead. Coerced to UUID for ``db.get``.
                    _bg_campaign_id = None
                    try:
                        _cid_raw = getattr(outbound_ctx, "campaign_id", None)
                        if _cid_raw:
                            _bg_campaign_id = uuid.UUID(str(_cid_raw))
                    except Exception:
                        _bg_campaign_id = None

                    # ── Interest verdict (powers the Qualified Leads table) ──
                    # The condenser doesn't judge interest, so classify it from
                    # the transcript here. call_link_id is the stable per-contact
                    # key — the webhook's call_id may not be set yet at teardown.
                    _bg_call_link_id = str(contact_for_followup.get("call_link_id") or "")
                    _campaign_goal_for_interest = None
                    try:
                        _campaign_goal_for_interest = getattr(outbound_ctx, "goal", None) or getattr(
                            outbound_ctx, "pitch_summary", None
                        )
                    except Exception:
                        _campaign_goal_for_interest = None

                    # Lead-capture questionnaire snapshot (immutable post-launch).
                    # When the campaign has one, post-call SCORING replaces the
                    # binary interest verdict for this contact. Read off the
                    # already-loaded context — no extra DB round-trip.
                    try:
                        _bg_questions = list(getattr(outbound_ctx, "questions", []) or [])
                        _bg_threshold = int(getattr(outbound_ctx, "question_threshold", 0) or 0)
                    except Exception:
                        _bg_questions, _bg_threshold = [], 0
                    _bg_language = None
                    if isinstance(final_campaign_context, dict):
                        _bg_language = final_campaign_context.get("language") or None

                    async def _classify_and_persist_interest():
                        # Best-effort: judge the call outcome from the transcript
                        # and stamp the verdict onto the matching campaign.contacts
                        # entry. For questionnaire campaigns this is the LEAD SCORE
                        # (score/qualified/breakdown); otherwise the legacy interest
                        # bucket. Surfaced by GET /campaigns → "Qualified Leads".
                        if not (_bg_campaign_id and _bg_call_link_id):
                            return
                        # COGS: this task runs AFTER the CallCost row committed and
                        # the session sink was torn down — its LLM calls (scorer /
                        # outcome classifier / bulk-path condenser) meter into a
                        # fresh sink that flushes onto the row as an atomic delta.
                        from app.services.call_cost_recorder import post_call_llm_attribution

                        async with post_call_llm_attribution(_bg_call_id):
                            await _classify_and_persist_interest_inner()

                    async def _classify_and_persist_interest_inner():
                        try:
                            from app.services.outbound_call_outcome_classifier import (
                                classify_outbound_outcome,
                            )
                            from app.services.lead_score_service import (
                                classify_lead_score,
                                detect_callback_request,
                            )
                            from app.services.outbound_campaign_service import (
                                OutboundCampaignService,
                            )
                            from app.db.session import AsyncSessionLocal
                            from sqlalchemy.orm.attributes import flag_modified

                            try:
                                _hist = await AgentSessionStore.get_history(_bg_tenant_res, _bg_call_id)
                            except Exception:
                                _hist = []
                            # {role, content} history → {query, answer} turn pairs
                            # (the shape classify_outbound_outcome expects).
                            _turns: list[dict[str, Any]] = []
                            _pending_user: str | None = None
                            for _e in _hist or []:
                                _role = (_e.get("role") or "").lower()
                                _content = (_e.get("content") or "").strip()
                                if not _content:
                                    continue
                                if _role == "user":
                                    if _pending_user is not None:
                                        _turns.append({"query": _pending_user, "answer": ""})
                                    _pending_user = _content
                                elif _role == "assistant":
                                    _turns.append({"query": _pending_user or "", "answer": _content})
                                    _pending_user = None
                                else:
                                    _turns.append({"query": "", "answer": _content})
                            if _pending_user is not None:
                                _turns.append({"query": _pending_user, "answer": ""})
                            if not _turns:
                                return

                            # Branch: questionnaire campaigns are SCORED (score +
                            # qualified + per-question breakdown); everything else
                            # keeps the legacy interest bucket. The LLM call runs
                            # OUTSIDE the FOR UPDATE lock (never hold a row lock
                            # across model latency).
                            _stamp: dict[str, Any]
                            _log: str
                            if _bg_questions:
                                _ls = await classify_lead_score(
                                    _bg_tenant_res,
                                    transcript_turns=_turns,
                                    questions=_bg_questions,
                                    threshold=_bg_threshold,
                                    language=_bg_language,
                                    campaign_name=_campaign_name,
                                )
                                _stamp = {
                                    "lead_score": _ls.score,
                                    "max_score": _ls.max_score,
                                    "qualified": _ls.qualified,
                                    "score_breakdown": _ls.breakdown,
                                    "lead_score_reason": _ls.reason,
                                    "lead_score_degraded": _ls.degraded,
                                    # Derived so legacy tabs/condenser/follow-up
                                    # that read interest_outcome keep working.
                                    "interest_outcome": _ls.interest_outcome,
                                    "interest_reason": _ls.reason,
                                }
                                _log = (
                                    f"lead_score {_ls.score}/{_ls.max_score} "
                                    f"qualified={_ls.qualified}"
                                )
                            else:
                                _outcome = await classify_outbound_outcome(
                                    _bg_tenant_res,
                                    transcript_turns=_turns,
                                    campaign_name=_campaign_name,
                                    campaign_goal=_campaign_goal_for_interest,
                                )
                                _stamp = {
                                    "interest_outcome": _outcome.outcome,
                                    "interest_reason": _outcome.reason,
                                }
                                _log = f"interest {_outcome.outcome}"

                            # A contact that QUALIFIED (questionnaire campaigns)
                            # or read as INTERESTED (non-questionnaire) becomes a
                            # qualified-lead "ticket" in the campaign's Qualified
                            # Leads tab. Attach a human Call Note (the same
                            # condenser the lead/clinic paths use) so the operator
                            # sees what happened without opening the transcript —
                            # the contact already carries the phone number. The
                            # condense LLM call stays OUTSIDE the FOR UPDATE lock,
                            # mirroring the scorer above; it reuses the history we
                            # already fetched. Best-effort: a missing note just
                            # leaves the row showing phone + score, no summary.
                            #
                            # Only the bulk path (no lead/customer row) needs the
                            # scorer to produce the note — lead/customer calls get
                            # theirs from the condenser task, which ALSO writes it
                            # onto the lead row; doing it here too would condense
                            # the same call twice.
                            _surfaces_as_qualified = (
                                bool(_ls.qualified)
                                if _bg_questions
                                else _outcome.outcome == "interested"
                            )
                            # Busy / call-me-later: a connected caller who asked
                            # to be called back is a RE-DIALABLE outcome, not a
                            # rejection — stamp the flag so they land in the
                            # "busy" bucket (campaign card → Call busy button)
                            # instead of Not Interested. Qualified always wins:
                            # a lead who qualified AND said "call me later" is
                            # still a qualified lead.
                            if not _surfaces_as_qualified:
                                try:
                                    if detect_callback_request(_turns):
                                        _stamp["callback_requested"] = True
                                        _log = f"{_log} callback_requested"
                                except Exception:
                                    logger.exception(
                                        "NOKVO-OUTBOUND-INTEREST: callback detection failed"
                                    )
                            if _surfaces_as_qualified and not _has_followup_target:
                                try:
                                    from app.services.call_condenser_service import (
                                        condense_call,
                                    )

                                    _note = await condense_call(
                                        tenant_res=_bg_tenant_res,
                                        call_id=_bg_call_id,
                                        lead_name=_lead_name,
                                        campaign_name=_campaign_name,
                                        transcript=_hist,
                                        timeout_s=8.0,
                                    )
                                except Exception:
                                    logger.exception(
                                        "NOKVO-OUTBOUND-INTEREST: call-note condense failed"
                                    )
                                    _note = None
                                if _note:
                                    _stamp["call_note"] = _note
                                    _stamp["call_note_generated_at"] = datetime.now(
                                        timezone.utc
                                    ).isoformat()
                                elif _bg_questions and _ls.reason:
                                    # Short call → the condenser returned nothing,
                                    # but a qualified ticket should never be
                                    # note-less. Fall back to the score summary
                                    # (e.g. "Scored 4/5 (threshold 3). Earned: …").
                                    _stamp["call_note"] = _ls.reason
                                    _stamp["call_note_generated_at"] = datetime.now(
                                        timezone.utc
                                    ).isoformat()

                            # V2: write the verdict to the contact ROW (O(1), no
                            # blob lock). update_status_by_link touches only a V2
                            # row, so it's a no-op (rowcount 0) for a legacy blob
                            # campaign → we fall back to the blob write below.
                            _wrote_v2 = False
                            if settings.CAMPAIGN_CONTACTS_V2:
                                try:
                                    from app.services import campaign_contacts_v2 as _v2

                                    _qual = bool(
                                        _stamp.get("qualified")
                                        if _bg_questions
                                        else (_stamp.get("interest_outcome") == "interested")
                                    )
                                    _score = _stamp.get("lead_score")
                                    _result = {k: v for k, v in _stamp.items()
                                               if k not in ("qualified", "lead_score")}
                                    async with AsyncSessionLocal() as _rdb:
                                        _wrote_v2 = await _v2.update_status_by_link(
                                            _rdb, _bg_call_link_id, "completed",
                                            qualified=_qual, lead_score=_score, result=_result,
                                        )
                                        if _wrote_v2:
                                            await _rdb.commit()
                                            logger.info(
                                                "NOKVO-OUTBOUND-INTEREST: %s for call %s (v2 row)",
                                                _log, _bg_call_id,
                                            )
                                            # CRM result webhook: the verdict is
                                            # final on the row → queue delivery
                                            # (no-op unless the campaign has a
                                            # Result Webhook URL). Fail-soft.
                                            from app.services.crm_webhook_service import (
                                                enqueue_for_link,
                                            )

                                            await enqueue_for_link(_rdb, _bg_call_link_id)
                                except Exception:
                                    logger.exception("NOKVO-OUTBOUND-INTEREST: v2 row write failed")

                            # Legacy blob write (only when the V2 row wasn't the
                            # target). Under the campaign FOR UPDATE lock — the only
                            # safe way to touch the contacts JSON.
                            if not _wrote_v2:
                                async with AsyncSessionLocal() as _idb:
                                    _camp = await OutboundCampaignService._lock_campaign(
                                        _idb, _bg_campaign_id
                                    )
                                    if _camp is None:
                                        return
                                    _contacts = list(_camp.contacts or [])
                                    _changed = False
                                    for _ct in _contacts:
                                        if str(_ct.get("call_link_id") or "") == _bg_call_link_id:
                                            _ct.update(_stamp)
                                            _changed = True
                                            break
                                    if _changed:
                                        _camp.contacts = _contacts
                                        flag_modified(_camp, "contacts")
                                        _idb.add(_camp)
                                        await _idb.commit()
                                        logger.info(
                                            "NOKVO-OUTBOUND-INTEREST: %s for call %s",
                                            _log, _bg_call_id,
                                        )
                        except Exception:
                            logger.exception("NOKVO-OUTBOUND-INTEREST: classify/persist failed")

                    async def _condense_and_persist():
                        # COGS: post-teardown task — meter its LLM calls into a
                        # fresh sink, flushed onto the CallCost row as a delta.
                        from app.services.call_cost_recorder import post_call_llm_attribution

                        async with post_call_llm_attribution(_bg_call_id):
                            await _condense_and_persist_inner()

                    async def _condense_and_persist_inner():
                        try:
                            from app.services.call_condenser_service import condense_call
                            from app.models.outgoing_lead import OutgoingLead
                            from app.db.session import AsyncSessionLocal

                            # Re-establish the LangSmith parent context for
                            # this background task. tracing_context is a sync
                            # contextmanager; the contextvar it sets stays
                            # bound for the duration of this task once we
                            # enter it manually.
                            _ls_tcm = None
                            if _bg_call_run is not None:
                                try:
                                    from langsmith.run_helpers import tracing_context
                                    _ls_tcm = tracing_context(parent=_bg_call_run)
                                    _ls_tcm.__enter__()
                                except Exception:
                                    _ls_tcm = None

                            note = await condense_call(
                                tenant_res=_bg_tenant_res,
                                call_id=_bg_call_id,
                                lead_name=_lead_name,
                                campaign_name=_campaign_name,
                                timeout_s=8.0,
                            )
                            if not note:
                                return

                            # ── Outbound parity with the inbound condenser ──
                            # Write the SAME note onto the lead / site-visit
                            # records THIS call created (the dashboard Leads /
                            # Site Visits tabs live in nokvo_one_tool_records,
                            # NOT the OutgoingLead contact row written below),
                            # and hand a booked site visit to the RE scheduler.
                            # Reuses the single condense above — no second LLM
                            # call. Best-effort + isolated so a record write can
                            # never block the OutgoingLead handoff_note below.
                            try:
                                from app.models.nokvo_one_tool_record import NokvoOneToolRecord
                                from sqlalchemy.orm.attributes import flag_modified

                                _ob_state = await AgentSessionStore.get_state(
                                    _bg_tenant_res, _bg_call_id
                                ) or {}
                                _ob_tf = _ob_state.get("tool_flow") or {}
                                _ob_record_ids: list[str] = []
                                for _idv in (
                                    _ob_state.get("auto_site_visit_id"),
                                    _ob_state.get("auto_lead_id"),
                                    _ob_tf.get("created_record_id"),
                                ):
                                    if _idv:
                                        _ob_record_ids.append(str(_idv))
                                _ob_record_ids = list(dict.fromkeys(_ob_record_ids))
                                if _ob_record_ids:
                                    _now_iso = datetime.now(timezone.utc).isoformat()
                                    _written = 0
                                    async with AsyncSessionLocal() as rec_db:
                                        for _rid in _ob_record_ids:
                                            try:
                                                _ruuid = uuid.UUID(str(_rid))
                                            except (TypeError, ValueError):
                                                continue
                                            _rec = await rec_db.get(NokvoOneToolRecord, _ruuid)
                                            if _rec is None:
                                                continue
                                            _data = dict(_rec.data or {})
                                            _data["handoff_note"] = note
                                            _data["handoff_note_generated_at"] = _now_iso
                                            _data["handoff_note_source"] = "condenser"
                                            _rec.data = _data
                                            flag_modified(_rec, "data")
                                            rec_db.add(_rec)
                                            _written += 1
                                        await rec_db.commit()
                                    logger.info(
                                        "NOKVO-CONDENSE: outbound handoff note written to %d record(s) for call %s (%d chars)",
                                        _written, _bg_call_id, len(note),
                                    )
                                    # Booked site visit → RE scheduler fills the
                                    # Site Visit Fields from the note + assigns
                                    # the nearest-free agent (same as inbound).
                                    _ob_sv_id = _ob_state.get("auto_site_visit_id")
                                    if not _ob_sv_id and _ob_tf.get("flow_key") == "real_estate_site_visit":
                                        _ob_sv_id = _ob_tf.get("created_record_id")
                                    if _ob_sv_id:
                                        try:
                                            from app.services.re_agent_scheduler import REAgentScheduler

                                            async with AsyncSessionLocal() as sched_db:
                                                await REAgentScheduler.schedule_for_site_visit(
                                                    sched_db, _bg_tenant_res, str(_ob_sv_id)
                                                )
                                        except Exception:
                                            logger.exception(
                                                "NOKVO-RE-SCHED: outbound agent scheduling failed"
                                            )
                            except Exception:
                                logger.exception(
                                    "NOKVO-CONDENSE: outbound record note write failed"
                                )

                            if _customer_id_raw and not _lead_id_raw:
                                try:
                                    customer_uuid = uuid.UUID(str(_customer_id_raw))
                                except (TypeError, ValueError):
                                    return
                                from app.services.customer_base_service import (
                                    record_call_summary,
                                )

                                async with AsyncSessionLocal() as bg_db:
                                    written = await record_call_summary(
                                        bg_db,
                                        tenant_id=_customer_tenant_id,
                                        summary=note,
                                        customer_id=customer_uuid,
                                        call_id=str(_bg_call_id),
                                    )
                                if written:
                                    logger.info(
                                        "NOKVO-CONDENSE: call summary written for customer %s (%d chars)",
                                        customer_uuid, len(note),
                                    )
                                return
                            try:
                                lead_uuid = uuid.UUID(str(_lead_id_raw))
                            except (TypeError, ValueError):
                                return
                            _lead_campaign_id = None
                            async with AsyncSessionLocal() as bg_db:
                                lead = await bg_db.get(OutgoingLead, lead_uuid)
                                if lead is not None:
                                    lead.handoff_note = note
                                    lead.handoff_note_generated_at = datetime.now(timezone.utc)
                                    bg_db.add(lead)
                                    await bg_db.commit()
                                    # OutgoingLead has no campaign_id column; the
                                    # campaign comes from the live call context.
                                    _lead_campaign_id = _bg_campaign_id
                                    logger.info(
                                        "NOKVO-CONDENSE: handoff note written for lead %s (%d chars)",
                                        lead_uuid, len(note),
                                    )
                            # Lead → Follow-up agent: read the note for a callback
                            # time the prospect asked for and configure the
                            # follow-up callback (campaign-linked). Best-effort,
                            # fresh session; mirrors the RE_scheduler hook above.
                            if lead is not None:
                                try:
                                    from app.services.lead_followup_note_scheduler import (
                                        LeadFollowupNoteScheduler,
                                    )
                                    from app.models.outbound_campaign import OutboundCampaign

                                    async with AsyncSessionLocal() as fu_db:
                                        _camp = (
                                            await fu_db.get(OutboundCampaign, _lead_campaign_id)
                                            if _lead_campaign_id
                                            else None
                                        )
                                        await LeadFollowupNoteScheduler.schedule_from_note(
                                            fu_db,
                                            tenant_res=_bg_tenant_res,
                                            note=note,
                                            source_call_id=str(_bg_call_id),
                                            lead_id=lead_uuid,
                                            campaign=_camp,
                                        )
                                except Exception:
                                    logger.exception(
                                        "NOKVO-LEAD-FOLLOWUP: outbound scheduling failed"
                                    )
                        except Exception:
                            logger.exception("NOKVO-CONDENSE: background task failed")
                        finally:
                            # Always close the tracing context to keep the
                            # contextvar from leaking across asyncio task
                            # boundaries. _ls_tcm is None when tracing is
                            # disabled or when the SDK import failed.
                            try:
                                if _ls_tcm is not None:
                                    _ls_tcm.__exit__(None, None, None)
                            except Exception:
                                pass

                    # The condenser writes the handoff note onto the lead /
                    # customer row and schedules the follow-up — only meaningful
                    # when there IS such a row. A bulk CSV campaign contact has
                    # none, so skip it (its Call Note is produced by the scorer
                    # task below) rather than burn a condense LLM call per dial.
                    if _has_followup_target:
                        _condense_task = asyncio.create_task(
                            _condense_and_persist(), name=f"condense:{call_id}"
                        )
                        _background_tasks.add(_condense_task)
                        _condense_task.add_done_callback(_background_tasks.discard)

                    _interest_task = asyncio.create_task(
                        _classify_and_persist_interest(), name=f"interest:{call_id}"
                    )
                    _background_tasks.add(_interest_task)
                    _interest_task.add_done_callback(_background_tasks.discard)

                # ── Post-call handoff note for INBOUND records ────────────
                # Inbound leads & site-visits live in nokvo_one_tool_records
                # (there is no OutgoingLead row). A single call may create BOTH
                # a lead and a site visit; it's the same conversation, so we run
                # the condenser ONCE and write the same note onto every record
                # this call created. Created ids are stashed in session state by
                # the deterministic flow + the end-of-call safety net. Best-effort,
                # fire-and-forget (never blocks WS teardown, never a hard gate).
                if outbound_ctx is None:
                    try:
                        _final_state = await AgentSessionStore.get_state(tenant_res, call_id) or {}
                    except Exception:
                        _final_state = {}
                    _inbound_record_ids: list[str] = []
                    for _idk in ("auto_site_visit_id", "auto_lead_id"):
                        _idv = _final_state.get(_idk)
                        if _idv:
                            _inbound_record_ids.append(str(_idv))
                    _tf_created = (_final_state.get("tool_flow") or {}).get("created_record_id")
                    if _tf_created:
                        _inbound_record_ids.append(str(_tf_created))
                    # De-dup, preserve order (a record may be referenced twice).
                    _inbound_record_ids = list(dict.fromkeys(_inbound_record_ids))
                    # Run when the call created records OR when we know the
                    # caller's number — non-booking callers still get their
                    # summary onto the Customer base row so a later manual
                    # follow-up opens with context.
                    if _inbound_record_ids or _cb_phone:
                        _bg_tenant_res_in = tenant_res
                        _bg_call_id_in = call_id
                        _bg_call_run_in = _ls_call_run
                        try:
                            _fm_in = await load_memory(tenant_res, call_id)
                            _bg_lead_name_in = _fm_in.get("name") if _fm_in.has("name") else None
                        except Exception:
                            _bg_lead_name_in = None
                        # The site-visit ticket (if this call booked one) gets handed to
                        # RE_agent_scheduler once its note is written below.
                        _site_visit_id_in = _final_state.get("auto_site_visit_id")

                        async def _condense_and_persist_inbound(
                            record_ids=_inbound_record_ids,
                            lead_name=_bg_lead_name_in,
                            cb_phone=_cb_phone,
                            cb_tenant_id=tenant_id_str,
                            site_visit_id=_site_visit_id_in,
                        ):
                            # COGS: post-teardown task — meter its LLM calls into
                            # a fresh sink, flushed onto the CallCost row.
                            from app.services.call_cost_recorder import post_call_llm_attribution

                            async with post_call_llm_attribution(_bg_call_id_in):
                                await _condense_and_persist_inbound_inner(
                                    record_ids, lead_name, cb_phone, cb_tenant_id, site_visit_id
                                )

                        async def _condense_and_persist_inbound_inner(
                            record_ids, lead_name, cb_phone, cb_tenant_id, site_visit_id
                        ):
                            try:
                                from app.services.call_condenser_service import condense_call
                                from app.models.nokvo_one_tool_record import NokvoOneToolRecord
                                from app.db.session import AsyncSessionLocal
                                from sqlalchemy.orm.attributes import flag_modified

                                _ls_tcm = None
                                if _bg_call_run_in is not None:
                                    try:
                                        from langsmith.run_helpers import tracing_context
                                        _ls_tcm = tracing_context(parent=_bg_call_run_in)
                                        _ls_tcm.__enter__()
                                    except Exception:
                                        _ls_tcm = None
                                try:
                                    note = await condense_call(
                                        tenant_res=_bg_tenant_res_in,
                                        call_id=_bg_call_id_in,
                                        lead_name=lead_name,
                                        timeout_s=8.0,
                                    )
                                    # Enrich the records with the LLM note when we
                                    # got one. When condense fails, the records keep
                                    # the deterministic note written at creation — so
                                    # downstream (RE_agent_scheduler) still has input.
                                    if note:
                                        now_iso = datetime.now(timezone.utc).isoformat()
                                        written = 0
                                        async with AsyncSessionLocal() as bg_db:
                                            for rid in record_ids:
                                                try:
                                                    rec_uuid = uuid.UUID(str(rid))
                                                except (TypeError, ValueError):
                                                    continue
                                                rec = await bg_db.get(NokvoOneToolRecord, rec_uuid)
                                                if rec is None:
                                                    continue
                                                data = dict(rec.data or {})
                                                data["handoff_note"] = note
                                                data["handoff_note_generated_at"] = now_iso
                                                data["handoff_note_source"] = "condenser"
                                                # Reassign + flag_modified so SQLAlchemy
                                                # persists the JSONB change (in-place
                                                # mutation alone is not tracked).
                                                rec.data = data
                                                flag_modified(rec, "data")
                                                bg_db.add(rec)
                                                written += 1
                                            await bg_db.commit()
                                        logger.info(
                                            "NOKVO-CONDENSE: inbound handoff note written to %d record(s) for call %s (%d chars)",
                                            written, _bg_call_id_in, len(note),
                                        )
                                    else:
                                        logger.info(
                                            "NOKVO-CONDENSE: inbound condenser returned no note for call %s "
                                            "(record(s) keep their deterministic note)",
                                            _bg_call_id_in,
                                        )

                                    # RE_agent_scheduler: turn the site-visit ticket
                                    # into a filled, agent-assigned ticket (LLM extract
                                    # the note → fill the Site Visit Fields → assign the
                                    # nearest-free agent). Runs REGARDLESS of whether the
                                    # condenser succeeded — it reads the ticket's current
                                    # handoff_note, which is never empty (deterministic
                                    # note is written at creation). Best-effort, fresh
                                    # session so it never perturbs the note write above.
                                    if site_visit_id:
                                        try:
                                            from app.services.re_agent_scheduler import REAgentScheduler

                                            async with AsyncSessionLocal() as sched_db:
                                                await REAgentScheduler.schedule_for_site_visit(
                                                    sched_db, _bg_tenant_res_in, site_visit_id
                                                )
                                        except Exception:
                                            logger.exception(
                                                "NOKVO-RE-SCHED: inbound agent scheduling failed"
                                            )

                                    # Customer-base summary + Follow-up agent need the
                                    # LLM prose, so they only run when condense produced
                                    # a note. (The site-visit scheduler above does not.)
                                    if note and cb_phone:
                                        # Same note onto the caller's Customer base
                                        # row (best-effort — the row was upserted at
                                        # teardown just before this task started).
                                        try:
                                            from app.services.customer_base_service import (
                                                record_call_summary,
                                            )

                                            async with AsyncSessionLocal() as bg_db2:
                                                await record_call_summary(
                                                    bg_db2,
                                                    tenant_id=cb_tenant_id,
                                                    summary=note,
                                                    phone=str(cb_phone),
                                                    call_id=str(_bg_call_id_in),
                                                )
                                        except Exception:
                                            logger.exception(
                                                "NOKVO-CONDENSE: customer summary write failed"
                                            )
                                        # Inbound caller → Follow-up agent: if the
                                        # note shows the caller asked for a callback
                                        # time, schedule it against their Customer
                                        # base row (campaign-less). Best-effort,
                                        # fresh session; mirrors RE_scheduler.
                                        try:
                                            from app.services.lead_followup_note_scheduler import (
                                                LeadFollowupNoteScheduler,
                                            )

                                            async with AsyncSessionLocal() as fu_db:
                                                await LeadFollowupNoteScheduler.schedule_from_note(
                                                    fu_db,
                                                    tenant_res=_bg_tenant_res_in,
                                                    note=note,
                                                    source_call_id=str(_bg_call_id_in),
                                                    customer_phone=str(cb_phone),
                                                    campaign=None,
                                                )
                                        except Exception:
                                            logger.exception(
                                                "NOKVO-LEAD-FOLLOWUP: inbound scheduling failed"
                                            )
                                finally:
                                    try:
                                        if _ls_tcm is not None:
                                            _ls_tcm.__exit__(None, None, None)
                                    except Exception:
                                        pass
                            except Exception:
                                logger.exception("NOKVO-CONDENSE: inbound background task failed")

                        _condense_task_in = asyncio.create_task(
                            _condense_and_persist_inbound(), name=f"condense-inbound:{call_id}"
                        )
                        _background_tasks.add(_condense_task_in)
                        _condense_task_in.add_done_callback(_background_tasks.discard)

                # Outbound-tester end-of-call hook. Runs after billing/memory so
                # by the time it sends its `outcome` message all the durable
                # side-effects are committed. Wrapped in try/except — a slow
                # classifier or a client that already closed the WS must not
                # surface as a 500 to the operator.
                if on_session_end is not None:
                    try:
                        await on_session_end(websocket)
                    except Exception:
                        logger.exception("NOKVO-VOICE: on_session_end hook failed")
