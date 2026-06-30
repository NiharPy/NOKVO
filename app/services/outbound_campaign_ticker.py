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
            # Only SCHEDULED campaigns need a timed wake-up — webhook-driven ones
            # already refill themselves one-for-one as calls end.
            if not ((campaign.agent_config or {}).get("call_window")):
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
