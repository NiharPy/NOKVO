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

from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_cost import CallCost
from app.services.call_cost_calculator import (
    RUPEES_PER_SECOND,
    CostBreakdown,
)


logger = logging.getLogger(__name__)


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
) -> CallCost | None:
    """Insert a single CallCost row for a completed session.

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

    try:
        breakdown = CostBreakdown.for_duration(duration_seconds)
    except (TypeError, ValueError):
        logger.exception(
            "NOKVO-COST: cost calculator rejected duration %r for call_id=%s",
            duration_seconds,
            call_id,
        )
        return None

    campaign_uuid = _coerce_uuid(campaign_id)

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
            rate_per_second=RUPEES_PER_SECOND.quantize(Decimal("0.000001")),
            started_at=started_at,
            ended_at=ended_at,
            trace_id=trace_id,
            created_at=datetime.now(timezone.utc),
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
