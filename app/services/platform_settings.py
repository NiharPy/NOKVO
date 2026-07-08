"""Runtime platform settings — the SuperAdmin-tunable USD→INR FX rate.

Every per-call COGS calculation converts vendor USD list prices with
``settings.USD_TO_INR`` (see :mod:`app.services.call_usage` — the reads happen
at call time, so mutating the in-process ``settings`` object changes all
future pricing immediately). This module makes that rate operator-tunable:

  * the SuperAdmin console writes the override to the ``platform_settings``
    row (``key='usd_to_inr'``) AND applies it to this instance's ``settings``
    at once;
  * every OTHER instance's background refresher (started from app startup,
    mirroring the ``llm_pool_keys`` refresher) folds the persisted value into
    its own ``settings`` within one interval — no redeploy;
  * a missing row means "use the env/config default", and clearing the
    override restores it.

Scope: the FX applies to per-call COGS priced FROM NOW ON. Historical
``call_costs`` rows keep the cost computed with the rate in force when they
were recorded (their raw token/second/char counters are stored precisely so
they *could* be re-priced, but the ledger deliberately freezes per row).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.platform_setting import PlatformSetting

logger = logging.getLogger(__name__)

USD_TO_INR_KEY = "usd_to_inr"

# Sanity bounds — a fat-fingered 8600 (or 0) must never silently reprice every
# call by 100×. Generous enough for any plausible INR/USD reality.
FX_MIN = 10.0
FX_MAX = 500.0

# The env/config default, captured ONCE at import — ``settings.USD_TO_INR``
# itself gets mutated by overrides, so "the default" must be remembered here.
DEFAULT_USD_TO_INR: float = float(settings.USD_TO_INR)

REFRESH_INTERVAL_SECONDS = 60  # one tiny PK read per instance per minute


def validate_fx(value) -> float:
    """Parse + bound-check a proposed rate. Raises ValueError with a
    user-facing message on junk or out-of-range input."""
    try:
        fx = float(value)
    except (TypeError, ValueError):
        raise ValueError("Enter the rate as a number, e.g. 86.5")
    if not (FX_MIN <= fx <= FX_MAX):
        raise ValueError(f"Rate must be between {FX_MIN:g} and {FX_MAX:g} ₹/$.")
    return round(fx, 4)


async def get_usd_to_inr(db: AsyncSession) -> dict:
    """The current effective rate + override metadata for the console."""
    row = await db.get(PlatformSetting, USD_TO_INR_KEY)
    override: float | None = None
    if row is not None:
        try:
            override = validate_fx(row.value)
        except ValueError:
            logger.warning("PLATFORM-FX: ignoring junk persisted rate %r", row.value)
    return {
        "usd_to_inr": override if override is not None else DEFAULT_USD_TO_INR,
        "default": DEFAULT_USD_TO_INR,
        "is_override": override is not None,
        "updated_at": row.updated_at.isoformat() if (row and row.updated_at) else None,
        "updated_by": row.updated_by if row else None,
    }


async def set_usd_to_inr(db: AsyncSession, value, *, updated_by: str | None) -> dict:
    """Persist the override AND apply it to this instance immediately.

    Other instances converge via their refresher within one interval. Raises
    ValueError on invalid input (the endpoint maps it to a 400)."""
    fx = validate_fx(value)
    stmt = (
        pg_insert(PlatformSetting.__table__)
        .values(key=USD_TO_INR_KEY, value=str(fx), updated_by=updated_by)
        .on_conflict_do_update(
            index_elements=["key"],
            set_={"value": str(fx), "updated_by": updated_by, "updated_at": datetime.utcnow()},
        )
    )
    await db.execute(stmt)
    await db.commit()
    settings.USD_TO_INR = fx  # all future compute_cogs_inr/llm_cost_inr reads
    logger.info("PLATFORM-FX: USD→INR set to %s by %s", fx, updated_by or "?")
    return await get_usd_to_inr(db)


async def clear_usd_to_inr(db: AsyncSession, *, updated_by: str | None) -> dict:
    """Remove the override — the config default takes effect again everywhere."""
    row = await db.get(PlatformSetting, USD_TO_INR_KEY)
    if row is not None:
        await db.delete(row)
        await db.commit()
    settings.USD_TO_INR = DEFAULT_USD_TO_INR
    logger.info("PLATFORM-FX: USD→INR override cleared by %s (back to %s)",
                updated_by or "?", DEFAULT_USD_TO_INR)
    return await get_usd_to_inr(db)


async def apply_persisted_fx() -> None:
    """Fold the persisted rate (or the default, when no row) into this
    instance's ``settings``. Own short session; best-effort — a DB blip leaves
    the current in-process rate untouched."""
    try:
        from app.db.session import AsyncSessionLocal

        async with AsyncSessionLocal() as db:
            row = await db.get(PlatformSetting, USD_TO_INR_KEY)
        target = DEFAULT_USD_TO_INR
        if row is not None:
            try:
                target = validate_fx(row.value)
            except ValueError:
                logger.warning("PLATFORM-FX: ignoring junk persisted rate %r", row.value)
        if float(settings.USD_TO_INR) != target:
            logger.info("PLATFORM-FX: applying USD→INR %s → %s", settings.USD_TO_INR, target)
            settings.USD_TO_INR = target
    except Exception:
        logger.debug("PLATFORM-FX: refresh skipped (DB unavailable?)", exc_info=True)


# ── background refresher (mirrors the llm_pool_keys refresher) ───────────────
_refresher_task: asyncio.Task | None = None


async def _refresh_loop() -> None:
    while True:
        await apply_persisted_fx()
        await asyncio.sleep(REFRESH_INTERVAL_SECONDS)


def start_platform_settings_refresher() -> None:
    """Idempotent. Wires into FastAPI's startup hook in :mod:`app.main` so a
    SuperAdmin rate change reaches every replica within a minute."""
    global _refresher_task
    if _refresher_task is not None:
        return
    try:
        _refresher_task = asyncio.get_event_loop().create_task(_refresh_loop())
    except Exception:
        logger.exception("PLATFORM-FX: failed to start refresher")


async def stop_platform_settings_refresher() -> None:
    global _refresher_task
    if _refresher_task is not None:
        _refresher_task.cancel()
        _refresher_task = None
