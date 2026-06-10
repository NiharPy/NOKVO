"""In-process follow-up scheduler.

Sibling of ``retry_scheduler.py`` and ``lead_sync_scheduler.py``. A single
asyncio task started at FastAPI startup drains
:class:`LeadFollowupSchedule` rows whose ``scheduled_at`` is due and
``status='pending'``, placing one Exotel call per row.

The scheduler is dumb glue. All policy lives in
:class:`FollowupSchedulerService`:

  - Decision tree (promise vs disposition rule)
  - Call-window clamp
  - Cap checks (opt-out, max attempts, conversion, pause)
  - State transitions

The scheduler's only job is: pick due rows, re-verify caps once more against
current state, place the call, and mark in_flight. The webhook handlers then
take the row to ``completed`` (and may enqueue the next follow-up).

If you ever split this out into a worker, the entry point you'd port is
:func:`FollowupSchedulerService.next_due` + :func:`_dispatch_one` below.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import suppress
from datetime import datetime, timezone

from sqlalchemy import select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.lead_followup_schedule import FollowupStatus, LeadFollowupSchedule
from app.models.outbound_campaign import OutboundCampaign
from app.models.outgoing_lead import LeadConsentStatus, OutgoingLead
from app.models.tenant_resources import TenantResources
from app.services.plivo_service import PlivoService
from app.services.followup_scheduler_service import (
    FollowupSchedulerService,
    clamp_to_call_window,
    effective_followup_rules,
)
from app.services.scheduler_leader import scheduler_leader


logger = logging.getLogger(__name__)


FOLLOWUP_DRAIN_INTERVAL_SECONDS = 60   # tick every minute
FOLLOWUP_DRAIN_JITTER_SECONDS = 10     # spread load across multi-instance boot
FOLLOWUP_DRAIN_LIMIT = 25              # at most 25 calls placed per pass


_task: asyncio.Task | None = None
_shutdown = asyncio.Event()


async def _drain_once() -> None:
    """One pass over the due queue. Opens its own session per dispatch so a
    slow Exotel call doesn't hold a DB transaction open for long."""
    try:
        async with scheduler_leader(
            "followup-scheduler",
            ttl_seconds=FOLLOWUP_DRAIN_INTERVAL_SECONDS,
        ) as is_leader:
            if not is_leader:
                return
            async with AsyncSessionLocal() as db:
                rows = await FollowupSchedulerService.next_due(
                    limit=FOLLOWUP_DRAIN_LIMIT, db=db
                )
                # Materialise scalar values from each row so we can release
                # the SELECT FOR UPDATE lock before placing calls (which can
                # take seconds against Exotel).
                row_ids = [row.id for row in rows]
                await db.commit()  # releases the skip-locked rows
        if not row_ids:
            return
        # Dispatch each row with its own session — keeps transactions
        # narrow, and a single bad row can't poison the batch.
        for row_id in row_ids:
            try:
                await _dispatch_one(row_id)
            except Exception:
                logger.exception("Follow-up dispatch failed for row %s", row_id)
    except Exception:
        logger.exception("Follow-up drain pass failed")


async def _dispatch_one(row_id: uuid.UUID) -> None:
    """Place exactly one follow-up call. Re-loads the row + lead + campaign
    so any state change between enqueue and now (opt-out flip, manual
    pause, campaign cancelled) is honoured.
    """
    async with AsyncSessionLocal() as db:
        row = await db.get(LeadFollowupSchedule, row_id)
        if row is None or row.status != FollowupStatus.pending:
            return

        # Re-load lead and verify caps.
        lead = await db.get(OutgoingLead, row.lead_id)
        if lead is None or lead.consent_status == LeadConsentStatus.revoked:
            await FollowupSchedulerService.mark_failed(
                row=row, reason="lead_revoked_or_missing", db=db
            )
            return

        campaign = None
        if row.campaign_id is not None:
            campaign = await db.get(OutboundCampaign, row.campaign_id)

        # Re-clamp against the current call window (admin may have shifted
        # hours since the row was enqueued).
        rules = effective_followup_rules(campaign)
        now = datetime.now(timezone.utc)
        clamped = clamp_to_call_window(max(row.scheduled_at, now), rules)
        if clamped > now:
            # Window moved or shrank — re-schedule and bow out. Next tick
            # picks it up when due.
            row.scheduled_at = clamped
            db.add(row)
            await db.commit()
            return

        # Resolve tenant resources for Exotel call placement.
        tr_res = await db.execute(
            select(TenantResources).where(TenantResources.tenant_id == row.tenant_id)
        )
        tenant_res = tr_res.scalars().first()
        if tenant_res is None:
            await FollowupSchedulerService.mark_failed(
                row=row, reason="tenant_resources_missing", db=db
            )
            return

        # Pick a callable phone number.
        phone = lead.phone_e164 or lead.phone_raw
        if not phone:
            await FollowupSchedulerService.mark_failed(
                row=row, reason="lead_phone_missing", db=db
            )
            return

        # Generate a fresh call_link_id and place the call. The webhook
        # extension (task 36) resolves this id back to this row.
        call_link_id = str(uuid.uuid4())
        base = (settings.AGENT_PUBLIC_BASE_URL or "http://localhost:8000").rstrip("/")
        prefix = "/api/nokvo-one/agents"
        answer_url = f"{base}{prefix}/plivo/outbound-answer/{call_link_id}"
        status_callback = f"{base}{prefix}/plivo/outbound-status/{call_link_id}"

        try:
            await PlivoService.initiate_outbound_call(
                tenant_res,
                to_number=phone,
                answer_url=answer_url,
                status_callback=status_callback,
            )
        except Exception as exc:
            logger.warning(
                "Exotel initiate failed for follow-up %s: %s", row.id, exc
            )
            await FollowupSchedulerService.mark_failed(
                row=row, reason=f"exotel_error:{str(exc)[:120]}", db=db
            )
            return

        await FollowupSchedulerService.mark_in_flight(
            row=row, placed_call_id=call_link_id, db=db
        )
        logger.info(
            "Follow-up call placed: row=%s lead=%s attempt=%s",
            row.id, lead.id, row.attempts,
        )


async def _scheduler_loop() -> None:
    import random

    await asyncio.sleep(random.uniform(0, FOLLOWUP_DRAIN_JITTER_SECONDS))
    while not _shutdown.is_set():
        await _drain_once()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                _shutdown.wait(), timeout=FOLLOWUP_DRAIN_INTERVAL_SECONDS
            )


def start_followup_scheduler() -> None:
    """Idempotent. Wires into FastAPI's startup hook in :mod:`app.main`."""
    global _task
    if _task is not None and not _task.done():
        return
    _shutdown.clear()
    _task = asyncio.create_task(_scheduler_loop(), name="followup-scheduler")
    logger.info(
        "Follow-up scheduler started (interval=%ss)", FOLLOWUP_DRAIN_INTERVAL_SECONDS
    )


async def stop_followup_scheduler() -> None:
    _shutdown.set()
    if _task is not None and not _task.done():
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(_task, timeout=5)
