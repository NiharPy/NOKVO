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
from app.models.notification import (
    NOTIFICATION_LEAD_SYNC,
    NOTIFICATION_SYNC_QUIET,
    SEVERITY_P1,
    SEVERITY_P2,
)
from app.models.outgoing_lead import LeadConnectionStatus, LeadSourceConnection
from app.models.tenant_resources import TenantResources
from app.services.notification_service import NotificationService
from app.services.scheduler_leader import scheduler_leader
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
        async with scheduler_leader(
            "lead-sync",
            ttl_seconds=LEAD_SYNC_INTERVAL_SECONDS,
        ) as is_leader:
            if not is_leader:
                return
            async with AsyncSessionLocal() as db:
                res = await db.execute(
                    select(LeadSourceConnection).where(
                        LeadSourceConnection.status == LeadConnectionStatus.connected
                    )
                )
                connections = list(res.scalars().all())
                for connection in connections:
                    try:
                        result = await OutgoingLeadService.sync_connection(connection, db)
                        processed += 1
                        await _emit_sync_captured_notification(db, connection, result)
                    except Exception as exc:
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
                        await _emit_sync_quiet_notification(db, connection, exc)
    except Exception:
        logger.exception("Lead-sync scheduler pass crashed")
    if processed or failed:
        logger.info(
            "Lead-sync pass complete: %s synced, %s failed",
            processed,
            failed,
        )


async def _resolve_org_id(db, tenant_id: str):
    """Look up the organization UUID for a tenant. We need this because
    LeadSourceConnection is keyed by ``tenant_id`` (a string) but the
    notification inbox is keyed by the organization UUID."""
    res = await db.execute(
        select(TenantResources.organization_id).where(
            TenantResources.tenant_id == tenant_id
        )
    )
    return res.scalar_one_or_none()


async def _emit_sync_captured_notification(db, connection, result) -> None:
    """Fire a P2 ``lead_sync_captured`` notification when fresh leads came in.

    A pass that imports zero leads is the common case (provider was quiet
    since the last sync) and we deliberately don't notify on it — the
    inbox would flood. We emit only when ``leads > 0``.
    """
    try:
        leads = int((result or {}).get("leads") or 0)
        if leads <= 0:
            return
        org_id = await _resolve_org_id(db, connection.tenant_id)
        if org_id is None:
            return
        provider_name = getattr(connection.provider, "value", str(connection.provider))
        await NotificationService.emit(
            db,
            organization_id=org_id,
            tenant_id=connection.tenant_id,
            type=NOTIFICATION_LEAD_SYNC,
            title=f"{leads} new lead{'s' if leads != 1 else ''} from {connection.display_name}",
            body=f"Synced from {provider_name}.",
            severity=SEVERITY_P2,
            payload={
                "connection_id": str(connection.id),
                "provider": provider_name,
                "leads": leads,
                "forms": int((result or {}).get("forms") or 0),
            },
            dedup_key=f"lead_sync_captured:{connection.id}",
        )
    except Exception:
        logger.exception("NOKVO-NOTIF: failed to emit lead_sync_captured")


async def _emit_sync_quiet_notification(db, connection, exc) -> None:
    """Fire a P1 ``lead_sync_quiet`` notification when a provider goes dark.

    Dedup key collapses repeated fires into one — a permanently broken
    OAuth grant would otherwise post every 30 minutes until reconnected.
    """
    try:
        org_id = await _resolve_org_id(db, connection.tenant_id)
        if org_id is None:
            return
        provider_name = getattr(connection.provider, "value", str(connection.provider))
        await NotificationService.emit(
            db,
            organization_id=org_id,
            tenant_id=connection.tenant_id,
            type=NOTIFICATION_SYNC_QUIET,
            title=f"Lead source {connection.display_name} is offline",
            body=str(exc)[:400],
            severity=SEVERITY_P1,
            payload={
                "connection_id": str(connection.id),
                "provider": provider_name,
                "error": str(exc)[:1000],
            },
            dedup_key=f"lead_sync_quiet:{connection.id}",
        )
    except Exception:
        logger.exception("NOKVO-NOTIF: failed to emit lead_sync_quiet")


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
