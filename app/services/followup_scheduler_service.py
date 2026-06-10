"""FollowupSchedulerService — single owner of the follow-up state machine.

The decision tree (per call end):

  1. Memory extracted ``promised_callback_at``?  → schedule at that exact time.
     (Highest priority. Overrides everything below.)

  2. Disposition rule from campaign.agent_config.followup_rules:
       no_answer  → +rule.no_answer.delay_minutes
       voicemail  → +rule.voicemail.delay_minutes
       failed     → +rule.failed.delay_minutes
       partial    → +rule.partial.delay_minutes
     answered+interested+booking_created → no follow-up (conversion kill switch).

  3. Clamp scheduled_at into the campaign's allowed call window (TRAI: 09:00–
     19:00 Asia/Kolkata default).

  4. Caps:
       consent_status == revoked        → drop (opt-out kill switch)
       attempts >= rule.max_attempts    → mark exhausted, stop
       lead has a paused pending row    → drop (human override)

  5. Insert into lead_followup_schedules.

The scheduler loop (``followup_scheduler.py``) drains due rows and places the
call. Every cap is re-checked at dispatch in case state changed between
enqueue and now.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lead_followup_schedule import (
    FollowupReason,
    FollowupStatus,
    LeadFollowupSchedule,
)
from app.models.outbound_campaign import OutboundCampaign
from app.models.outgoing_lead import LeadConsentStatus, OutgoingLead

logger = logging.getLogger(__name__)


# Defaults applied when campaign.agent_config.followup_rules is missing or
# partially specified. Times are TRAI-aligned: 9 AM – 7 PM IST. Delays are
# tuned for a typical real-estate / clinic call flow — no-answer in 2h
# (caller is likely still in the same context), voicemail next day (caller
# was probably busy), failed in 4h (could be network), partial in 3 days
# (give the lead time to think).
DEFAULT_FOLLOWUP_RULES: dict = {
    "enabled": True,
    "max_attempts_per_lead": 3,
    "rules": {
        "no_answer": {"delay_minutes": 120, "max_attempts": 3},
        "voicemail": {"delay_minutes": 1440, "max_attempts": 2},
        "failed": {"delay_minutes": 240, "max_attempts": 2},
        "partial": {"delay_minutes": 4320, "max_attempts": 1},
    },
    "call_window": {
        "start_local": "09:00",
        "end_local": "19:00",
        "timezone": "Asia/Kolkata",
    },
}

# Map (disposition, outcome) → (reason enum, rule key). The disposition comes
# from Exotel's status webhook; outcome (if available) comes from the
# OutboundCallOutcomeClassifier for answered calls.
_DISPOSITION_TO_REASON: dict[str, tuple[FollowupReason, str]] = {
    "no_answer": (FollowupReason.no_answer_retry, "no_answer"),
    "voicemail": (FollowupReason.voicemail_retry, "voicemail"),
    "failed": (FollowupReason.failed_retry, "failed"),
}


@dataclass
class FollowupCue:
    """Memory-extracted cues that influence the scheduling decision."""

    promised_callback_at: Optional[datetime] = None
    opted_out: bool = False
    converted: bool = False  # booking/lead row was created on this call


def effective_followup_rules(campaign: OutboundCampaign | None) -> dict:
    """Merge admin overrides on top of DEFAULT_FOLLOWUP_RULES.

    Pure function — no DB access. Safe to call inline during enqueue.
    """
    if campaign is None:
        return DEFAULT_FOLLOWUP_RULES
    admin = (campaign.agent_config or {}).get("followup_rules") or {}
    merged: dict = {**DEFAULT_FOLLOWUP_RULES, **admin}
    # Deep-merge the two nested dicts so admin-overriding just no_answer
    # doesn't wipe voicemail/failed/partial.
    merged["rules"] = {
        **DEFAULT_FOLLOWUP_RULES["rules"],
        **(admin.get("rules") or {}),
    }
    merged["call_window"] = {
        **DEFAULT_FOLLOWUP_RULES["call_window"],
        **(admin.get("call_window") or {}),
    }
    return merged


def clamp_to_call_window(when: datetime, rules: dict) -> datetime:
    """If ``when`` falls outside the configured local call window, advance to
    the next valid window start. Pure, unit-testable.

    The rules block carries ``call_window.start_local`` ("HH:MM"),
    ``end_local`` ("HH:MM"), and ``timezone`` ("Asia/Kolkata"). The clamp is
    computed in the local TZ then converted back to UTC.

    Examples (IST window 09:00–19:00):
      19:30 IST → next-day 09:00 IST
      03:00 IST → same-day 09:00 IST
      12:00 IST → unchanged
    """
    try:
        from zoneinfo import ZoneInfo  # py3.9+
    except ImportError:  # pragma: no cover
        return when

    window = (rules or {}).get("call_window") or DEFAULT_FOLLOWUP_RULES["call_window"]
    tz_name = window.get("timezone", "Asia/Kolkata")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("Asia/Kolkata")

    start_h, start_m = _parse_hhmm(window.get("start_local", "09:00"))
    end_h, end_m = _parse_hhmm(window.get("end_local", "19:00"))
    start_t = time(start_h, start_m)
    end_t = time(end_h, end_m)

    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(tz)

    if start_t <= local.time() < end_t:
        return when  # already in window

    # Build target = today's start_local in local TZ.
    target = local.replace(hour=start_h, minute=start_m, second=0, microsecond=0)
    if local.time() >= end_t:
        # After window — push to next day.
        target = target + timedelta(days=1)
    # If local.time() < start_t, target already correct (same day).
    return target.astimezone(timezone.utc)


def _parse_hhmm(value: str) -> tuple[int, int]:
    try:
        h_str, m_str = value.split(":", 1)
        return max(0, min(23, int(h_str))), max(0, min(59, int(m_str)))
    except (ValueError, AttributeError):
        return 9, 0


class FollowupSchedulerService:
    """Stateless gateway to the lead_followup_schedules table."""

    # ---------------------------------------------------------------- enqueue

    @staticmethod
    async def enqueue_after_call(
        *,
        lead: OutgoingLead,
        campaign: OutboundCampaign | None,
        source_call_id: str | None,
        disposition: str,
        outcome: str | None = None,
        cue: FollowupCue | None = None,
        db: AsyncSession,
    ) -> Optional[LeadFollowupSchedule]:
        """Implements the decision tree at the top of this module.

        Returns the inserted row, or None if no follow-up was scheduled
        (conversion, opt-out, exhausted, rule says skip, lead paused).
        """
        cue = cue or FollowupCue()
        rules = effective_followup_rules(campaign)

        # Kill switch #1: feature toggle.
        if not rules.get("enabled", True):
            return None

        # Kill switch #2: opt-out. If the prior call extracted an opt-out cue,
        # the lead's consent_status was already flipped to 'revoked' by the
        # call-end hook. Honour it here too in case the flip raced.
        if cue.opted_out or lead.consent_status == LeadConsentStatus.revoked:
            await FollowupSchedulerService.cancel_for_lead(
                lead_id=lead.id, reason="opted_out", db=db
            )
            return None

        # Kill switch #3: conversion.
        if cue.converted:
            await FollowupSchedulerService.cancel_for_lead(
                lead_id=lead.id, reason="converted", db=db
            )
            return None

        # Compute scheduled_at.
        scheduled_at, reason = FollowupSchedulerService._decide_scheduled_at(
            cue=cue, disposition=disposition, outcome=outcome, rules=rules
        )
        if scheduled_at is None:
            # No rule matched (e.g. answered+not_interested with no promise).
            return None

        scheduled_at = clamp_to_call_window(scheduled_at, rules)

        # Kill switch #4: max attempts per lead.
        attempts_so_far = await FollowupSchedulerService._attempts_for_lead(
            lead_id=lead.id, campaign_id=campaign.id if campaign else None, db=db
        )
        per_rule_cap = FollowupSchedulerService._rule_max_attempts(reason, rules)
        global_cap = int(rules.get("max_attempts_per_lead", 3))
        cap = min(per_rule_cap, global_cap)
        if attempts_so_far >= cap:
            # Mark any lingering pending row as exhausted so the chip reads
            # right immediately.
            await FollowupSchedulerService._exhaust_pending_for_lead(
                lead_id=lead.id, db=db
            )
            return None

        # Kill switch #5: lead is paused — admin took over.
        if await FollowupSchedulerService._lead_is_paused(lead_id=lead.id, db=db):
            return None

        row = LeadFollowupSchedule(
            id=uuid.uuid4(),
            tenant_id=lead.tenant_id,
            lead_id=lead.id,
            campaign_id=campaign.id if campaign else None,
            source_call_id=source_call_id,
            scheduled_at=scheduled_at,
            reason=reason,
            attempts=attempts_so_far,
            status=FollowupStatus.pending,
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    @staticmethod
    def _decide_scheduled_at(
        *,
        cue: FollowupCue,
        disposition: str,
        outcome: str | None,
        rules: dict,
    ) -> tuple[Optional[datetime], FollowupReason]:
        """Promise-first, disposition-rule-fallback."""
        now = datetime.now(timezone.utc)
        if cue.promised_callback_at is not None:
            promised = cue.promised_callback_at
            if promised.tzinfo is None:
                promised = promised.replace(tzinfo=timezone.utc)
            # If the promise is in the past (caller said "right now" and the
            # webhook hits ~10 min later), don't drop the follow-up — push it
            # 5 min into the future. Then call_window clamp may push further.
            if promised < now:
                promised = now + timedelta(minutes=5)
            return promised, FollowupReason.promise_extracted

        # No promise → consult disposition rule.
        if disposition in _DISPOSITION_TO_REASON:
            reason, rule_key = _DISPOSITION_TO_REASON[disposition]
            rule = (rules.get("rules") or {}).get(rule_key) or {}
            delay = int(rule.get("delay_minutes", 0))
            if delay <= 0:
                return None, reason
            return now + timedelta(minutes=delay), reason

        # Answered + partial outcome → admin-configurable retry.
        if disposition == "answered" and outcome == "partial":
            rule = (rules.get("rules") or {}).get("partial") or {}
            delay = int(rule.get("delay_minutes", 0))
            if delay <= 0:
                return None, FollowupReason.partial_retry
            return now + timedelta(minutes=delay), FollowupReason.partial_retry

        # Answered + interested → conversion handled by kill switch above.
        # Answered + not_interested → no retry (caller explicitly declined).
        return None, FollowupReason.manual

    @staticmethod
    def _rule_max_attempts(reason: FollowupReason, rules: dict) -> int:
        """Per-disposition max from the rules block (falls back to global)."""
        key_map = {
            FollowupReason.no_answer_retry: "no_answer",
            FollowupReason.voicemail_retry: "voicemail",
            FollowupReason.failed_retry: "failed",
            FollowupReason.partial_retry: "partial",
            FollowupReason.promise_extracted: None,  # no per-rule cap
            FollowupReason.manual: None,
        }
        key = key_map.get(reason)
        if key is None:
            return int(rules.get("max_attempts_per_lead", 3))
        rule = (rules.get("rules") or {}).get(key) or {}
        return int(rule.get("max_attempts", rules.get("max_attempts_per_lead", 3)))

    # ---------------------------------------------------------------- queries

    @staticmethod
    async def _attempts_for_lead(
        *,
        lead_id: uuid.UUID,
        campaign_id: uuid.UUID | None,
        db: AsyncSession,
    ) -> int:
        """How many follow-ups already exist for this lead+campaign pair
        (counts completed + in_flight + pending, excludes cancelled).

        Campaign-scoped: a lead in two campaigns has two independent counters.
        """
        from sqlalchemy import func as sa_func

        stmt = (
            select(sa_func.count(LeadFollowupSchedule.id))
            .where(LeadFollowupSchedule.lead_id == lead_id)
            .where(
                LeadFollowupSchedule.status.in_(
                    [
                        FollowupStatus.pending,
                        FollowupStatus.in_flight,
                        FollowupStatus.completed,
                        FollowupStatus.exhausted,
                    ]
                )
            )
        )
        if campaign_id is None:
            stmt = stmt.where(LeadFollowupSchedule.campaign_id.is_(None))
        else:
            stmt = stmt.where(LeadFollowupSchedule.campaign_id == campaign_id)
        res = await db.execute(stmt)
        return int(res.scalar() or 0)

    @staticmethod
    async def _lead_is_paused(*, lead_id: uuid.UUID, db: AsyncSession) -> bool:
        stmt = (
            select(LeadFollowupSchedule.id)
            .where(LeadFollowupSchedule.lead_id == lead_id)
            .where(LeadFollowupSchedule.status == FollowupStatus.paused)
            .limit(1)
        )
        res = await db.execute(stmt)
        return res.scalar() is not None

    @staticmethod
    async def next_due(
        *, limit: int = 25, db: AsyncSession, now: datetime | None = None
    ) -> list[LeadFollowupSchedule]:
        """Drain query for the scheduler loop. FOR UPDATE SKIP LOCKED so two
        scheduler instances can't both pick the same row.

        Caller is expected to commit (or rollback) after processing — the
        skip-locked lock releases on transaction end.
        """
        now = now or datetime.now(timezone.utc)
        stmt = (
            select(LeadFollowupSchedule)
            .where(LeadFollowupSchedule.status == FollowupStatus.pending)
            .where(LeadFollowupSchedule.scheduled_at <= now)
            .order_by(LeadFollowupSchedule.scheduled_at.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        res = await db.execute(stmt)
        return list(res.scalars().all())

    # ---------------------------------------------------------------- mutate

    @staticmethod
    async def mark_in_flight(
        *,
        row: LeadFollowupSchedule,
        placed_call_id: str,
        db: AsyncSession,
    ) -> None:
        row.status = FollowupStatus.in_flight
        row.placed_call_id = placed_call_id
        row.attempts = int(row.attempts or 0) + 1
        db.add(row)
        await db.commit()

    @staticmethod
    async def mark_completed(
        *, row: LeadFollowupSchedule, db: AsyncSession
    ) -> None:
        row.status = FollowupStatus.completed
        db.add(row)
        await db.commit()

    @staticmethod
    async def mark_failed(
        *, row: LeadFollowupSchedule, reason: str, db: AsyncSession
    ) -> None:
        row.status = FollowupStatus.cancelled
        row.cancelled_reason = reason
        db.add(row)
        await db.commit()

    @staticmethod
    async def _exhaust_pending_for_lead(
        *, lead_id: uuid.UUID, db: AsyncSession
    ) -> None:
        await db.execute(
            update(LeadFollowupSchedule)
            .where(LeadFollowupSchedule.lead_id == lead_id)
            .where(LeadFollowupSchedule.status == FollowupStatus.pending)
            .values(status=FollowupStatus.exhausted, cancelled_reason="max_attempts")
        )
        await db.commit()

    @staticmethod
    async def cancel_for_lead(
        *, lead_id: uuid.UUID, reason: str, db: AsyncSession
    ) -> int:
        """Cancel every pending or paused row for this lead.

        Returns the count of cancelled rows. Used by the opt-out and
        conversion hooks, and by admin "cancel all" actions.
        """
        result = await db.execute(
            update(LeadFollowupSchedule)
            .where(LeadFollowupSchedule.lead_id == lead_id)
            .where(
                LeadFollowupSchedule.status.in_(
                    [FollowupStatus.pending, FollowupStatus.paused]
                )
            )
            .values(status=FollowupStatus.cancelled, cancelled_reason=reason)
        )
        await db.commit()
        return result.rowcount or 0

    @staticmethod
    async def pause_row(
        *,
        row_id: uuid.UUID,
        by_user_id: uuid.UUID,
        db: AsyncSession,
    ) -> Optional[LeadFollowupSchedule]:
        row = await db.get(LeadFollowupSchedule, row_id)
        if row is None or row.status != FollowupStatus.pending:
            return None
        row.status = FollowupStatus.paused
        row.paused_at = datetime.now(timezone.utc)
        row.paused_by = by_user_id
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    @staticmethod
    async def resume_row(
        *, row_id: uuid.UUID, db: AsyncSession
    ) -> Optional[LeadFollowupSchedule]:
        row = await db.get(LeadFollowupSchedule, row_id)
        if row is None or row.status != FollowupStatus.paused:
            return None
        # Re-clamp the scheduled_at against the current call window — admin
        # may have shifted hours between pause and resume.
        rules: dict = DEFAULT_FOLLOWUP_RULES
        if row.campaign_id is not None:
            stmt = select(OutboundCampaign).where(
                OutboundCampaign.id == row.campaign_id
            )
            campaign = (await db.execute(stmt)).scalar_one_or_none()
            rules = effective_followup_rules(campaign)
        row.scheduled_at = clamp_to_call_window(
            max(row.scheduled_at, datetime.now(timezone.utc)), rules
        )
        row.status = FollowupStatus.pending
        row.paused_at = None
        row.paused_by = None
        db.add(row)
        await db.commit()
        await db.refresh(row)
        return row

    # ---------------------------------------------------------------- chip

    @staticmethod
    async def summary_for_tenant(
        *, tenant_id: str, db: AsyncSession
    ) -> dict:
        """Counts for the dashboard tile + leads-tab chips.

        Returns: scheduled (pending), today (pending with scheduled_at on
        today's date in UTC), exhausted, paused.
        """
        from sqlalchemy import func as sa_func

        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        stmt = (
            select(LeadFollowupSchedule.status, sa_func.count(LeadFollowupSchedule.id))
            .where(LeadFollowupSchedule.tenant_id == tenant_id)
            .group_by(LeadFollowupSchedule.status)
        )
        rows = (await db.execute(stmt)).all()
        counts = {str(status.value): int(count) for status, count in rows}

        today_stmt = (
            select(sa_func.count(LeadFollowupSchedule.id))
            .where(LeadFollowupSchedule.tenant_id == tenant_id)
            .where(LeadFollowupSchedule.status == FollowupStatus.pending)
            .where(LeadFollowupSchedule.scheduled_at >= day_start)
            .where(LeadFollowupSchedule.scheduled_at < day_end)
        )
        today_count = int((await db.execute(today_stmt)).scalar() or 0)

        return {
            "scheduled": counts.get("pending", 0),
            "in_flight": counts.get("in_flight", 0),
            "today": today_count,
            "exhausted": counts.get("exhausted", 0),
            "paused": counts.get("paused", 0),
            "completed": counts.get("completed", 0),
            "cancelled": counts.get("cancelled", 0),
        }

    @staticmethod
    def _due_bucket(scheduled_at: datetime, now: datetime) -> str:
        """Classify a pending row's ``scheduled_at`` into a queue bucket.

        Overdue rows (in the past) fold into ``today`` — both demand attention
        now. Pure function so the bucketing is unit-testable without a DB.
        """
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        if scheduled_at < day_end:
            return "today"  # incl. overdue
        if scheduled_at < day_end + timedelta(days=1):
            return "tomorrow"
        if scheduled_at < day_start + timedelta(days=7):
            return "this_week"
        return "later"

    @staticmethod
    def _conversion_rate(converted: int, placed: int) -> float:
        """converted / placed, guarded against divide-by-zero. Rounded to 4dp."""
        return round(converted / placed, 4) if placed else 0.0

    @staticmethod
    async def pipeline_for_tenant(*, tenant_id: str, db: AsyncSession) -> dict:
        """Full Follow-Up Pipeline payload for the dedicated page.

        One owner for the funnel view: overall status counts, the pending
        queue bucketed by due window (overdue + today folded together since
        both need to go out now), today's actionable list joined to the lead
        (name / phone / handoff note), a recent-called sample, and the
        month's conversion ROI.

        Conversion rate = converted / placed where ``placed`` = completed +
        converted this month (calls actually dialled). ``converted`` rows are
        the ones the conversion kill switch cancelled with
        ``cancelled_reason='converted'``.
        """
        from sqlalchemy import func as sa_func

        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        last_7d = now - timedelta(days=7)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        T = LeadFollowupSchedule

        # ── Overall status counts ────────────────────────────────────
        status_rows = (
            await db.execute(
                select(T.status, sa_func.count(T.id))
                .where(T.tenant_id == tenant_id)
                .group_by(T.status)
            )
        ).all()
        counts_by_status = {str(s.value): int(c) for s, c in status_rows}
        counts = {
            "scheduled": counts_by_status.get("pending", 0),
            "in_flight": counts_by_status.get("in_flight", 0),
            "completed": counts_by_status.get("completed", 0),
            "exhausted": counts_by_status.get("exhausted", 0),
            "paused": counts_by_status.get("paused", 0),
            "cancelled": counts_by_status.get("cancelled", 0),
        }

        # ── Pending queue, bucketed by due window ─────────────────────
        # Overdue (scheduled_at < day_end and in the past) folds into "today"
        # since both demand attention now. tomorrow / this_week / later split
        # the rest of the pending backlog.
        pending_due = (
            await db.execute(
                select(T.scheduled_at)
                .where(T.tenant_id == tenant_id)
                .where(T.status == FollowupStatus.pending)
            )
        ).scalars().all()
        queue_buckets = {"today": 0, "tomorrow": 0, "this_week": 0, "later": 0}
        for sched in pending_due:
            if sched is None:
                continue
            queue_buckets[FollowupSchedulerService._due_bucket(sched, now)] += 1

        # ── Today's actionable queue (today + overdue), with the lead ──
        queue_rows = (
            await db.execute(
                select(T, OutgoingLead)
                .join(OutgoingLead, OutgoingLead.id == T.lead_id)
                .where(T.tenant_id == tenant_id)
                .where(T.status == FollowupStatus.pending)
                .where(T.scheduled_at < day_end)
                .order_by(T.scheduled_at.asc())
            )
        ).all()
        today_queue = [
            {
                "followup_id": str(row.id),
                "lead_id": str(row.lead_id),
                "name": lead.name,
                "phone_e164": lead.phone_e164 or lead.phone_raw,
                "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
                "overdue": bool(row.scheduled_at and row.scheduled_at < now),
                "reason": row.reason.value if row.reason else None,
                "attempts": int(row.attempts or 0),
                "campaign_id": str(row.campaign_id) if row.campaign_id else None,
                "handoff_note": lead.handoff_note,
                "handoff_note_generated_at": (
                    lead.handoff_note_generated_at.isoformat()
                    if lead.handoff_note_generated_at else None
                ),
            }
            for row, lead in queue_rows
        ]

        # ── Called: completed in the last 7 days + exhausted (failed) ──
        called_week = int(
            (
                await db.execute(
                    select(sa_func.count(T.id))
                    .where(T.tenant_id == tenant_id)
                    .where(T.status == FollowupStatus.completed)
                    .where(T.updated_at >= last_7d)
                )
            ).scalar()
            or 0
        )

        # ── Conversion ROI for the current month ──────────────────────
        converted = int(
            (
                await db.execute(
                    select(sa_func.count(T.id))
                    .where(T.tenant_id == tenant_id)
                    .where(T.cancelled_reason == "converted")
                    .where(T.updated_at >= month_start)
                )
            ).scalar()
            or 0
        )
        completed_month = int(
            (
                await db.execute(
                    select(sa_func.count(T.id))
                    .where(T.tenant_id == tenant_id)
                    .where(T.status == FollowupStatus.completed)
                    .where(T.updated_at >= month_start)
                )
            ).scalar()
            or 0
        )
        placed = converted + completed_month
        rate = FollowupSchedulerService._conversion_rate(converted, placed)

        return {
            "counts": counts,
            "queue_buckets": queue_buckets,
            "today_queue": today_queue,
            "called": {"this_week": called_week, "failed": counts["exhausted"]},
            "conversion": {"converted": converted, "placed": placed, "rate": rate},
        }

    @staticmethod
    async def for_lead(
        *, lead_id: uuid.UUID, db: AsyncSession
    ) -> list[LeadFollowupSchedule]:
        """All rows for one lead, newest first. Powers the per-lead chip."""
        stmt = (
            select(LeadFollowupSchedule)
            .where(LeadFollowupSchedule.lead_id == lead_id)
            .order_by(LeadFollowupSchedule.created_at.desc())
        )
        res = await db.execute(stmt)
        return list(res.scalars().all())
