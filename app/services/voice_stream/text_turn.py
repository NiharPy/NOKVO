"""The per-turn dispatcher: deterministic lanes, LLM turn streaming, and
the vad-blob utterance path.

Extracted from nokvo_one_voice_stream_service.py (turn_router helpers pattern:
functions taking ``helpers`` receive ``NokvoOneVoiceStreamService`` and call
sibling statics through it, so class-attribute monkeypatches keep
working). The class keeps delegating wrappers - no API change.
"""
from __future__ import annotations

import logging
from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    PROACTIVE_NUDGE_PROMPT,
    infer_covered_objectives,
    strip_leading_fillers,
    strip_leading_right_so,
    update_outbound_memory,
)
from app.services.agent_robustness import (
    AudioQualityProbe,
    CLARIFY_ESCALATE,
    CLARIFY_NUDGE,
    CLARIFY_OFFER_OPTIONS,
    CLARIFY_RESET,
    ClarificationState,
    LanguageState,
    QUALITY_UNUSABLE,
    RobustnessContext,
    TURN_SPEAKING,
    TurnArbiter,
    clarification_prompt,
    is_turn_vague,
    repeat_prompt,
)
from app.services.agent_session_store import AgentSessionStore
from app.services.conversational_memory import (
    ConversationalMemory,
    bootstrap_caller_memory,
    load_memory,
    promote_to_caller_memory,
    save_memory,
)
from app.services.language_intent import detect_language_switch, detect_spoken_language_switch
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.prosody import DEFAULT_TONE, ProsodyChunk, prosody_for, stream_prosody_chunks, style_prosody
from app.services.sarvam_voice_service import SarvamVoiceService
from app.services.voice_stream.audio import _extract_pcm_from_wav, _pcm16le_to_wav
from app.services.voice_stream.openers import _resolve_business_type
from app.services.voice_stream.call_texts import (
    _OUTRO_DRAIN_SECONDS,
    _answer_is_outro,
    _busy_outro,
    _default_questionnaire_outro,
    _latency_guard_text,
    _quick_ack_text,
    _site_visit_confirm_text,
)
from app.services.voice_stream.tts_pump import (
    _TTS_BATCH_MAX,
    _TtsPump,
    _campaign_voice_style,
    _scaled_pace,
)
from app.services.voice_stream.utterance_checks import (
    _is_backchannel_utterance,
    _is_check_in_utterance,
    _is_project_inventory_question,
    _is_site_visit_confirmation_turn,
    _is_voicemail_utterance,
)
from datetime import datetime, timezone
from fastapi import WebSocket
from sqlalchemy.ext.asyncio import AsyncSession
from time import perf_counter
from typing import Any
import asyncio
import contextlib
import uuid

logger = logging.getLogger(__name__)


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


async def _site_visit_out_of_hours_reply(db, tenant_res, text: str, language: str | None) -> str | None:
    """When the caller names a site-visit time OUTSIDE the org's working window,
    return a localized rejection that states the window and offers the closest
    valid slot. Returns ``None`` when the time is in-hours, unparseable, or the
    org hasn't configured hours — the caller then falls through to the normal
    templated confirmation.

    This guards the fast booking-confirmation path (the 6ms templated reply),
    which short-circuits before the tool-flow executor where the same check
    also runs. Without it the agent would cheerfully "note" an 8 PM visit when
    hours end at 7 PM."""
    try:
        from datetime import datetime, timezone

        from app.services.nokvo_one_assignment_service import (
            NokvoOneAssignmentService,
            _within_working_window,
            suggest_within_working_hours,
        )

        org_id = getattr(tenant_res, "organization_id", None)
        if org_id is None or db is None:
            return None
        defaults = await NokvoOneAssignmentService.resolve_org_working_window(db, org_id)
        if defaults is None:
            return None

        from app.services.voice_turn_policy import extract_turn_entities
        from app.services.nokvo_one_voice_pipeline import (
            NokvoOneVoicePipeline,
            _APPOINTMENT_LOCAL_TZ,
            _AppointmentToolInputError,
        )

        ents = extract_turn_entities(text)
        time_text = ents.get("time_text")
        if not time_text:
            return None  # no concrete time → can't range-check; let confirm proceed
        try:
            visit_date = NokvoOneVoicePipeline._parse_appointment_date(ents.get("date_text") or "today")
            visit_time = NokvoOneVoicePipeline._parse_appointment_time(time_text)
        except _AppointmentToolInputError:
            return None
        visit_dt = datetime.combine(
            visit_date, visit_time, tzinfo=_APPOINTMENT_LOCAL_TZ
        ).astimezone(timezone.utc)
        if _within_working_window(defaults, visit_dt):
            return None
        suggestion = suggest_within_working_hours(defaults, visit_dt)
        return NokvoOneVoicePipeline._site_visit_hours_reprompt(
            requested_dt=visit_dt,
            suggestion_dt=suggestion,
            defaults=defaults,
            language=language,
        )
    except Exception:
        return None


