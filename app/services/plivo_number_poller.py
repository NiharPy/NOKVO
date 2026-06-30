"""Background poller — rent a Plivo DID once its compliance application is approved.

India DIDs can't be bought at signup: the business KYC (a Plivo *compliance
application*) must be APPROVED by Plivo first, and that review is asynchronous
(minutes to days, on Plivo's side). The onboarding documents step files the
application; this poller watches each pending tenant's application and, the
moment Plivo flips it to ``approved``, rents + assigns a number and persists it
on the tenant. No manual step — the number simply appears once Plivo clears it.

Everything is best-effort and fail-open: a Plivo/Redis blip just defers the
tenant to the next tick. Single-process loop, mirroring ``followup_scheduler``.
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.tenant_resources import TenantResources
from app.services.plivo_compliance_service import PlivoComplianceService
from app.services.plivo_service import PlivoError, PlivoService

logger = logging.getLogger(__name__)

# India compliance review is slow — polling every few minutes is plenty and
# keeps Plivo API load trivial.
POLL_INTERVAL_SECONDS = 600  # 10 minutes

_task: asyncio.Task | None = None
_shutdown = asyncio.Event()


def _is_approved(status: str | None) -> bool:
    """Plivo compliance-application status values vary ('approved', 'Approved',
    'in-review', 'pending', 'rejected', …). Treat anything containing 'approv'
    as approved, and never act on a rejection."""
    s = (status or "").strip().lower()
    return "approv" in s and "reject" not in s


async def _poll_tenant(db, tr: TenantResources) -> bool:
    """Check one tenant's compliance application; rent + assign a number if it's
    approved. Returns True when a number was allotted this tick."""
    plivo = dict((tr.provider_status or {}).get("plivo") or {})
    if plivo.get("number"):
        return False  # already has a number
    compliance = dict(plivo.get("compliance") or {})
    compliance_app_id = compliance.get("application_id")
    if not compliance_app_id:
        return False  # no compliance application filed yet (or it errored pre-file)
    if (plivo.get("number_status") or "") not in ("pending_compliance", "pending_verification", ""):
        return False

    auth = PlivoService._master_auth()
    base = PlivoService._base(auth[0])
    try:
        appdata = await PlivoService._request(
            "GET", f"{base}/ComplianceApplication/{compliance_app_id}/", auth=auth
        )
    except PlivoError as exc:
        logger.info("PLIVO-POLLER: status check failed for tenant=%s: %s", tr.tenant_id, exc)
        return False
    status = str(appdata.get("status") or appdata.get("application_status") or "")
    if not _is_approved(status):
        logger.debug("PLIVO-POLLER: tenant=%s compliance still '%s'", tr.tenant_id, status)
        return False

    # Approved → rent + assign, linking the approved compliance application.
    try:
        rented = await PlivoService.rent_number(
            country=settings.PLIVO_NUMBER_COUNTRY,
            app_id=plivo.get("application_id"),
            sub_auth_id=plivo.get("subaccount_auth_id"),
            compliance_application_id=str(compliance_app_id),
        )
    except PlivoError as exc:
        logger.info("PLIVO-POLLER: approved but not yet rentable for tenant=%s: %s", tr.tenant_id, exc)
        return False
    number = rented.get("number")
    if not number:
        return False

    PlivoComplianceService._save(tr, compliance=compliance, number=number, number_status="active")
    flag_modified(tr, "provider_status")
    db.add(tr)
    await db.commit()
    logger.info("PLIVO-POLLER: allotted number %s for tenant=%s", number, tr.tenant_id)
    return True


async def _poll_tenant_bulk(db, tr: TenantResources) -> None:
    """Top up an APEX tenant's AUTO-PROVISIONED bulk DID pool once its onboarding
    compliance is approved. No-op unless ``bulk_calling.status == 'provisioning'``
    (only the APEX one-click auto-provision sets that), so this is naturally scoped
    to APEX and idle for everyone else."""
    bulk = dict((tr.provider_status or {}).get("bulk_calling") or {})
    if bulk.get("status") != "provisioning" or not bulk.get("auto_provisioned"):
        return
    from app.services.plivo_bulk_provisioning_service import PlivoBulkProvisioningService

    await PlivoBulkProvisioningService._buy_pending_numbers(tr, db)


async def _poll_once() -> None:
    if not (settings.PLIVO_AUTH_ID and settings.PLIVO_AUTH_TOKEN):
        return  # Plivo not configured — nothing to poll.
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(TenantResources))).scalars().all()
        for tr in rows:
            try:
                await _poll_tenant(db, tr)
            except Exception:
                logger.exception("PLIVO-POLLER: unexpected error for tenant=%s", tr.tenant_id)
                with suppress(Exception):
                    await db.rollback()
            # Independent of the per-tenant inbound number: top up the bulk pool.
            try:
                await _poll_tenant_bulk(db, tr)
            except Exception:
                logger.exception("PLIVO-POLLER: bulk top-up failed for tenant=%s", tr.tenant_id)
                with suppress(Exception):
                    await db.rollback()


async def _poll_loop() -> None:
    import random

    await asyncio.sleep(random.uniform(0, 30))
    while not _shutdown.is_set():
        try:
            await _poll_once()
        except Exception:
            logger.exception("PLIVO-POLLER: poll cycle failed")
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_shutdown.wait(), timeout=POLL_INTERVAL_SECONDS)


def start_plivo_number_poller() -> None:
    """Idempotent. Wires into FastAPI's startup hook in :mod:`app.main`."""
    global _task
    if _task is not None and not _task.done():
        return
    _shutdown.clear()
    _task = asyncio.create_task(_poll_loop(), name="plivo-number-poller")
    logger.info("PLIVO-POLLER: started (interval=%ss)", POLL_INTERVAL_SECONDS)


async def stop_plivo_number_poller() -> None:
    _shutdown.set()
    if _task is not None and not _task.done():
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(_task, timeout=5)
