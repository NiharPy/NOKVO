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
from datetime import datetime, timedelta, timezone

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
            # Reconcile rows whose status webhook never arrived BEFORE the
            # drain so a stuck lead/customer can be re-scheduled by the admin
            # instead of sitting in_flight forever.
            try:
                await _reconcile_in_flight()
            except Exception:
                logger.exception("Follow-up in-flight reconciliation failed")
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


async def _reconcile_in_flight() -> None:
    """Close out in_flight rows whose status webhook never arrived.

    Two paths per timed-out row:
      1. The webhook arrived but raced row creation — `plivo_outbound_status`
         stashed it under ``plivo:orphan-status:{call_link_id}``. Replay it
         through the normal handler so the row completes properly.
      2. Nothing arrived → mark the row failed with reason="webhook_timeout"
         so the chip reads honestly and the admin can re-schedule.
    """
    from datetime import datetime, timezone

    from app.models.lead_followup_schedule import FollowupStatus as FS

    timeout_minutes = int(settings.FOLLOWUP_INFLIGHT_TIMEOUT_MINUTES or 30)
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        rows = (
            (
                await db.execute(
                    select(LeadFollowupSchedule)
                    .where(LeadFollowupSchedule.status == FS.in_flight)
                    .limit(50)
                )
            )
            .scalars()
            .all()
        )
        stale = [
            row for row in rows
            if FollowupSchedulerService.is_webhook_timed_out(row, now, timeout_minutes)
        ]
        if not stale:
            return
        for row in stale:
            orphan_payload = None
            if row.placed_call_id:
                try:
                    from app.services.agent_session_store import AgentSessionStore

                    raw = await AgentSessionStore.client().get(
                        f"plivo:orphan-status:{row.placed_call_id}"
                    )
                    if raw:
                        import json as _json

                        orphan_payload = _json.loads(raw)
                except Exception:
                    orphan_payload = None
            if orphan_payload is not None:
                try:
                    from app.services.outbound_campaign_service import OutboundCampaignService

                    campaign, contact = await OutboundCampaignService.get_by_call_link_id(
                        row.placed_call_id, db
                    )
                    if contact is not None:
                        await OutboundCampaignService.handle_call_status(
                            campaign, row.placed_call_id, "call.hangup", orphan_payload, db,
                            followup_contact=contact if contact.get("is_followup") else None,
                        )
                        logger.info(
                            "Follow-up %s reconciled from orphan status event", row.id
                        )
                        continue
                except Exception:
                    logger.exception("Orphan-status replay failed for row %s", row.id)
            await FollowupSchedulerService.mark_failed(
                row=row, reason="webhook_timeout", db=db
            )
            logger.warning(
                "Follow-up %s stuck in_flight > %sm with no status webhook — marked failed",
                row.id, timeout_minutes,
            )


async def _dispatch_one(row_id: uuid.UUID) -> None:
    """Place exactly one follow-up call. Re-loads the row + lead + campaign
    so any state change between enqueue and now (opt-out flip, manual
    pause, campaign cancelled) is honoured.
    """
    async with AsyncSessionLocal() as db:
        row = await db.get(LeadFollowupSchedule, row_id)
        if row is None or row.status != FollowupStatus.pending:
            return

        # Re-load the target and verify caps. Two target styles: a lead
        # (real-estate campaign path) or a customer (clinic manual path).
        lead = None
        customer = None
        if row.lead_id is not None:
            lead = await db.get(OutgoingLead, row.lead_id)
            if lead is None or lead.consent_status == LeadConsentStatus.revoked:
                await FollowupSchedulerService.mark_failed(
                    row=row, reason="lead_revoked_or_missing", db=db
                )
                return
        elif row.customer_id is not None:
            from app.models.customer_base import CustomerBase

            customer = await db.get(CustomerBase, row.customer_id)
            if customer is None or customer.opt_out:
                await FollowupSchedulerService.mark_failed(
                    row=row, reason="customer_opted_out_or_missing", db=db
                )
                return
        else:
            await FollowupSchedulerService.mark_failed(
                row=row, reason="no_target", db=db
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

        # Per-tenant concurrency cap: don't place an outbound call we can't
        # service. At capacity the media WS would just hang up, so defer this
        # row a minute and let a later tick retry when a line frees up.
        from app.services.call_concurrency import active_count

        if await active_count(tenant_res.tenant_id) >= int(
            settings.NOKVO_MAX_CONCURRENT_CALLS_PER_TENANT or 10
        ):
            row.scheduled_at = now + timedelta(minutes=1)
            db.add(row)
            await db.commit()
            logger.info("Follow-up %s deferred — tenant at call capacity", row.id)
            return

        # Pick a callable phone number.
        if lead is not None:
            phone = lead.phone_e164 or lead.phone_raw
        else:
            phone = customer.phone_e164
        if not phone:
            await FollowupSchedulerService.mark_failed(
                row=row, reason="lead_phone_missing", db=db
            )
            return

        # Generate a fresh call_link_id and place the call. The webhook
        # extension (task 36) resolves this id back to this row.
        call_link_id = str(uuid.uuid4())
        from app.services.public_url import public_base_url

        base = public_base_url()
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
            "Follow-up call placed: row=%s target=%s attempt=%s",
            row.id,
            f"lead:{lead.id}" if lead is not None else f"customer:{customer.id}",
            row.attempts,
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