async def _run_text_turn(
    helpers: Any,
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
    cleaned = " ".join((text or "").split())
    if not cleaned:
        return
    # Outbound answering-machine guard: a voicemail greeting gets transcribed
    # as a "caller" turn. Don't pitch to a recording — leave one short message
    # and end the call. One-shot per call via a flag on the shared
    # campaign_context dict (the same object across every turn of the call).
    if (
        campaign_context is not None
        and not campaign_context.get("_voicemail_ended")
        and _is_voicemail_utterance(cleaned)
    ):
        campaign_context["_voicemail_ended"] = True
        await helpers._leave_voicemail_and_end(
            websocket,
            tenant_res,
            language=language,
            call_id=call_id,
            campaign_context=campaign_context,
            outbound_context=outbound_context,
            arbiter=arbiter,
            turn_state=turn_state,
        )
        return
    # ── Deterministic questionnaire CLOSE (gate-fail OR all-answered) ──────
    # The model is unreliable at delivering the closing line once the
    # questionnaire is done — in the field it re-asks the last question, makes
    # up a name question, or loops back to an earlier one instead of closing.
    # Two deterministic close triggers, both handled here with NO LLM turn:
    #   1. gate-fail: the last-asked DEALBREAKER gate question just got its
    #      disqualifying answer. Once questions are asked verbatim there is no
    #      LLM turn to notice the dealbreaker, so we enforce it here (also a
    #      strict improvement for the LLM path — it can't miss the gate).
    #   2. all-answered: EVERY question asked AND the latest reply is a genuine
    #      answer (not a fragment / re-greeting).
    # One-shot per call via campaign_context["_outro_ended"]. An EMPTY outro
    # no longer disables the close (the prod loop: no outro → no close → the
    # model cycled the questionnaire forever) — a default per-language
    # thank-you line closes instead.
    _intended_q: int | None = None  # the question this turn is expected to ask (LLM path tracking)
    if (
        outbound_context is not None
        and call_id
        and campaign_context is not None
        and not campaign_context.get("_outro_ended")
        and getattr(outbound_context, "has_questionnaire", False)
    ):
        # ── BUSY dealbreaker (deterministic, before any other lane) ────────
        # "I'm busy / call me later" ends the call NOW: play the busy close
        # and hang up — no LLM turn, no next question. The caller's line
        # stays in the history, so the post-call classifier stamps
        # callback_requested and the contact lands in the re-dialable
        # "busy" bucket (Call busy button). An opt-out in the same line
        # ("not interested, don't call later") is NOT a busy cut —
        # is_callback_line refuses it and the disinterest path handles it.
        try:
            from app.services.lead_score_service import is_callback_line

            if is_callback_line(cleaned):
                campaign_context["_outro_ended"] = True
                logger.info(
                    "NOKVO-BUSY-CUT: caller asked to be called back — closing call %s",
                    call_id,
                )
                await helpers._speak_outro_and_end(
                    websocket,
                    tenant_res,
                    outro=_busy_outro(language),
                    language=language,
                    call_id=call_id,
                    last_user_text=cleaned,
                    campaign_context=campaign_context,
                    arbiter=arbiter,
                    turn_state=turn_state,
                    eou_fired_at=eou_fired_at,
                    style=_campaign_voice_style(outbound_context),
                )
                return
        except Exception:
            logger.exception("NOKVO-BUSY-CUT: busy dealbreaker check failed")
        _qoutro = (getattr(outbound_context, "question_outro", "") or "").strip()
        # Phase 3: close in the caller's language from the pre-translated outro.
        if settings.APEX_VERBATIM_QUESTIONS_ENABLED and _qoutro:
            from app.services.agent_outbound_context import verbatim_line_for_language

            _qoutro = verbatim_line_for_language(
                getattr(outbound_context, "question_outro_i18n", None), _qoutro, language
            )
        _qs = list(getattr(outbound_context, "questions", []) or [])
        if _qs:
            try:
                from app.services.agent_outbound_context import (
                    gate_failed,
                    next_question_to_advance,
                    questionnaire_is_complete,
                    set_delivered_questions,
                )

                # Install the AUTHORITATIVE delivered set for this turn — the
                # loop-killer. Everything downstream (this close check, the
                # verbatim advance, the LLM prompt's progress directive) reads
                # it via questionnaire_asked_state, so a question the model
                # paraphrased / spoke in native script / that fell out of the
                # history window can never flip back to "unasked".
                _qstate = await AgentSessionStore.get_state(tenant_res, call_id) or {}
                set_delivered_questions(
                    ((_qstate.get("questionnaire_progress") or {}).get("delivered")) or []
                )
                _hist = await AgentSessionStore.get_history(tenant_res, call_id)
                if gate_failed(_qs, _hist, cleaned) or questionnaire_is_complete(_qs, _hist, cleaned):
                    campaign_context["_outro_ended"] = True
                    await helpers._speak_outro_and_end(
                        websocket,
                        tenant_res,
                        outro=_qoutro or _default_questionnaire_outro(language),
                        language=language,
                        call_id=call_id,
                        last_user_text=cleaned,
                        campaign_context=campaign_context,
                        arbiter=arbiter,
                        turn_state=turn_state,
                        eou_fired_at=eou_fired_at,
                        style=_campaign_voice_style(outbound_context),
                    )
                    return
                # A clean forward advance means THIS turn (verbatim or LLM) is
                # expected to ask exactly this question — remember it so the
                # LLM path can persist it as delivered once spoken.
                _plan = next_question_to_advance(_qs, _hist, cleaned)
                if _plan is not None:
                    _intended_q = _plan[0]
            except Exception:
                logger.exception("NOKVO-OUTRO: deterministic close check failed")
    # ── Verbatim per-language question delivery (APEX Phase 3, flag-gated) ──
    # Speak the next questionnaire question from its pre-translated string
    # (cache hit) instead of an LLM turn. Only clean forward advances are
    # handled here; re-asks / non-answers / off-script fall through to the LLM.
    if (
        settings.APEX_VERBATIM_QUESTIONS_ENABLED
        and outbound_context is not None
        and call_id
        and getattr(outbound_context, "has_questionnaire", False)
    ):
        if await helpers._deliver_verbatim_question(
            websocket,
            tenant_res,
            cleaned=cleaned,
            language=language,
            call_id=call_id,
            outbound_context=outbound_context,
            arbiter=arbiter,
            turn_state=turn_state,
            campaign_context=campaign_context,
            eou_fired_at=eou_fired_at,
        ):
            if after_turn is not None:
                _r = after_turn()
                if asyncio.iscoroutine(_r):
                    await _r
            return
    # This turn is going to the LLM — the expected-answer endpointing hint
    # belongs only to a just-delivered verbatim question, so drop it (an
    # LLM re-ask/objection reply invites free-form speech, not a known
    # answer shape).
    if isinstance(campaign_context, dict):
        campaign_context.pop("_awaiting_answer_kind", None)
    if outbound_context is not None and call_id and source != "proactive_silence":
        await AgentSessionStore.merge_state(
            tenant_res,
            call_id,
            {"proactive_silence_nudges": 0},
        )
    turn_id = str(uuid.uuid4())[:8]
    started = perf_counter()
    # ── LangSmith per-turn span ─────────────────────────────
    # One node per user turn. Inner classifier / retrieval / LLM
    # spans auto-attach as children via tracing_context.
    from app.services.langsmith_tracer import trace_turn as _ls_trace_turn
    # OTel turn span (LLM → TTS → response). Attaches to the call span via
    # the contextvar the parent task snapshotted. No-op when OTel is off.
    from app.services.otel_tracer import trace_stage as _otel_trace_stage
    async with _otel_trace_stage("turn", turn_id=turn_id, source=source), _ls_trace_turn(
        turn_id=turn_id,
        user_text=cleaned,
        mode=source,
        language=language,
    ) as _ls_turn_run:

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
            prosody = prosody_for("warm", _campaign_voice_style(outbound_context))
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
            # Roll the in-call summary forward (async, off the latency path):
            # folds messages that just fell past the condense window into the
            # rolling narrative so the agent stays aware of the whole call.
            try:
                from app.services.in_call_summary_service import maybe_update as _summary_update
                asyncio.create_task(_summary_update(tenant_res, call_id, language=language))
            except Exception:
                pass
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

        # Site-visit booking confirmation (inbound real-estate). When the
        # caller has agreed to a visit and just gave a date/time, confirm
        # with a deterministic per-language template instead of letting the
        # LLM free-generate it — the booking turn is exactly where Telugu /
        # Hindi free-gen corrupts. Narrowly gated; falls through to the LLM
        # path when it doesn't apply.
        if source != "proactive_silence" and outbound_context is None:
            try:
                _bt_confirm = await _resolve_business_type(db, tenant_res)
            except Exception:
                _bt_confirm = None
            if _bt_confirm == "real_estate":
                _confirm_history = await AgentSessionStore.get_history(tenant_res, call_id)
                # Out-of-hours guard runs on ANY real-estate turn that states a
                # site-visit time — not only the narrow "confirmation" turn —
                # so the LLM can't conversationally "note" an 8 PM visit across
                # several turns before any deterministic check fires. It returns
                # a rejection only when there's a concrete out-of-hours time.
                from app.services.voice_turn_policy import text_has_datetime

                # NOTE: questions are deliberately NOT excluded here. "Is
                # tomorrow 8 PM possible?" is exactly when the caller must hear
                # "no, we run 9-7" — gating out questions would let an
                # out-of-hours time slip straight to the LLM, which then says
                # yes. The guard still only fires on a concrete out-of-hours
                # time, so in-hours questions fall through to normal Q&A.
                # Resolve the single deterministic reply (if any) for this
                # real-estate turn. Order matters: an explicit inventory
                # question is answered from the catalog FIRST (gpt-4.1-mini
                # otherwise hallucinates a fake portfolio), then the
                # out-of-hours guard, then the booking confirmation.
                confirm = None
                confirm_source = None
                if source != "proactive_silence" and _is_project_inventory_question(cleaned):
                    try:
                        from app.services.real_estate_project_service import (
                            load_active_projects,
                            project_inventory_spoken,
                        )

                        _projs = await load_active_projects(db, getattr(tenant_res, "organization_id", None))
                        _inv = project_inventory_spoken(_projs, language)
                    except Exception:
                        _inv = None
                    if _inv:
                        confirm = _inv
                        confirm_source = "project_inventory"
                if confirm is None:
                    _ooh_reply = None
                    if source != "proactive_silence" and text_has_datetime(cleaned):
                        _ooh_reply = await _site_visit_out_of_hours_reply(db, tenant_res, cleaned, language)
                    if _ooh_reply:
                        confirm = _ooh_reply
                        confirm_source = "site_visit_hours_rejection"
                    elif _is_site_visit_confirmation_turn(cleaned, _confirm_history):
                        from app.services.voice_turn_policy import extract_datetime_phrase

                        confirm = _site_visit_confirm_text(language, extract_datetime_phrase(cleaned))
                        confirm_source = "site_visit_confirmation"
                if confirm is not None:
                    await websocket.send_json(
                        {"type": "stt_finished", "text": cleaned, "turn_id": turn_id, "source": source}
                    )
                    await websocket.send_json(
                        {
                            "type": "agent_sentence",
                            "turn_id": turn_id,
                            "sentence": confirm,
                            "tone": "warm",
                            "first_sentence_ms": int((perf_counter() - started) * 1000),
                            "cache_hit": False,
                            "source": confirm_source,
                        }
                    )
                    prosody = prosody_for("warm")
                    _mark_speaking()
                    try:
                        await SarvamVoiceService.stream_sentence_tts(
                            websocket,
                            tenant_res,
                            confirm,
                            language=language,
                            purpose=confirm_source,
                            pace=prosody.pace,
                            pitch=prosody.pitch,
                            loudness=prosody.loudness,
                        )
                    except Exception:
                        pass
                    await AgentSessionStore.append_turn(tenant_res, call_id, cleaned, confirm)
                    try:
                        from app.services.in_call_summary_service import maybe_update as _summary_update
                        asyncio.create_task(_summary_update(tenant_res, call_id, language=language))
                    except Exception:
                        pass
                    await websocket.send_json(
                        {
                            "type": "agent_answer",
                            "turn_id": turn_id,
                            "answer": confirm,
                            "refused": False,
                            "citations": [],
                            "chunks": [],
                            "runtime": {"mode": confirm_source},
                            "intent": {"type": confirm_source, "should_retrieve": False},
                        }
                    )
                    await websocket.send_json(
                        {
                            "type": "turn_complete",
                            "turn_id": turn_id,
                            "total_ms": int((perf_counter() - started) * 1000),
                            "context_source": confirm_source,
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
        # ── Sub-1s latency record (WS5) ──────────────────────────────────
        # ONE structured per-turn record anchored at end-of-speech, emitted
        # the instant first audio (real OR filler) is dispatched. This is the
        # signal that proves "strictly sub-1s" in prod, broken down so a
        # regression is attributable: eos→eou_fire is the EOU silence wait,
        # eou_fire→first_audio is the turn's compute (route+LLM+TTS dispatch).
        direction = (
            "outbound"
            if (outbound_context is not None and getattr(outbound_context, "is_proactive", False))
            else "inbound"
        )
        _latency_emitted = False
        _first_audio_perf_val: float | None = None
        _first_audio_source: str | None = None
        _content_latency_emitted = False

        def _eos_elapsed_ms(at_perf: float) -> int:
            anchor = eou_fired_at if eou_fired_at is not None else started
            return int((at_perf - anchor) * 1000)

        def _emit_latency_record(*, source: str, cache_hit: bool, first_audio_perf: float) -> None:
            nonlocal _latency_emitted, _first_audio_perf_val, _first_audio_source
            if _latency_emitted:
                return
            _latency_emitted = True
            _first_audio_perf_val = first_audio_perf
            _first_audio_source = source
            eou_fire_to_audio_ms = int((first_audio_perf - started) * 1000)
            if eou_fired_at is not None:
                eos_to_audio_ms = int((first_audio_perf - eou_fired_at) * 1000)
                eos_to_eou_fire_ms = int((started - eou_fired_at) * 1000)
            else:
                eos_to_audio_ms = eou_fire_to_audio_ms
                eos_to_eou_fire_ms = 0
            within_budget = eos_to_audio_ms < settings.VOICE_LATENCY_BUDGET_MS
            logger.info(
                "NOKVO-LATENCY-TURN: language=%s direction=%s tier=%s source=%s "
                "cache_hit=%s eos_to_eou_fire_ms=%d eou_fire_to_first_audio_ms=%d "
                "eos_to_first_audio_ms=%d within_budget=%s",
                language, direction, eou_tier or "-", source, cache_hit,
                eos_to_eou_fire_ms, eou_fire_to_audio_ms, eos_to_audio_ms, within_budget,
            )

        def _emit_content_latency_record(*, first_content_perf: float, cache_hit: bool) -> None:
            """Time-to-first-*content* audio — the first REAL (non-filler)
            sentence. When a filler covered the wait, eos→first_audio (above)
            stays sub-budget but the caller still waits this long to hear the
            actual answer; the filler→content gap is the dead air they
            perceive. Emitted once, separately, so a slow real answer can't
            hide behind a fast filler."""
            nonlocal _content_latency_emitted
            if _content_latency_emitted:
                return
            _content_latency_emitted = True
            filler_preceded = _first_audio_source == "filler"
            gap_ms = (
                int((first_content_perf - _first_audio_perf_val) * 1000)
                if (filler_preceded and _first_audio_perf_val is not None)
                else 0
            )
            eos_to_content_ms = _eos_elapsed_ms(first_content_perf)
            logger.info(
                "NOKVO-LATENCY-CONTENT: language=%s direction=%s tier=%s "
                "cache_hit=%s filler_preceded=%s eos_to_first_content_audio_ms=%d "
                "filler_to_content_gap_ms=%d within_budget=%s",
                language, direction, eou_tier or "-", cache_hit, filler_preceded,
                eos_to_content_ms, gap_ms,
                eos_to_content_ms < settings.VOICE_LATENCY_BUDGET_MS,
            )

        # Decouple LLM stream consumption from TTS roundtrips: the pump
        # owns the Sarvam calls so the LLM token loop never blocks waiting
        # on TTS, and adjacent sentences are batched into a single REST
        # call after the first one has been dispatched.
        # Outbound calls speak slightly slower (more deliberate/human);
        # inbound is unchanged (factor 1.0).
        _pace_factor = (
            settings.VOICE_OUTBOUND_PACE_FACTOR
            if bool((campaign_context or {}).get("campaign_id"))
            else 1.0
        )
        tts_pump = _TtsPump(
            websocket=websocket,
            tenant_res=tenant_res,
            language=language,
            turn_id=turn_id,
            purpose="answer",
            speaking_mark=_mark_speaking,
            pace_factor=_pace_factor,
            style=_campaign_voice_style(outbound_context),
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
        # Use the monotonic counter from the unified session state instead
        # of deriving from the conversation log's length. Pre-unification
        # this was ``len(get_history)``, which produced collisions when the
        # previous turn's ``append_turn`` had not yet flushed by the time
        # the next user utterance arrived — two consecutive turns then
        # shared the same index and the "newer wins on tie" tiebreaker in
        # MemoryFact merge silently became order-dependent. ``next_turn_index``
        # is bumped under the per-call lock by ``append_turn``, so it can't
        # collide.
        try:
            from app.services.session_state_v2 import load_state as _load_state_v2

            _state_for_turn_idx = await _load_state_v2(
                AgentSessionStore.client(),
                AgentSessionStore.namespace(tenant_res),
                call_id,
            )
            turn_index = _state_for_turn_idx.next_turn_index
        except Exception:
            # Fall back to legacy derivation if the unified store hiccups.
            turn_index = len(await AgentSessionStore.get_history(tenant_res, call_id))
        # Business type drives which domain slots the extractor mines and
        # which the prompt block renders. Resolved from the cached runtime
        # bundle so this is a near-free lookup; ``None`` falls back to the
        # broad superset extractor.
        business_type = await _resolve_business_type(db, tenant_res)
        # First turn? Bootstrap from cross-call caller memory if we
        # have a phone. Outbound campaigns carry it on
        # ``campaign_context.contact.phone``; inbound webhooks stash
        # ``from_phone``. Failures are silent — a cold call is the
        # default and always works.
        if turn_index == 0:
            # Persist the caller's number (ANI / campaign contact) into session
            # state so the booking slot engine can auto-fill the phone slot —
            # no spoken-digit capture (which telephony STT garbles). Runs even
            # when memory already has facts, so the phone is always available.
            try:
                _cp = None
                _contact0 = (campaign_context or {}).get("contact")
                if isinstance(_contact0, dict):
                    _cp = _contact0.get("phone") or _contact0.get("phone_e164")
                _cp = _cp or (campaign_context or {}).get("from_phone")
                if _cp and call_id:
                    from app.services.voice_turn_policy import normalize_phone_number as _norm_ph
                    await AgentSessionStore.merge_state(
                        tenant_res, call_id, {"caller_phone": _norm_ph(str(_cp)) or str(_cp)}
                    )
            except Exception:
                logger.debug("NOKVO: persist caller_phone failed", exc_info=True)
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
                        tenant_res,
                        phone=caller_phone,
                        memory=conv_memory,
                        business_type=business_type,
                    )
                except Exception:
                    logger.debug("NOKVO-MEMORY: bootstrap failed", exc_info=True)
            # Single-project auto-fill — when a real-estate org has exactly one
            # active project in the inventory, seed FACT_PROPERTY so the booking
            # FSM never has to ask "which project?" and the LLM can't paraphrase
            # a hardcoded location list from the admin's free-text guidance as
            # if it were the inventory. The strategy / booking layer treats
            # this exactly like a bootstrapped fact.
            if (business_type or "").lower() == "real_estate" and not conv_memory.has("property"):
                try:
                    from app.services.conversational_memory import FACT_PROPERTY, seed_facts
                    from app.services.real_estate_project_service import load_active_projects

                    # NOTE: organization_id_uuid is intentionally unbound (verbatim
                    # from the pre-extraction staticmethod, where this run_session
                    # local was never in scope): the NameError lands in the except
                    # below, so single-project autofill has never fired here.
                    _projects = await load_active_projects(db, organization_id_uuid)
                    if len(_projects) == 1 and (_projects[0].name or "").strip():
                        seed_facts(conv_memory, {FACT_PROPERTY: _projects[0].name.strip()})
                except Exception:
                    logger.debug("NOKVO-MEMORY: single-project autofill failed", exc_info=True)
        if source != "proactive_silence":
            conv_memory.merge_text(
                cleaned,
                turn_index=turn_index,
                language=language,
                role="user",
                business_type=business_type,
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
                # Seed the follow-up preamble inputs on the first turn so the
                # composer can render the "FOLLOW-UP CALL" block. Subsequent
                # turns find it already present in session state.
                if (
                    isinstance(campaign_context, dict)
                    and campaign_context.get("is_followup")
                    and not stored_outbound_memory.get("followup")
                ):
                    stored_outbound_memory["followup"] = {
                        "is_followup": True,
                        "attempt_n": campaign_context.get("attempt_n") or 1,
                        "source_call_id": campaign_context.get("source_call_id"),
                        # Handoff note from the previous call's condenser.
                        # When present, the preamble emits the natural-
                        # language block; when empty, it falls back to the
                        # structured-facts block built from caller memory.
                        "handoff_note": str(
                            campaign_context.get("handoff_note") or ""
                        ).strip(),
                        # The prior promise text — surfaced by the composer in
                        # the preamble. Pulled from the caller's prior memory
                        # via bootstrap; not present here on first turn, so
                        # we leave it blank for the WS-handler-seeded path.
                        "prior_promise": "",
                        # Admin's typed purpose for a manual customer
                        # follow-up (clinic path). Rendered as the REASON
                        # FOR THIS CALL block by the composer.
                        "admin_note": str(
                            campaign_context.get("admin_note") or ""
                        ).strip(),
                    }
                prompt_outbound_memory = update_outbound_memory(
                    stored_outbound_memory,
                    caller_text=cleaned,
                )
            except Exception:
                covered_objectives = []
                stored_outbound_memory = {}
                prompt_outbound_memory = update_outbound_memory({}, caller_text=cleaned)
            # Unify the two memory silos: feed the outbound turn-memory dict into
            # the structured ConversationalMemory so the strategy layer (lead
            # scoring, objection playbook) sees outbound-captured facts too.
            # Low-confidence seed — never overrides an in-call extraction.
            try:
                from app.services.agent_outbound_context import outbound_memory_as_facts
                from app.services.conversational_memory import seed_facts

                seed_facts(conv_memory, outbound_memory_as_facts(prompt_outbound_memory))
            except Exception:
                logger.debug("NOKVO-MEMORY: outbound→conv seed failed", exc_info=True)
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
                # Deterministic leading-filler scrub for outbound turns: the
                # agents are prompt-banned from opening with a stock
                # acknowledgement filler, but the LLM still leaks one. Strip
                # it from the FIRST content sentence so it is never heard,
                # shown, or stored. A bare-ack sentence scrubs to "" → skip
                # it; the next sentence becomes the first and carries the turn.
                #
                # The DETERMINISTIC questionnaire agent must be crisp — open
                # with the question itself — so it gets the FULL filler scrub
                # ("Great,", "Perfect.", "Got it.", "Sure,", "Okay,", …). The
                # free-form salesperson agent keeps the narrow "right so"-only
                # scrub, so a natural "Got it," can still warm the rapport.
                if outbound_context is not None and not answer_parts:
                    if getattr(outbound_context, "has_questionnaire", False):
                        sentence = strip_leading_fillers(sentence)
                    else:
                        sentence = strip_leading_right_so(sentence)
                    if not sentence:
                        return
                _sentence_perf = perf_counter()
                if first_sentence_ms is None:
                    first_sentence_ms = int((_sentence_perf - started) * 1000)
                    _emit_latency_record(
                        source="real",
                        cache_hit=bool(event.get("cache_hit")),
                        first_audio_perf=_sentence_perf,
                    )
                # First REAL content sentence — recorded even when a filler
                # already fired first_audio, so the filler→content gap (the
                # dead air the caller hears) is visible. One-shot internally.
                _emit_content_latency_record(
                    first_content_perf=_sentence_perf,
                    cache_hit=bool(event.get("cache_hit")),
                )
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
        # The spoken latency guard guarantees SOME audio lands within the
        # sub-1s budget when the real LLM answer is slow (an audible hold/
        # bridge counts). It runs on BOTH directions now — inbound speaks a
        # reassuring "one moment" hold, outbound a short thinking-aloud bridge
        # (direction-selected in _latency_guard_text). Still disabled mid-
        # booking (tool_flow active) on either direction: slot-fill turns are
        # tiny LLM calls that finish well under budget, and the filler makes
        # the booking exchange feel robotic ("one moment… Got it, Nihar. What
        # phone number…").
        _tool_flow_active_for_guard = False
        if call_id:
            try:
                _guard_state = await AgentSessionStore.get_state(tenant_res, call_id) or {}
                _tf_for_guard = _guard_state.get("tool_flow") or {}
                _tool_flow_active_for_guard = bool(
                    _tf_for_guard.get("active") and not _tf_for_guard.get("completed")
                )
            except Exception:
                _tool_flow_active_for_guard = False
        # The DETERMINISTIC questionnaire agent must speak ZERO filler — no
        # spoken latency bridge either ("Okay, just a sec…"). Its turns are
        # tiny "ask the next question" LLM calls that finish well under the
        # budget, so a slow turn is rare; on the odd slow one the caller hears
        # a brief silence instead of a hold, which is the intended behaviour
        # for this agent. Inbound + the free-form outbound agent keep the
        # guard (a hold reads naturally there).
        _is_deterministic_questionnaire = bool(
            outbound_context is not None
            and getattr(outbound_context, "has_questionnaire", False)
        )
        latency_guard_enabled = (
            not _tool_flow_active_for_guard and not _is_deterministic_questionnaire
        )

        def _guard_timeout_s() -> float | None:
            """Dynamic guard wait. When end-of-speech is known, size the wait
            so the filler fires early enough that eos→audio stays under the
            budget: budget − (time already spent since eos) − a TTS-dispatch
            margin, clamped to [floor, ceiling]. Without an eos anchor (manual/
            proactive turns) fall back to the fixed ceiling. Returns ``None``
            once the real answer has landed or the guard is disabled, so the
            loop then waits indefinitely on the real stream."""
            if first_sentence_ms is not None or not latency_guard_enabled:
                return None
            ceiling_ms = settings.VOICE_FIRST_SENTENCE_TIMEOUT_MS
            if eou_fired_at is None:
                return max(0.05, ceiling_ms / 1000)
            elapsed_ms = (perf_counter() - eou_fired_at) * 1000
            budget_left_ms = (
                settings.VOICE_LATENCY_BUDGET_MS
                - elapsed_ms
                - settings.VOICE_LATENCY_GUARD_TTS_MARGIN_MS
            )
            timeout_ms = max(
                settings.VOICE_LATENCY_GUARD_FLOOR_MS,
                min(ceiling_ms, budget_left_ms),
            )
            return max(0.02, timeout_ms / 1000)

        try:
            while True:
                timeout_s = _guard_timeout_s()
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=timeout_s)
                except asyncio.TimeoutError:
                    if latency_guard_sent:
                        continue
                    # Grace peek before committing the filler. The real answer
                    # may be just past the budget; firing a filler now queues
                    # its ~1s of playback AHEAD of the real content in the
                    # single TTS pump and delays it. Wait one short window for
                    # a real sentence — if it lands, speak it and skip the
                    # filler (content arrives sooner than filler+content).
                    try:
                        event = await asyncio.wait_for(
                            event_queue.get(),
                            timeout=max(
                                0.0,
                                settings.VOICE_LATENCY_GUARD_CONTENT_GRACE_MS / 1000,
                            ),
                        )
                    except asyncio.TimeoutError:
                        pass  # truly nothing yet — fall through and fire the filler
                    else:
                        if event is stream_done:
                            break
                        if isinstance(event, Exception):
                            raise event
                        await _handle_stream_event(event)
                        continue
                    latency_guard_sent = True
                    _first_audio_perf = perf_counter()
                    first_sentence_ms = int((_first_audio_perf - started) * 1000)
                    guard_tone = "thinking" if direction == "outbound" else "warm"
                    guard_sentence = _latency_guard_text(language, direction)
                    _emit_latency_record(
                        source="filler",
                        cache_hit=False,
                        first_audio_perf=_first_audio_perf,
                    )
                    await websocket.send_json(
                        {
                            "type": "agent_sentence",
                            "turn_id": turn_id,
                            "sentence": guard_sentence,
                            "tone": guard_tone,
                            "first_sentence_ms": first_sentence_ms,
                            "cache_hit": False,
                            "source": "latency_guard",
                        }
                    )
                    await tts_pump.submit(guard_sentence, guard_tone, cacheable_tts=True)
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
                business_type=business_type,
            )
        await save_memory(tenant_res, call_id, conv_memory)
        outbound_state_patch: dict[str, Any] = {}
        if outbound_context is not None and call_id:
            # LLM-path questionnaire tracking: this turn was a clean advance,
            # the directive told the model to ask exactly Q{_intended_q}, and
            # the model spoke — persist it as delivered (paraphrase-proof).
            if _intended_q and (answer or "").strip():
                with contextlib.suppress(Exception):
                    await helpers._persist_question_delivered(
                        tenant_res, call_id, _intended_q
                    )
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
        # Attach the turn outcome to the LangSmith span so a trace shows
        # WHAT the agent decided, not just the raw LLM I/O: the turn
        # index, the spoken reply, the routed intent, and the cumulative
        # captured facts (name/phone/property/date for a booking). Lets
        # "did it capture the right slots?" be answered from the trace
        # alone. Best-effort + None-guarded — never perturbs the turn.
        if _ls_turn_run is not None:
            try:
                _ls_turn_run.add_metadata({"turn_index": turn_index})
                _ls_turn_run.add_outputs({
                    "turn_index": turn_index,
                    "answer": answer,
                    "intent": (final_payload or {}).get("intent"),
                    "refused": bool((final_payload or {}).get("refused")),
                    "captured_facts": conv_memory.snapshot(),
                    # Latency (closes the scan gap): time to first agent audio
                    # and the full turn, so caller→first-audio is exact in the
                    # trace instead of estimated. EOU tier is in the
                    # NOKVO-LATENCY-EOU log line for the same turn.
                    "first_sentence_ms": first_sentence_ms,
                    "total_ms": int((perf_counter() - started) * 1000),
                })
            except Exception:
                logger.debug("NOKVO-LANGSMITH: turn outcome attach failed", exc_info=True)

        if arbiter is not None:
            arbiter.mark_done()
        if after_turn is not None:
            result = after_turn()
            if asyncio.iscoroutine(result):
                await result

        # ── Outro hang-up (deterministic questionnaire campaigns) ──
        # When the agent delivers the campaign's closing line — a failed
        # intent gate ("dealbreaker"), all questions answered, or a
        # disinterest close — play it out, then drop the call. Mirrors the
        # voicemail speak→close pattern. One-shot per call. Gated on an outro
        # being set, so non-deterministic / lead / clinic calls never hang up
        # here. A non-match is harmless (the agent wrapped; the call ends as
        # before) — we never cut a live prospect on a wrong guess.
        _outro = (getattr(outbound_context, "question_outro", "") or "").strip()
        if (
            _outro
            and campaign_context is not None
            and not campaign_context.get("_outro_ended")
            and _answer_is_outro(answer, _outro)
        ):
            campaign_context["_outro_ended"] = True
            logger.info(
                "NOKVO-OUTRO: agent delivered closing line — ending call %s", call_id
            )
            with contextlib.suppress(Exception):
                await asyncio.sleep(_OUTRO_DRAIN_SECONDS)
                await websocket.close()


async def _process_blob_utterance(
    helpers: Any,
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
                        # Conditioning-chain telemetry (when the adapter
                        # carries the enhancer/denoiser): what gain the
                        # AGC settled at and how speech-like RNNoise
                        # judged the most recent frames.
                        "agc_gain": round(float(getattr(getattr(websocket, "_enhancer", None), "gain", 0.0) or 0.0), 3),
                        "speech_prob": round(float(getattr(getattr(websocket, "_denoiser", None), "last_speech_prob", 0.0) or 0.0), 3),
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
                await helpers._dispatch_quality_recovery(
                    websocket, tenant_res, language=recover_lang,
                )
                return
            # Trim leading dead air (keep ~100 ms pre-roll) before STT —
            # the model anchors better when the clip starts at speech.
            try:
                from app.services.agent_robustness import trim_leading_silence

                trimmed = trim_leading_silence(pcm, sample_rate=pcm_sample_rate)
                if len(trimmed) < len(pcm):
                    audio_bytes = _pcm16le_to_wav(trimmed, sample_rate=pcm_sample_rate)
            except Exception:
                pass

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
                timeout=max(0.2, settings.AGENT_TRANSLATE_TIMEOUT_MS / 1000),
            )
            return str(result.get("transcript") or "").strip()
        except asyncio.TimeoutError:
            logger.warning("NOKVO-TRANSLATE: timeout in vad_blob mode; using native only")
            return ""
        except Exception as exc:
            logger.warning(f"NOKVO-TRANSLATE: failed: {exc!r}")
            return ""

    # Meter STT audio seconds for this turn → per-call COGS (best-effort,
    # WAV only). Counted once for the native pass; the optional translate
    # pass (a second Sarvam call when retrieval-translate is on) is not
    # separately metered, so STT cost is a slight under-estimate when that
    # path runs.
    try:
        from app.services.call_usage import current_call_usage

        _usage = current_call_usage()
        if _usage is not None and is_wav:
            _stt_extracted = _extract_pcm_from_wav(audio_bytes)
            if _stt_extracted is not None:
                _stt_pcm, _stt_sr = _stt_extracted
                if _stt_sr:
                    _usage.add_stt_seconds(len(_stt_pcm) / 2 / _stt_sr)
    except Exception:
        pass

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

    # OUTBOUND barge-in immunity (vad_blob backstop): a short backchannel
    # ("uh-huh" / "haan", or a cough that transcribed to noise) must not cut
    # the agent off — leave the in-flight reply running. The streaming path's
    # sustained-speech window is the primary outbound guard; this covers the
    # vad_blob path. Inbound keeps the existing immediate barge-in.
    if (
        verdict == "barge_in"
        and bool((campaign_context or {}).get("campaign_id"))
        and _is_backchannel_utterance(transcript)
    ):
        logger.info("NOKVO-BARGEIN: suppressed:backchannel call=%s", call_id)
        return

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

    # Resolve reply language with the same precedence as the streaming path:
    #   1) explicit switch request ("speak in Telugu")
    #   2) the caller simply STARTED speaking another language — follow it
    #   3) previously-locked session language (sticky)
    #   4) first detection → lock
    requested = detect_language_switch(transcript)
    spoken_switch = None
    if not requested and session_locked_language[0]:
        spoken_switch = detect_spoken_language_switch(
            transcript,
            detected_lang,
            session_locked_language[0],
            confidence=native_result.get("language_probability"),
        )
    if requested or spoken_switch:
        normalized = SarvamVoiceService.normalize_language(requested or spoken_switch)
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
        if requested or spoken_switch:
            robustness.language_state.lock(turn_language)

    await helpers._run_text_turn(
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
