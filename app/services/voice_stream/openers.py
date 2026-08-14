"""Call-opening flow: inbound greeting, returning-caller awareness, outbound
opener facts, and the deterministic prosody-aware opener playback.

Extracted from nokvo_one_voice_stream_service.py (turn_router helpers
pattern: functions taking ``helpers`` receive ``NokvoOneVoiceStreamService``
and call sibling statics through it, so class-attribute monkeypatches keep
working). The service class keeps delegating wrappers - no API change.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from time import perf_counter
from typing import Any
import uuid

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_resources import TenantResources
from app.services.agent_robustness import TurnArbiter
from app.services.agent_session_store import AgentSessionStore
from app.services.prosody import DEFAULT_TONE, ProsodyChunk, prosody_for, stream_prosody_chunks, style_prosody
from app.services.sarvam_voice_service import SarvamVoiceService

logger = logging.getLogger(__name__)

# Never hold the speaking flag longer than this, however the playout clock reads.
# A wedged clock must not leave the arbiter believing the agent is mid-sentence
# for the rest of the call — that would classify every later utterance as a
# barge-in. Comfortably longer than any opener.
_MAX_PLAYOUT_WAIT_S = 30.0


async def _await_playout(websocket: WebSocket) -> None:
    """Block until audio already handed to the telephony adapter has finished
    playing. No-op on transports without a playback queue (the web test call's
    raw WebSocket), which is why it is reached duck-typed.

    Polls rather than sleeping once: the remaining time grows as later sentences
    are queued, and a ``clearAudio`` on barge-in resets the clock to now, so this
    returns promptly when the caller interrupts."""
    remaining = getattr(websocket, "playout_remaining_s", None)
    if remaining is None:
        return
    deadline = asyncio.get_running_loop().time() + _MAX_PLAYOUT_WAIT_S
    with contextlib.suppress(Exception):
        while asyncio.get_running_loop().time() < deadline:
            left = float(remaining())
            if left <= 0:
                return
            await asyncio.sleep(min(left, 0.2))


async def _resolve_business_type(
    db: AsyncSession | None,
    tenant_res: TenantResources,
) -> str | None:
    """Return the org's business type (``organization.industry``) via the
    cached runtime bundle. Best-effort — a failure returns ``None`` so the
    memory layer falls back to its broad superset extractor."""
    try:
        from app.services.agent_runtime_bundle import get_bundle

        bundle = await get_bundle(db, tenant_res)
        return (bundle.organization_industry or "").strip().lower() or None
    except Exception:
        logger.debug("NOKVO-MEMORY: business_type resolve failed", exc_info=True)
        return None


def _inbound_opening_text(language: str | None) -> str:
    lang = SarvamVoiceService.normalize_language(language)
    if lang == "te":
        return "హలో, మీరు మీకు సౌకర్యమైన భాషలో మాట్లాడండి. నేను అదే భాషలో కొనసాగిస్తాను. నేను ఎలా సహాయం చేయగలను?"
    if lang == "hi":
        return "नमस्ते, आप जिस भाषा में सहज हैं उसमें बात कीजिए। मैं उसी भाषा में आगे बात करूंगा। मैं कैसे मदद कर सकता हूं?"
    return "Hello, please speak in the language you're comfortable with. I'll continue in that language. How can I help?"


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


async def _outbound_opener_known_facts(
    db: AsyncSession | None,
    tenant_res: TenantResources,
    campaign_context: dict[str, Any] | None,
) -> dict[str, Any]:
    """Assemble what we already know about THIS lead for a personalised
    opener: name + enquiry / prior-call facts (bhk, location, budget) and a
    ``returning`` flag. Two sources, best-effort: the dialer-provided
    ``contact`` (the lead's own enquiry) and the cross-call caller-memory
    blob (a prior call). An empty dict yields the cold template opener."""
    facts: dict[str, Any] = {}
    if isinstance(campaign_context, dict) and campaign_context.get("is_followup"):
        # Flag the follow-up + the project/campaign name so the opener can
        # re-engage grounded ("following up about <project>"). Only mark
        # ``returning`` (the "follow up on our LAST CONVERSATION" wording)
        # when a real prior call actually happened — i.e. a handoff_note
        # exists. Otherwise that line is a lie.
        facts["followup"] = True
        project = str(campaign_context.get("goal") or "").strip()
        if project and project.lower() != "follow-up call":
            facts["project"] = project
        if str(campaign_context.get("handoff_note") or "").strip():
            facts["returning"] = True
    contact = (campaign_context or {}).get("contact") if isinstance(campaign_context, dict) else None
    contact = contact if isinstance(contact, dict) else {}

    for key in ("name", "full_name", "customer_name"):
        if contact.get(key):
            facts["name"] = str(contact[key]).strip()
            break
    for src, dst in (
        ("bhk", "bhk"),
        ("property_type", "bhk"),
        ("location", "location"),
        ("location_preference", "location"),
        ("budget", "budget"),
    ):
        if contact.get(src) and not facts.get(dst):
            facts[dst] = str(contact[src]).strip()

    phone = (
        contact.get("phone")
        or contact.get("phone_e164")
        or (campaign_context or {}).get("from_phone")
        or (campaign_context or {}).get("to_phone")
    )
    if phone:
        try:
            from app.services.conversational_memory import (
                FACT_BHK,
                FACT_BUDGET,
                FACT_LOCATION,
                FACT_NAME,
                ConversationalMemory,
                bootstrap_caller_memory,
            )

            business_type = await _resolve_business_type(db, tenant_res)
            prior = ConversationalMemory()
            await bootstrap_caller_memory(
                tenant_res, phone=phone, memory=prior, business_type=business_type
            )
            if prior.facts or prior.prior_stage:
                facts["returning"] = True
            for fkey, dst in (
                (FACT_NAME, "name"),
                (FACT_BHK, "bhk"),
                (FACT_LOCATION, "location"),
                (FACT_BUDGET, "budget"),
            ):
                value = prior.get(fkey)
                if value and not facts.get(dst):
                    facts[dst] = str(value).strip()
            # Carry the prior-call buying-journey context too: the stage
            # they reached and (if any) the last objection wording — so
            # the opener can address it by name on a returning call
            # instead of starting cold over the same concern.
            if prior.prior_stage:
                facts["prior_stage"] = prior.prior_stage
            for entry in prior.objections:
                if entry.get("from_prior_call") and entry.get("text"):
                    facts["last_objection_code"] = str(entry.get("code") or "")
                    facts["last_objection_text"] = str(entry.get("text") or "")[:120]
                    break
        except Exception:
            logger.debug("NOKVO-MEMORY: opener known-facts bootstrap failed", exc_info=True)
    return facts


async def _play_opener(
    websocket: WebSocket,
    tenant_res: TenantResources,
    opening_text: str,
    *,
    language: str,
    call_id: str | None = None,
    campaign_context: dict[str, Any] | None = None,
    style: str = "",
    arbiter: TurnArbiter | None = None,
    turn_state: dict[str, Any] | None = None,
) -> None:
    """Deterministic prosody-aware opener — no LLM round-trip.

    Saves ~150ms of perceived latency on outbound campaign calls vs.
    sending the opener through ``_run_text_turn``. The opening text may
    contain prosody tags (``[warm]…[/warm] [neutral]…[/neutral]``); if
    none are present, the whole opener is voiced as ``[warm]``.

    ``arbiter``/``turn_state`` put the opener inside the turn-taking state
    machine, exactly like every other utterance the agent produces
    (:func:`_deliver_verbatim_question`, ``_speak_outro_and_end``). This used to
    be the ONE speech path that skipped it, which mattered most precisely where
    it hurt: the opener is the first six-to-eight seconds of an outbound call, so
    the arbiter read IDLE while the agent was greeting a stranger. A "Hello?" over
    the intro was classified as a fresh turn rather than a barge-in, nothing
    flushed the queued audio, and the agent's reply stacked up behind an opener
    still playing. Callers heard it talk over them and then answer a question
    from ten seconds ago.

    The speaking flag is held for the audio's REAL duration (the adapter's playout
    clock), not until the last byte is handed to the telephony socket — those
    differ by the whole length of the greeting.
    """
    from app.services.apex_micro_acks import tts_variant_for_call

    text = (opening_text or "").strip()
    if not text:
        return
    if "[" not in text or "]" not in text:
        text = f"[warm]{text}[/warm]"
    turn_id = str(uuid.uuid4())[:8]
    started = perf_counter()
    _variant = tts_variant_for_call(call_id)
    # THINKING → SPEAKING mirrors a normal turn. mark_speaking only advances from
    # THINKING, so the turn must be begun before the first audio goes out.
    if arbiter is not None and not arbiter.is_active:
        arbiter.begin(turn_id=f"opener-{turn_id}")
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
            # First audio is about to go out — from here a caller utterance is a
            # genuine interruption, not a new turn.
            if turn_state is not None:
                turn_state["speaking"] = True
            if arbiter is not None:
                arbiter.mark_speaking()
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
        prosody = prosody_for(chunk.tone, style)
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
                cache=True,  # verbatim, zero-LLM opener — cacheable across calls
                variant=_variant,
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

    # Every byte is on the socket, but the callee is still LISTENING. Hold the
    # speaking flag for the audio's real remaining duration so a barge-in during
    # the greeting is classified as one; a caller who interrupts cancels this
    # task, which is what makes the wait interruptible rather than a dead sleep.
    await _await_playout(websocket)

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
    if turn_state is not None:
        turn_state["speaking"] = False
    if arbiter is not None:
        arbiter.mark_done()
