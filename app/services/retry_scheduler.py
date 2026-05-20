"""In-process retry-queue scheduler.

A single asyncio task started at FastAPI startup that calls
:func:`ToolRetryService.process_due` every ``RETRY_DRAIN_INTERVAL_SECONDS``.
Each pass picks up any pending tool retry whose backoff has elapsed and
re-runs it through the same execution path the live voice pipeline uses.

Why in-process instead of a dedicated worker? Two reasons:

1. The current deployment runs a single uvicorn instance — there is no
   pre-existing worker infra to wire into. A background asyncio task gives
   us automated drain without adding ops surface.
2. The drain is bounded: it batches a small number of due retries per pass
   and yields between them, so it never starves request handling.

If you ever split this into a Celery / RQ / dramatiq worker, the entry
point you'd port is ``process_due`` itself — the rest of this file is just
scheduling glue.
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress

from app.db.session import AsyncSessionLocal
from app.services.tool_retry_service import ToolRetryService


logger = logging.getLogger(__name__)


RETRY_DRAIN_INTERVAL_SECONDS = 120  # one drain pass every 2 minutes
RETRY_DRAIN_JITTER_SECONDS = 15     # spread load if multiple instances boot


_task: asyncio.Task | None = None
_shutdown = asyncio.Event()


async def _drain_once() -> None:
    """One pass over the queue. Opens its own session so it doesn't
    contend with a request-scoped session."""
    try:
        async with AsyncSessionLocal() as db:
            report = await ToolRetryService.process_due(db)
        if report:
            logger.info("Retry drain processed %s entries: %s", len(report), report)
    except Exception:
        logger.exception("Retry drain pass failed")


async def _scheduler_loop() -> None:
    # Small jitter so multi-instance deployments don't hammer the queue
    # in lockstep on cold start.
    import random

    await asyncio.sleep(random.uniform(0, RETRY_DRAIN_JITTER_SECONDS))
    while not _shutdown.is_set():
        await _drain_once()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(_shutdown.wait(), timeout=RETRY_DRAIN_INTERVAL_SECONDS)


def start_scheduler() -> None:
    """Idempotent. Wires into FastAPI's startup hook in :mod:`app.main`."""
    global _task
    if _task is not None and not _task.done():
        return
    _shutdown.clear()
    _task = asyncio.create_task(_scheduler_loop(), name="retry-scheduler")
    logger.info("Retry scheduler started (interval=%ss)", RETRY_DRAIN_INTERVAL_SECONDS)


async def stop_scheduler() -> None:
    """Called from FastAPI shutdown — gives the loop a chance to exit
    cleanly between passes instead of being torn down mid-drain."""
    _shutdown.set()
    if _task is not None and not _task.done():
        with suppress(asyncio.CancelledError, asyncio.TimeoutError):
            await asyncio.wait_for(_task, timeout=5)
