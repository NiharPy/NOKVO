"""Outcome tracking for records the voice agent created.

Every appointment / site visit / callback the agent books has a downstream
truth — did the patient show up? did the buyer keep the site visit? did the
team manage to confirm the reservation? Those truths usually live in the
clinic's spreadsheet or a CRM, never in the voice system, so the agent
never learns from them.

This service is the inbound channel for that signal:

* The team (or an integration) POSTs the outcome for a record.
* :func:`record_outcome` writes it into ``NokvoOneToolRecord.data["outcome"]``
  via :mod:`flow_session.mark_outcome`.
* :func:`recent_outcomes_for_caller` is used at call start to surface past
  outcomes for a returning caller (e.g., "you no-showed last time — would
  you like an SMS reminder this time?").

We don't mutate ``status`` on the record because that's already serving the
booking lifecycle (assigned / cancelled). The outcome layer is orthogonal —
"the booking exists" vs. "the visit happened".
"""

from __future__ import annotations

import uuid
from typing import Any

from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nokvo_one_tool_record import NokvoOneToolRecord
from app.services import flow_session
from app.services.agent_spec import OUTCOME_STATES


# Surfaced from the canonical agent spec so any new outcome state must be
# added there first — keeps the API, dashboards, and policy in lock-step.
VALID_OUTCOMES = OUTCOME_STATES.all_values()


# Mapping of telephony / campaign call dispositions to terminal outcomes.
# Keys are the lowercased status strings the outbound campaign service uses
# ("answered", "no_answer", "failed"); values are the outcome our record
# should be marked with. "answered" alone doesn't promise the booking was
# kept, but it does close the "did the agent reach them" question — we
# bump the record to ``completed`` and let the ops team override later
# with the real downstream outcome if they need to.
DISPOSITION_TO_OUTCOME: dict[str, str] = {
    "answered": OUTCOME_STATES.completed,
    "no_answer": OUTCOME_STATES.failed_followup,
    "failed": OUTCOME_STATES.failed_followup,
    "machine": OUTCOME_STATES.failed_followup,
    "voicemail": OUTCOME_STATES.failed_followup,
    "cancelled": OUTCOME_STATES.cancelled,
    "no_show": OUTCOME_STATES.no_show,
}


