"""Periodic lead-source sync.

Walks every ``LeadSourceConnection`` in ``connected`` status and pulls fresh
leads in from the provider (Meta / Google Ads / Google Forms). Mirrors the
shape of :mod:`app.services.retry_scheduler`: a single in-process asyncio
task started during FastAPI lifespan, draining the queue on a fixed cadence,
each pass opens its own DB session so it doesn't contend with request
handlers.

Why in-process again
--------------------
Same logic as ``retry_scheduler``: the deployment is single-uvicorn for now,
the cadence is loose (30 min), and one DB read + N provider calls per pass
is cheap. When we shard onto multiple uvicorn workers we'll either
single-instance this via a leader-election flag in Redis or split it out to
its own worker — both swaps are local to this file.

Failure mode
------------
A failed sync stamps ``connection.last_error`` (via
``OutgoingLeadService.sync_connection``) and flips status to ``error`` so the
dashboard health summary surfaces it. We don't pause the queue or back off —
the next 30-min pass will retry. Persistent failures (revoked OAuth grants,
deleted forms) need the admin to disconnect and reconnect anyway.
"""
from __future__ import annotations

import asyncio
import logging
import random
from contextlib import suppress

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.outgoing_lead import LeadConnectionStatus, LeadSourceConnection
from app.services.outgoing_lead_service import OutgoingLeadService


logger = logging.getLogger(__name__)

# Cadence and jitter. 30-min interval per spec; a small jitter on cold start
# so concurrent uvicorn replicas (when we get there) don't lockstep the
# upstream APIs.
LEAD_SYNC_INTERVAL_SECONDS = 30 * 60
LEAD_SYNC_JITTER_SECONDS = 45

_task: asyncio.Task | None = None
_shutdown = asyncio.Event()


async def _drain_once() -> None:
    """One scheduler pass. Pull every connection in ``connected`` state and
    re-sync it. Errors per-connection are isolated so a single bad token
    doesn't starve the rest of the queue."""
    processed = 0
    failed = 0
    try:
        async with AsyncSessionLocal() as db:
            res = await db.execute(
                select(LeadSourceConnection).where(
                    LeadSourceConnection.status == LeadConnectionStatus.connected
                )
            )
            connections = list(res.scalars().all())
            for connection in connections:
                try:
                    await OutgoingLeadService.sync_connection(connection, db)
                    processed += 1
                except Exception:
                    # OutgoingLeadService.sync_connection has already stamped
                    # connection.last_error and flipped status to ``error``;
                    # we log here for ops visibility but don't re-raise.
                    failed += 1
                    logger.warning(
                        "Lead sync failed for connection %s (%s)",
                        connection.id,
                        connection.provider,
                        exc_info=True,
                    )
    except Exception:
        logger.exception("Lead-sync scheduler pass crashed")
    if processed or failed:
        logger.info(
            "Lead-sync pass complete: %s synced, %s failed",
            processed,
            failed,
        )


async def _scheduler_loop() -> None:
    await asyncio.sleep(random.uniform(0, LEAD_SYNC_JITTER_SECONDS))
    while not _shutdown.is_set():
        await _drain_once()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(
                _shutdown.wait(), timeout=LEAD_SYNC_INTERVAL_SECONDS
            )


def start_scheduler() -> None:
    """Idempotent. Called from FastAPI startup in :mod:`app.main`."""
    global _task
    if _task is not None and not _task.done():
        return
    _shutdown.clear()
    _task = asyncio.create_task(_scheduler_loop(), name="lead-sync-scheduler")
    logger.info(
        "Lead-sync scheduler started (interval=%ss)",
        LEAD_SYNC_INTERVAL_SECONDS,
    )


async def stop_scheduler() -> None:
    """Lifespan shutdown. Lets the loop exit between passes rather than tearing
    it down mid-drain."""
    _shutdown.set()
    if _task is not None and not _task.done():
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(_task, timeout=5)
