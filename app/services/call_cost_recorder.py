"""Persist a CallCost row from a finished voice session.

The voice-pipeline ``run_session`` ends with a ``finally:`` block that
already knows the call lifecycle (started_at, ended_at, call_id, kind,
campaign_id). We hang a single helper off that point so the calculator
math + the DB write stay co-located.

Idempotency
-----------
``call_costs.call_id`` is UNIQUE. We INSERT ... ON CONFLICT DO NOTHING
so a retried session-close (e.g., the websocket disconnects then a
follow-up cleanup also runs) cannot double-bill. Returning the row is
informational — callers don't currently use it.

Failure mode
------------
The recorder runs **after** the call has ended. It must not raise into
the caller — a billing DB hiccup should not prevent a clean WS close.
We commit our own short transaction, rollback on error, and log.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, AsyncIterator

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_cost import CallCost
from app.services.call_cost_calculator import (
    minutes_for_seconds,
    rupees_for_minute_span,
    rupees_per_minute_for,
)
from app.services.call_usage import (
    CallUsage,
    begin_call_usage,
    compute_cogs_inr,
    end_call_usage,
    llm_cost_inr,
)


logger = logging.getLogger(__name__)


async def _month_to_date_billed_minutes(db: AsyncSession, org_uuid, now: datetime) -> int:
    """The org's BILLED minutes so far this calendar month (UTC) — used to place
    this call's minutes on the progressive tier ladder. Best-effort: 0 on any
    error (the call would then bill from tier 1, the safe under-charge)."""
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    try:
        res = await db.execute(
            select(func.coalesce(func.sum(CallCost.billed_minutes), 0)).where(
                CallCost.organization_id == org_uuid,
                CallCost.started_at >= month_start,
            )
        )
        return int(res.scalar() or 0)
    except Exception:
        logger.debug("NOKVO-COST: month-to-date billed-minutes query failed", exc_info=True)
        return 0


_CALL_KINDS = {"inbound", "outbound", "tester"}


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    if value is None:
        return None
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


async def record_call_cost(
    db: AsyncSession | None,
    *,
    organization_id: Any,
    tenant_id: str | None,
    call_id: str | None,
    started_at: datetime,
    ended_at: datetime,
    kind: str = "inbound",
    campaign_id: Any = None,
    trace_id: str | None = None,
    usage: CallUsage | None = None,
    deducts_prepaid: bool = False,
) -> CallCost | None:
    """Insert a single CallCost row for a completed session.

    ``usage`` carries the per-call metered vendor usage (LLM tokens, STT
    seconds, TTS characters). When supplied we price it into the per-component
    INR COGS columns (``cost_stt_inr`` … ``cost_total_inr``); when ``None``
    (e.g. the legacy agent path) those columns stay NULL and the row is
    "total-only" — ``rupees`` (the tenant's bill) is unaffected either way.

    Returns the persisted row, ``None`` when persistence was skipped
    (missing context, dedup hit, or rolled-back error). Never raises —
    failures are logged.
    """
    if db is None or not call_id or not tenant_id:
        return None
    org_uuid = _coerce_uuid(organization_id)
    if org_uuid is None:
        logger.warning(
            "NOKVO-COST: skipping call_cost — invalid organization_id (call_id=%s)",
            call_id,
        )
        return None
    if kind not in _CALL_KINDS:
        kind = "inbound"

    duration_seconds = (ended_at - started_at).total_seconds()
    if duration_seconds < 0:
        logger.warning(
            "NOKVO-COST: negative duration (%.3fs) for call_id=%s — clamping to 0",
            duration_seconds,
            call_id,
        )
        duration_seconds = 0.0

    # Whole-minute, progressive tiered billing. Every started minute is charged
    # in full (ceil), and each of this call's minutes is priced at the tier its
    # cumulative position in the month falls into — so a call that crosses a
    # tier mark (e.g. the 1,000th minute) bills the overflow at the next rate.
    try:
        billed_minutes = minutes_for_seconds(duration_seconds)
    except (TypeError, ValueError):
        logger.exception(
            "NOKVO-COST: cost calculator rejected duration %r for call_id=%s",
            duration_seconds,
            call_id,
        )
        return None
    prior_minutes = await _month_to_date_billed_minutes(db, org_uuid, datetime.now(timezone.utc))
    rupees = rupees_for_minute_span(prior_minutes, billed_minutes)
    # Persist the entry-tier per-minute rate (the rate in force when this call's
    # first billed minute started) for historical reproducibility; ``rupees`` is
    # the ledger of truth when a call straddles a tier boundary.
    entry_rate_per_minute = rupees_per_minute_for(prior_minutes)
    rate_per_second = entry_rate_per_minute / Decimal("60")
    billed_seconds = Decimal(str(duration_seconds))

    campaign_uuid = _coerce_uuid(campaign_id)

    # PREPAID usage deduction — two models, picked by the org's product:
    #   • Nokvo One: a CONNECTED inbound / non-deterministic-outbound call depletes
    #     the prepaid rupee balance by 0.6×started_min + rate×seconds (the rate set
    #     by the FIFO bundle). 0 for deterministic/bulk outbound (deducts_prepaid=
    #     False) and never-connected calls.
    #   • NOKVO APEX: deterministic IS the product — EVERY connected call depletes
    #     the Call Credits wallet at the SELLING rate (apex_call_cost: 1.5 once +
    #     (slab−1.5)/60×sec), regardless of the passed deducts_prepaid flag.
    # Both write the same CallCost.prepaid_rupees column. Best-effort: a balance
    # read hiccup must not block the ledger write (we record 0, call doesn't deplete).
    prepaid_rupees = Decimal("0")
    try:
        from app.models.organization import Organization

        _product_tier = (
            await db.execute(select(Organization.product_tier).where(Organization.id == org_uuid))
        ).scalar_one_or_none()
    except Exception:
        _product_tier = None
    _is_apex = (_product_tier or "") == "nokvo_apex"
    # APEX deducts for EVERY connected call — but never a browser tester session
    # (kind="tester"): the stream service deliberately passes deducts_prepaid=
    # False for those, and the tier override must not undo that.
    if duration_seconds > 0 and (deducts_prepaid or (_is_apex and kind != "tester")):
        try:
            from app.services.minute_balance_service import current_bundle_minutes

            bundle_minutes = await current_bundle_minutes(db, org_uuid)
            if _is_apex:
                from app.services.minute_pricing import apex_call_cost

                prepaid_rupees = apex_call_cost(duration_seconds, bundle_minutes)
            else:
                from app.services.minute_pricing import call_usage_cost

                prepaid_rupees = call_usage_cost(duration_seconds, bundle_minutes)
        except Exception:
            logger.exception(
                "NOKVO-COST: prepaid deduction failed (call_id=%s) — recording 0", call_id
            )
            prepaid_rupees = Decimal("0")

    # Per-component COGS (STT/LLM/TTS/Plivo) in INR, when usage was captured.
    # Best-effort: a pricing hiccup must not block the (revenue) ledger write.
    cogs_values: dict[str, Any] = {}
    if usage is not None:
        try:
            cogs = compute_cogs_inr(usage, billed_seconds)
            cogs_values = {
                "llm_input_tokens": cogs.llm_input_tokens,
                "llm_output_tokens": cogs.llm_output_tokens,
                "llm_cached_tokens": cogs.llm_cached_tokens,
                "stt_seconds": cogs.stt_seconds,
                "tts_characters": cogs.tts_characters,
                "cost_stt_inr": cogs.cost_stt_inr,
                "cost_llm_inr": cogs.cost_llm_inr,
                "cost_tts_inr": cogs.cost_tts_inr,
                "cost_telephony_inr": cogs.cost_telephony_inr,
                "cost_total_inr": cogs.cost_total_inr,
                "llm_requests": cogs.llm_requests,
                "tts_cache_hits": cogs.tts_cache_hits,
                "tts_cache_chars": cogs.tts_cache_chars,
            }
        except Exception:
            logger.exception(
                "NOKVO-COST: COGS breakdown failed (call_id=%s) — recording total-only",
                call_id,
            )
            cogs_values = {}

    # ON CONFLICT DO NOTHING — re-recording the same call_id is a no-op.
    stmt = (
        pg_insert(CallCost.__table__)
        .values(
            id=uuid.uuid4(),
            organization_id=org_uuid,
            tenant_id=tenant_id,
            call_id=call_id,
            kind=kind,
            campaign_id=campaign_uuid,
            duration_seconds=billed_seconds,
            billed_minutes=billed_minutes,
            rupees=rupees,
            prepaid_rupees=prepaid_rupees,
            rate_per_second=rate_per_second.quantize(Decimal("0.000001")),
            started_at=started_at,
            ended_at=ended_at,
            trace_id=trace_id,
            created_at=datetime.now(timezone.utc),
            **cogs_values,
        )
        .on_conflict_do_nothing(index_elements=["call_id"])
        .returning(CallCost.__table__)
    )
    try:
        result = await db.execute(stmt)
        await db.commit()
    except Exception:
        try:
            await db.rollback()
        except Exception:
            pass
        logger.exception(
            "NOKVO-COST: failed to persist call_cost (call_id=%s)", call_id
        )
        return None

    row = result.first()
    if row is None:
        # ON CONFLICT path — already recorded. Treat as success.
        return None
    if prepaid_rupees > 0:
        # This call depleted the prepaid balance — drop the cached gate sums so
        # the very next dial refill sees it (matters at the zero boundary).
        from app.services.minute_balance_service import invalidate_balance_cache

        await invalidate_balance_cache(org_uuid)
    return CallCost(**dict(row._mapping))


# ── Post-call LLM attribution ────────────────────────────────────────────────
# The lead scorer / call condensers run in background tasks spawned AFTER the
# session's CallCost row is committed and its usage sink torn down — their LLM
# tokens would otherwise vanish (added to a dead sink, or no sink at all).
# These helpers give each post-call task its own fresh sink and flush the
# accumulated usage back onto the already-written row as an atomic delta.


async def attribute_post_call_llm(
    call_id: str,
    usage: CallUsage,
    *,
    attempts: int = 4,
    retry_delay_s: float = 2.0,
) -> bool:
    """Fold post-call LLM usage into the call's committed CallCost row.

    ONE atomic UPDATE: COALESCE-increments the token/request counters and adds
    the priced delta (``llm_cost_inr`` — the same tariff ``compute_cogs_inr``
    uses, so the two can never diverge) to BOTH ``cost_llm_inr`` and
    ``cost_total_inr``. Guarded by ``cost_total_inr IS NOT NULL`` so a legacy
    "total-only" row is never half-instrumented. The retry is insurance for a
    recorder that hasn't committed yet (shouldn't happen — the row is written
    before the tasks spawn); rowcount 0 after the retries → warn and drop
    (matching the recorder's own best-effort posture). Never raises."""
    if not call_id or usage is None:
        return False
    if not (usage.llm_input_tokens or usage.llm_output_tokens or usage.llm_requests):
        return False
    from app.db.session import AsyncSessionLocal

    delta = llm_cost_inr(
        usage.llm_input_tokens, usage.llm_output_tokens, usage.llm_cached_tokens
    )
    stmt = (
        update(CallCost.__table__)
        .where(
            CallCost.call_id == str(call_id),
            CallCost.cost_total_inr.is_not(None),
        )
        .values(
            llm_input_tokens=func.coalesce(CallCost.llm_input_tokens, 0)
            + int(usage.llm_input_tokens),
            llm_output_tokens=func.coalesce(CallCost.llm_output_tokens, 0)
            + int(usage.llm_output_tokens),
            llm_cached_tokens=func.coalesce(CallCost.llm_cached_tokens, 0)
            + int(usage.llm_cached_tokens),
            llm_requests=func.coalesce(CallCost.llm_requests, 0)
            + int(usage.llm_requests),
            cost_llm_inr=func.coalesce(CallCost.cost_llm_inr, 0) + delta,
            cost_total_inr=func.coalesce(CallCost.cost_total_inr, 0) + delta,
        )
    )
    try:
        for attempt in range(max(1, int(attempts))):
            async with AsyncSessionLocal() as db:
                result = await db.execute(stmt)
                await db.commit()
                if (result.rowcount or 0) > 0:
                    logger.info(
                        "NOKVO-COST: post-call LLM attributed call_id=%s "
                        "(+%d in / +%d out tokens, +₹%s)",
                        call_id, usage.llm_input_tokens, usage.llm_output_tokens, delta,
                    )
                    return True
            if attempt + 1 < attempts:
                await asyncio.sleep(retry_delay_s)
        logger.warning(
            "NOKVO-COST: post-call LLM attribution dropped — no instrumented "
            "row for call_id=%s (+%d/%d tokens unbilled)",
            call_id, usage.llm_input_tokens, usage.llm_output_tokens,
        )
    except Exception:
        logger.exception(
            "NOKVO-COST: post-call LLM attribution failed (call_id=%s)", call_id
        )
    return False


@asynccontextmanager
async def post_call_llm_attribution(call_id: str) -> AsyncIterator[CallUsage]:
    """Give a post-call background task its own usage sink and flush it onto
    the call's CallCost row on exit.

    Two hard guarantees:
      * NO CONTEXTVAR BLEED — the reset token is consumed in the ``finally``,
        unconditionally, BEFORE anything else: even when the body runs
        synchronously in the caller's context (tests) or crashes, the sink can
        never leak past the ``with`` block.
      * FLUSH ON CRASH — the flush also lives in the ``finally``: a task that
        dies halfway through its LLM calls still attributes the tokens consumed
        before the crash (Azure bills for failed attempts too). The body's
        exception propagates (no swallow); the flush itself never raises."""
    usage, token = begin_call_usage()
    try:
        yield usage
    finally:
        end_call_usage(token)
        if usage.llm_input_tokens or usage.llm_output_tokens or usage.llm_requests:
            await attribute_post_call_llm(call_id, usage)