class OutcomeTracker:
    @staticmethod
    async def record_outcome(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        record_id: uuid.UUID,
        status: str,
        source: str = "ops_dashboard",
        notes: str | None = None,
    ) -> NokvoOneToolRecord | None:
        if status not in VALID_OUTCOMES:
            raise ValueError(f"Unknown outcome status: {status}")
        res = await db.execute(
            select(NokvoOneToolRecord)
            .where(NokvoOneToolRecord.id == record_id)
            .where(NokvoOneToolRecord.organization_id == organization_id)
        )
        record = res.scalars().first()
        if record is None:
            return None
        merged = dict(record.data or {})
        flow_session.mark_outcome(merged, status, source=source, notes=notes)
        flow_session.append_audit_trail(merged, "outcome_recorded", detail={"status": status, "source": source})
        record.data = merged
        db.add(record)
        await db.commit()
        return record

    @staticmethod
    async def record_from_disposition(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        record_id: uuid.UUID,
        disposition: str,
        source: str = "outbound_call_disposition",
        notes: str | None = None,
    ) -> NokvoOneToolRecord | None:
        """Map a call disposition string to an outcome status and write it.
        Returns ``None`` when the disposition isn't recognised — the caller
        should leave the outcome alone in that case."""
        status = DISPOSITION_TO_OUTCOME.get(disposition.lower().strip())
        if not status:
            return None
        return await OutcomeTracker.record_outcome(
            db,
            organization_id=organization_id,
            record_id=record_id,
            status=status,
            source=source,
            notes=notes,
        )

    @staticmethod
    async def aggregate_for_campaign(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        campaign_id: uuid.UUID | str,
    ) -> dict[str, Any]:
        """Per-campaign outcome counts. Iterates records that carry the
        campaign id in their data dict (set by the voice runtime when the
        record was created during a campaign call)."""
        res = await db.execute(
            select(NokvoOneToolRecord).where(NokvoOneToolRecord.organization_id == organization_id)
        )
        counts: dict[str, int] = {s: 0 for s in OUTCOME_STATES.all_values()}
        total = 0
        cid = str(campaign_id)
        for rec in res.scalars().all():
            data = rec.data or {}
            if str(data.get("campaign_id") or data.get("assignment_source") or "") != cid:
                # Some flows store campaign id under a different key — try
                # the assignment_source as a soft fallback so older records
                # still get counted.
                if data.get("campaign_id") != cid:
                    continue
            outcome = flow_session.outcome_summary(data)
            if not outcome:
                counts[OUTCOME_STATES.pending] += 1
            else:
                status = outcome.get("status")
                if status in counts:
                    counts[status] += 1
            total += 1
        return {
            "campaign_id": cid,
            "total_records": total,
            "by_outcome": counts,
        }

    @staticmethod
    async def auto_followup_if_needed(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        record_id: uuid.UUID,
    ) -> NokvoOneToolRecord | None:
        """When a record's outcome is ``no_show`` or ``failed_followup``,
        create a follow-up callback record so the agent (or a human) tries
        again the next business day. Idempotent — checks for an existing
        follow-up keyed off the source record id."""
        res = await db.execute(
            select(NokvoOneToolRecord)
            .where(NokvoOneToolRecord.id == record_id)
            .where(NokvoOneToolRecord.organization_id == organization_id)
        )
        record = res.scalars().first()
        if record is None:
            return None
        outcome = flow_session.outcome_summary(record.data or {})
        if not outcome or outcome.get("status") not in {
            OUTCOME_STATES.no_show,
            OUTCOME_STATES.failed_followup,
        }:
            return None
        existing = await db.execute(
            select(NokvoOneToolRecord)
            .where(NokvoOneToolRecord.organization_id == organization_id)
            .where(NokvoOneToolRecord.record_type == "callback")
        )
        for cb in existing.scalars().all():
            if str((cb.data or {}).get("source_record_id")) == str(record_id):
                return cb  # already created
        followup_at = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
        source_data = record.data or {}
        callback = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=organization_id,
            record_type="callback",
            status="scheduled",
            data={
                "source_record_id": str(record_id),
                "source_record_type": record.record_type,
                "callback_at": followup_at,
                "notes": (
                    "Auto-created follow-up: previous record outcome was "
                    f"{outcome.get('status')}. Original reason: "
                    f"{source_data.get('reason') or source_data.get('summary') or 'n/a'}."
                ),
                "contact_name": (
                    source_data.get("patient_name")
                    or source_data.get("name")
                    or source_data.get("customer_name")
                ),
                "contact_phone": record.contact_phone or source_data.get("phone"),
                "auto_followup": True,
            },
            contact_phone=record.contact_phone,
        )
        flow_session.append_audit_trail(
            callback.data,
            "auto_followup_created",
            detail={"source_outcome": outcome.get("status")},
        )
        db.add(callback)
        await db.commit()
        return callback

    @staticmethod
    async def recent_outcomes_for_caller(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        phone: str,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        """Pull the last few outcomes for a caller. Used at session start so
        the agent can adapt — e.g., a no_show history triggers a reminder
        SMS offer, a confirmed history triggers a warmer greeting."""
        digits = "".join(c for c in str(phone or "") if c.isdigit())
        if len(digits) < 10:
            return []
        suffix = digits[-10:]
        res = await db.execute(
            select(NokvoOneToolRecord)
            .where(NokvoOneToolRecord.organization_id == organization_id)
            .order_by(NokvoOneToolRecord.created_at.desc())
            .limit(40)
        )
        out: list[dict[str, Any]] = []
        for rec in res.scalars().all():
            phone_value = "".join(c for c in str(rec.contact_phone or "") if c.isdigit())
            if not phone_value:
                data = rec.data or {}
                phone_value = "".join(
                    c for c in str(data.get("phone") or data.get("contact_phone") or "") if c.isdigit()
                )
            if not phone_value or phone_value[-10:] != suffix:
                continue
            outcome = flow_session.outcome_summary(rec.data or {})
            if not outcome:
                continue
            out.append(
                {
                    "record_id": str(rec.id),
                    "record_type": rec.record_type,
                    "outcome": outcome,
                    "created_at": rec.created_at.isoformat() if rec.created_at else None,
                }
            )
            if len(out) >= limit:
                break
        return out
