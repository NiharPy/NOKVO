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

import logging
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_cost import CallCost
from app.services.call_cost_calculator import (
    CostBreakdown,
    rupees_per_second_for,
)
from app.services.call_usage import CallUsage, compute_cogs_inr


logger = logging.getLogger(__name__)


async def _month_to_date_minutes(db: AsyncSession, org_uuid, now: datetime) -> Decimal:
    """The org's billed MINUTES so far this calendar month (UTC) — used to pick
    the post-paid usage tier for this call. Best-effort: 0 on any error."""
    month_start = datetime(now.year, now.month, 1, tzinfo=timezone.utc)
    try:
        res = await db.execute(
            select(func.coalesce(func.sum(CallCost.duration_seconds), 0)).where(
                CallCost.organization_id == org_uuid,
                CallCost.started_at >= month_start,
            )
        )
        seconds = Decimal(str(res.scalar() or 0))
        return seconds / Decimal("60")
    except Exception:
        logger.debug("NOKVO-COST: month-to-date minutes query failed", exc_info=True)
        return Decimal("0")


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

    # Tiered post-paid rate: pick from this org's month-to-date minutes, compute
    # the cost at that rate, and persist the rate per row (so historical totals
    # stay reproducible across tier changes — like the old flat rate did).
    month_minutes = await _month_to_date_minutes(db, org_uuid, datetime.now(timezone.utc))
    rate_per_second = rupees_per_second_for(month_minutes)
    try:
        breakdown = CostBreakdown.for_duration_at_rate(duration_seconds, rate_per_second)
    except (TypeError, ValueError):
        logger.exception(
            "NOKVO-COST: cost calculator rejected duration %r for call_id=%s",
            duration_seconds,
            call_id,
        )
        return None

    campaign_uuid = _coerce_uuid(campaign_id)

    # Per-component COGS (STT/LLM/TTS/Plivo) in INR, when usage was captured.
    # Best-effort: a pricing hiccup must not block the (revenue) ledger write.
    cogs_values: dict[str, Any] = {}
    if usage is not None:
        try:
            cogs = compute_cogs_inr(usage, breakdown.seconds)
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
            duration_seconds=breakdown.seconds,
            rupees=breakdown.rupees,
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
    return CallCost(**dict(row._mapping))
