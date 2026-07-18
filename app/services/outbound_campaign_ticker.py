"""Background tick — resume scheduled outbound campaigns when their window reopens.

An APEX (or any deterministic) campaign carries a ``call_window`` on its
agent_config (09:00–19:00 IST, N weekdays, N calls/day). While a campaign is
PAUSED — outside the hour band, on an off day, or after the daily cap — NO
call-end webhook fires, so nothing re-triggers the dialer. This single-process
loop wakes every RUNNING windowed campaign on a fixed cadence and calls the
dialer; the time/day/cap/balance gates inside ``_dial_pending`` decide whether to
actually place a call. The cron simply guarantees a campaign comes back to life
the moment its window opens again.

Best-effort + fail-open, mirroring ``plivo_number_poller`` (single-process loop,
jittered start, shutdown-aware sleep).
"""
from __future__ import annotations

import asyncio
import logging
import random
from contextlib import suppress

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
from app.services.outbound_campaign_service import OutboundCampaignService
from app.services.public_url import public_base_url

logger = logging.getLogger(__name__)

# Wake cadence: short enough that a campaign resumes promptly when its 9 AM window
# opens, cheap enough to be trivial (each tick is a gated, idempotent dial check
# that no-ops when paused / at the concurrency cap).
TICK_INTERVAL_SECONDS = 600  # 10 minutes

_task: asyncio.Task | None = None
_shutdown = asyncio.Event()


async def _tick_once() -> None:
    async with AsyncSessionLocal() as db:
        # Stuck-row reaper (ALL running campaigns, windowed or not): release
        # contact rows whose terminal webhook never arrived, then complete any
        # campaign that turns out to be drained. Without this, one lost webhook
        # pins a dial slot forever and a finished list never flips to completed —
        # i.e. the campaign never "stops and waits for more contacts".
        try:
            from app.services import campaign_contacts_v2 as v2

            for campaign_id in await v2.reap_stale_rows(db):
                with suppress(Exception):
                    await v2.maybe_complete(db, campaign_id)
        except Exception:
            logger.exception("CAMPAIGN-TICKER: stale-row reap failed")
            with suppress(Exception):
                await db.rollback()

        # CRM-fed (webhook lead-source) campaigns: leads can land while the
        # campaign sits ``completed`` and another campaign holds the tenant's
        # single active slot (ingest then can't resume it). Once the slot frees,
        # nothing else would ever wake it — so the ticker retries the slot-safe
        # resume for any completed webhook campaign with pending rows.
        try:
            from app.services import campaign_contacts_v2 as v2c

            stranded = (
                (
                    await db.execute(
                        select(OutboundCampaign).where(
                            OutboundCampaign.status == CampaignStatus.completed,
                            OutboundCampaign.agent_config["lead_source"].astext == "webhook",
                        )
                    )
                )
                .scalars()
                .all()
            )
            for campaign in stranded:
                if await v2c.pending_count(db, campaign.id) <= 0:
                    continue
                try:
                    await OutboundCampaignService._assert_no_other_active_campaign(
                        db, campaign.tenant_id, exclude_id=campaign.id
                    )
                    campaign.status = CampaignStatus.running
                    campaign.completed_at = None
                    db.add(campaign)
                    await db.commit()
                    logger.info(
                        "CAMPAIGN-TICKER: resumed webhook campaign %s (queued leads)",
                        campaign.id,
                    )
                except ValueError:
                    await db.rollback()  # slot still busy — retry next tick
        except Exception:
            logger.exception("CAMPAIGN-TICKER: webhook-campaign resume sweep failed")
            with suppress(Exception):
                await db.rollback()

        try:
            rows = (
                (
                    await db.execute(
                        select(OutboundCampaign).where(
                            OutboundCampaign.status == CampaignStatus.running
                        )
                    )
                )
                .scalars()
                .all()
            )
        except Exception:
            logger.exception("CAMPAIGN-TICKER: failed to load running campaigns")
            return
        base = public_base_url()
        for campaign in rows:
            # SCHEDULED campaigns need a timed wake-up (call-end webhooks stop
            # while paused), and so do CRM-fed ones: leads can arrive while no
            # call is live (nothing refills one-for-one) and the ingest-time
            # dial kick is best-effort — the tick is their safety net.
            cfg = campaign.agent_config or {}
            if not (cfg.get("call_window") or cfg.get("lead_source") == "webhook"):
                continue
            try:
                await OutboundCampaignService.dial_next_pending(
                    campaign,
                    db,
                    public_base_url=base,
                    path_prefix="/api/nokvo-one/agents",
                )
            except Exception:
                logger.exception("CAMPAIGN-TICKER: tick failed for campaign=%s", campaign.id)
                with suppress(Exception):
                    await db.rollback()


async def _tick_loop() -> None:
    await asyncio.sleep(random.uniform(0, 30))
    while not _shutdown.is_set():
        try:
            await _tick_once()
        except Exception:
            logger.exception("CAMPAIGN-TICKER: tick cycle failed")
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_shutdown.wait(), timeout=TICK_INTERVAL_SECONDS)


def start_outbound_campaign_ticker() -> None:
    """Idempotent. Wires into FastAPI's startup hook in :mod:`app.main`."""
    global _task
    if _task is not None and not _task.done():
        return
    _shutdown.clear()
    _task = asyncio.create_task(_tick_loop(), name="outbound-campaign-ticker")
    logger.info("CAMPAIGN-TICKER: started (interval=%ss)", TICK_INTERVAL_SECONDS)


async def stop_outbound_campaign_ticker() -> None:
    _shutdown.set()
    if _task is not None and not _task.done():
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(_task, timeout=5)
