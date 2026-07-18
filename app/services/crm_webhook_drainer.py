"""Background drainer for ``crm_webhook_outbox`` — at-least-once delivery.

Claims due pending rows with ``FOR UPDATE SKIP LOCKED`` (replica-safe), POSTs
each signed payload to its campaign's Result Webhook URL, and either marks it
delivered or reschedules per ``BACKOFF_SCHEDULE`` until it goes ``dead``.

Best-effort + fail-open, mirroring ``outbound_campaign_ticker`` (single-process
loop, jittered start, shutdown-aware sleep). A short tick keeps first delivery
snappy — the claim query is a partial-index scan that is empty ~always.
"""
from __future__ import annotations

import asyncio
import logging
import random
import uuid as _uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, text

from app.db.session import AsyncSessionLocal
from app.models.outbound_campaign import OutboundCampaign
from app.services import crm_webhook_service as svc

logger = logging.getLogger(__name__)

TICK_INTERVAL_SECONDS = 30
_CLAIM_BATCH = 20

_task: asyncio.Task | None = None
_shutdown = asyncio.Event()


async def _drain_once() -> int:
    """One pass: claim → deliver → settle. Returns rows processed."""
    async with AsyncSessionLocal() as db:
        claimed = (
            await db.execute(
                text(
                    "SELECT id FROM crm_webhook_outbox "
                    "WHERE status = 'pending' AND next_attempt_at <= now() "
                    "ORDER BY next_attempt_at LIMIT :n FOR UPDATE SKIP LOCKED"
                ),
                {"n": _CLAIM_BATCH},
            )
        ).scalars().all()
        if not claimed:
            return 0
        # Nudge claimed rows into the future so a crash mid-delivery re-tries
        # them on a later tick instead of losing them, then release the locks —
        # the actual HTTP happens OUTSIDE any row lock.
        await db.execute(
            text(
                "UPDATE crm_webhook_outbox SET next_attempt_at = now() + interval '120 seconds' "
                "WHERE id = ANY(:ids)"
            ),
            {"ids": [str(i) for i in claimed]},
        )
        await db.commit()

    processed = 0
    secrets_by_campaign: dict[_uuid.UUID, tuple[str | None, str | None]] = {}
    for row_id in claimed:
        async with AsyncSessionLocal() as db:
            from app.models.crm_webhook_outbox import CrmWebhookOutbox

            row = (
                await db.execute(select(CrmWebhookOutbox).where(CrmWebhookOutbox.id == row_id))
            ).scalars().first()
            if row is None or row.status != "pending":
                continue
            if row.campaign_id not in secrets_by_campaign:
                campaign = (
                    await db.execute(
                        select(OutboundCampaign).where(OutboundCampaign.id == row.campaign_id)
                    )
                ).scalars().first()
                url = None
                secret = None
                if campaign is not None:
                    from app.services.apex_campaign_key_service import signing_secret, webhook_config

                    cfg = webhook_config(campaign)
                    url = cfg.get("url") if cfg.get("enabled") else None
                    secret = await signing_secret(campaign)
                secrets_by_campaign[row.campaign_id] = (url, secret)
            url, secret = secrets_by_campaign[row.campaign_id]

            now = datetime.now(timezone.utc)
            if not url:
                # Webhook was un-configured after enqueue — nothing to deliver to.
                row.status = "dead"
                row.last_error = "no webhook url configured"
            else:
                status_code, error = await svc.post_signed(url, secret, row.payload or {})
                row.attempt = int(row.attempt or 0) + 1
                row.last_status_code = status_code
                row.last_error = error
                if status_code is not None and 200 <= status_code < 300:
                    row.status = "delivered"
                    row.delivered_at = now
                elif row.attempt >= len(svc.BACKOFF_SCHEDULE):
                    row.status = "dead"
                else:
                    row.next_attempt_at = now + timedelta(
                        seconds=svc.BACKOFF_SCHEDULE[row.attempt - 1]
                    )
            db.add(row)
            await db.commit()
            processed += 1
    return processed


async def _tick_loop() -> None:
    await asyncio.sleep(random.uniform(0, 10))
    while not _shutdown.is_set():
        try:
            # Keep draining while there's a backlog; sleep only when caught up.
            while await _drain_once() > 0:
                if _shutdown.is_set():
                    return
        except Exception:
            logger.exception("CRM-WEBHOOK-DRAINER: tick failed")
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_shutdown.wait(), timeout=TICK_INTERVAL_SECONDS)


def start_crm_webhook_drainer() -> None:
    """Idempotent. Wires into FastAPI's startup hook in :mod:`app.main`."""
    global _task
    if _task is not None and not _task.done():
        return
    _shutdown.clear()
    _task = asyncio.create_task(_tick_loop(), name="crm-webhook-drainer")
    logger.info("CRM-WEBHOOK-DRAINER: started (interval=%ss)", TICK_INTERVAL_SECONDS)


async def stop_crm_webhook_drainer() -> None:
    _shutdown.set()
    if _task is not None and not _task.done():
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(_task, timeout=5)
