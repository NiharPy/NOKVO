"""LeadFollowupNoteScheduler — post-call layer that reads a lead's call note,
extracts any callback time the prospect asked for, and configures the
follow-up agent's callback accordingly (carrying the note).

The RE_agent_scheduler analog for the lead → follow-up-call path. The
condenser writes the prose call note at end of call; this layer runs AFTER
that, off the call's latency path:

  1. an LLM reads the note and extracts {callback_requested, callback_date,
     callback_time, callback_reason},
  2. when a concrete time is present, ``FollowupSchedulerService`` upserts a
     SINGLE pending follow-up (``reason=promise_extracted``) at that time —
     clamped to the campaign call window, note attached — overriding any
     weaker disposition / in-call-regex row.

Why a post-call LLM pass when the in-call regex already exists: the regex
(``ConversationalMemory._extract_promised_callback_at``) is heuristic,
English-centric, and per-utterance. A human reading the holistic summary
("customer asked us to call back next Tuesday afternoon", possibly said in
Telugu/Hindi) catches callbacks the regex misses. This is the safety net.

Best-effort + idempotent: never raises out, refines (not duplicates) an
existing pending follow-up, and a vague/empty note simply leaves scheduling
to the existing disposition rules. Targets outbound leads (``lead_id``) and
inbound callers (``customer_id`` via their CustomerBase row).
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import datetime, time as _time, timezone
from typing import Any

logger = logging.getLogger(__name__)

_JSON_OBJ = re.compile(r"\{[\s\S]*\}")

_EXTRACT_SYSTEM = (
    "You read a short real-estate call note and decide whether the PROSPECT "
    "asked to be called back, and when. Output STRICT JSON with exactly these "
    "keys: callback_requested (true/false), callback_date (YYYY-MM-DD or null), "
    "callback_time (24-hour HH:MM or null), callback_reason (one short phrase, "
    "or null). Set callback_requested=true ONLY when the prospect (not the "
    "agent) wants a follow-up call at a later time — e.g. 'call me tomorrow at "
    "4', 'reach out next week', 'I'm busy, call this evening'. The agent simply "
    "promising 'someone will follow up' is NOT a request. Resolve relative "
    "dates ('tomorrow', 'next Tuesday', 'this weekend') to a concrete "
    "YYYY-MM-DD using the Current date provided. Use null for anything the note "
    "does not state — never invent a date or time. Output ONLY the JSON object."
)


class LeadFollowupNoteScheduler:
    """Configure a lead/customer follow-up callback from its post-call note."""

    @staticmethod
    async def schedule_from_note(
        db,
        *,
        tenant_res,
        note: str,
        source_call_id: str | None,
        lead_id: Any = None,
        customer_id: Any = None,
        customer_phone: str | None = None,
        campaign: Any = None,
    ) -> dict[str, Any]:
        """Read the note → if the prospect asked for a callback at a concrete
        time, upsert their single pending follow-up at that time with the note.

        Returns a small status dict. Best-effort; never raises out.
        """
        note = (note or "").strip()
        if not note:
            return {"ok": False, "reason": "no_note"}

        # Resolve the target. Outbound carries lead_id; inbound carries the
        # caller's phone → resolve to their CustomerBase row id.
        lead_uuid = _coerce_uuid(lead_id)
        customer_uuid = _coerce_uuid(customer_id)
        tenant_id = getattr(tenant_res, "tenant_id", None)
        if lead_uuid is None and customer_uuid is None and customer_phone and tenant_id:
            customer_uuid = await LeadFollowupNoteScheduler._resolve_customer_id(
                db, tenant_id=str(tenant_id), phone=customer_phone
            )
        if lead_uuid is None and customer_uuid is None:
            return {"ok": False, "reason": "no_target"}

        # Cheap clinic gate BEFORE the LLM call (clinics are admin-only; the
        # service layer enforces this too, but skip the spend here).
        try:
            from app.services.followup_scheduler_service import FollowupSchedulerService

            if tenant_id and await FollowupSchedulerService._tenant_is_clinic(
                tenant_id=str(tenant_id), db=db
            ):
                return {"ok": False, "reason": "clinic_admin_only"}
        except Exception:
            logger.debug("NOKVO-LEAD-FOLLOWUP: clinic gate check failed", exc_info=True)

        extracted = await LeadFollowupNoteScheduler._extract(note)
        if not extracted.get("callback_requested"):
            return {"ok": False, "reason": "no_callback_in_note"}

        promised_at = LeadFollowupNoteScheduler._parse_callback_datetime(
            extracted.get("callback_date"), extracted.get("callback_time")
        )
        if promised_at is None:
            return {"ok": False, "reason": "no_parseable_time"}

        reason_note = str(extracted.get("callback_reason") or "").strip()
        if not reason_note:
            reason_note = note.splitlines()[0].strip()[:200] if note else None

        try:
            from app.services.followup_scheduler_service import FollowupSchedulerService

            row = await FollowupSchedulerService.upsert_promise_from_note(
                db=db,
                promised_at=promised_at,
                note=reason_note,
                source_call_id=source_call_id,
                lead_id=lead_uuid,
                customer_id=customer_uuid,
                campaign=campaign,
            )
        except Exception:
            logger.exception("NOKVO-LEAD-FOLLOWUP: upsert failed")
            return {"ok": False, "reason": "upsert_failed"}

        if row is None:
            return {"ok": False, "reason": "gated"}
        logger.info(
            "NOKVO-LEAD-FOLLOWUP: callback scheduled row=%s target=%s at=%s",
            row.id,
            f"lead:{lead_uuid}" if lead_uuid else f"customer:{customer_uuid}",
            getattr(row, "scheduled_at", None),
        )
        return {
            "ok": True,
            "row_id": str(row.id),
            "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        }

    # ── helpers ──────────────────────────────────────────────────────────────

    @staticmethod
    async def _extract(note: str) -> dict[str, Any]:
        """LLM-extract the callback intent + time from the note. Safe-default {}."""
        try:
            from zoneinfo import ZoneInfo

            today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
        except Exception:
            today = datetime.now(timezone.utc).date().isoformat()
        try:
            from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

            # Auxiliary extraction (not the agent's spoken reply) → gpt-4.1-nano
            # pool; falls back to the mini pool when no nano is configured.
            raw = await AzureGroundedLLM.complete_nano(
                [
                    {"role": "system", "content": _EXTRACT_SYSTEM},
                    {"role": "user", "content": f"Current date: {today}\n\nCall note:\n{note}"},
                ],
                max_tokens=160,
            )
        except Exception:
            logger.debug("NOKVO-LEAD-FOLLOWUP: extraction LLM call failed", exc_info=True)
            return {}
        match = _JSON_OBJ.search((raw or "").strip())
        if not match:
            return {}
        try:
            obj = json.loads(match.group(0))
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}

    @staticmethod
    def _parse_callback_datetime(date_val: Any, time_val: Any) -> datetime | None:
        """Combine extracted date (+ optional time) into a concrete UTC datetime,
        reusing the pipeline's lenient parsers. A bare date with no time defaults
        to 10:00 IST (the service then clamps into the call window). None when no
        date is present."""
        if not date_val:
            return None
        try:
            from app.services.nokvo_one_voice_pipeline import (
                _APPOINTMENT_LOCAL_TZ,
                NokvoOneVoicePipeline as _P,
            )

            d = _P._parse_appointment_date(date_val)
            t = _P._parse_appointment_time(time_val) if time_val else _time(10, 0)
            return datetime.combine(d, t, tzinfo=_APPOINTMENT_LOCAL_TZ).astimezone(timezone.utc)
        except Exception:
            return None

    @staticmethod
    async def _resolve_customer_id(db, *, tenant_id: str, phone: str) -> uuid.UUID | None:
        """Look up the caller's CustomerBase row id by (tenant, normalised phone)."""
        try:
            from sqlalchemy import select

            from app.models.customer_base import CustomerBase
            from app.services.voice_turn_policy import normalize_phone_number

            phone_e164 = normalize_phone_number(str(phone or "")) or str(phone or "").strip()
            if not phone_e164:
                return None
            res = await db.execute(
                select(CustomerBase.id)
                .where(CustomerBase.tenant_id == tenant_id)
                .where(CustomerBase.phone_e164 == phone_e164)
                .limit(1)
            )
            return res.scalars().first()
        except Exception:
            logger.debug("NOKVO-LEAD-FOLLOWUP: customer resolve failed", exc_info=True)
            return None


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None
