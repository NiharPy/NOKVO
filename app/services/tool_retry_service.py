"""Background retry queue for tool executions that failed in-call.

The voice pipeline already does one inline retry before telling the caller
"the team will call you back". Persistent failures (DB outage, third-party
API hiccup, schema validation race) land here. Anything that can run inside
a normal HTTP request handler can process them — there is no dedicated
worker process required to make this useful.

Lifecycle:

* ``enqueue()`` — called from the voice pipeline when both inline tries fail.
  Records the tool key, arguments, attempt count, and an exponential next-try
  timestamp. Status starts as ``pending``.
* ``process_due()`` — fetches everything whose ``next_attempt_at`` is in the
  past and re-runs the tool. On success the row moves to ``resolved``; on
  failure the attempt count + backoff are bumped, eventually landing in
  ``failed`` after too many tries.
* ``resolve()`` — manual override (operator dashboard) for cases the team
  worked around by hand.

The service is intentionally I/O thin so it can be called from a cron, an
admin button, or a request hook — wherever fits the deployment.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.pending_tool_retry import PendingToolRetry
from app.services.agent_spec import RETRY_POLICY


logger = logging.getLogger(__name__)

# Single source of truth lives in agent_spec — mirrored here so existing
# imports of MAX_ATTEMPTS keep working without a second indirection.
MAX_ATTEMPTS = RETRY_POLICY.max_attempts
_BACKOFF_MINUTES = RETRY_POLICY.backoff_minutes


def _next_attempt_after(attempt: int) -> datetime:
    minutes = _BACKOFF_MINUTES[min(attempt, len(_BACKOFF_MINUTES) - 1)]
    return datetime.now(timezone.utc) + timedelta(minutes=minutes)


class ToolRetryService:
    @staticmethod
    async def enqueue(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        tool_key: str,
        arguments: dict[str, Any],
        context: dict[str, Any] | None = None,
        last_error: str | None = None,
    ) -> PendingToolRetry:
        row = PendingToolRetry(
            id=uuid.uuid4(),
            organization_id=organization_id,
            tool_key=tool_key,
            arguments=arguments,
            context=context or {},
            status="pending",
            attempts=1,
            last_error=(last_error or "")[:1000] or None,
            next_attempt_at=_next_attempt_after(1),
        )
        db.add(row)
        try:
            await db.commit()
        except Exception:
            try:
                await db.rollback()
            except Exception:
                pass
        return row

    @staticmethod
    async def list_pending(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[PendingToolRetry]:
        stmt = (
            select(PendingToolRetry)
            .where(PendingToolRetry.status == "pending")
            .order_by(PendingToolRetry.created_at.asc())
            .limit(limit)
        )
        if organization_id is not None:
            stmt = stmt.where(PendingToolRetry.organization_id == organization_id)
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def process_due(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID | None = None,
        now: datetime | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch and re-run every pending retry whose backoff window has elapsed.
        Returns a small summary per row so the caller (cron, admin endpoint)
        can log what happened. Tool execution goes through the same path the
        live pipeline uses, so success has identical effects.
        """
        from app.services.predefined_tools_service import PredefinedToolsService, get_tool

        now = now or datetime.now(timezone.utc)
        stmt = (
            select(PendingToolRetry)
            .where(PendingToolRetry.status == "pending")
            .where(PendingToolRetry.next_attempt_at <= now)
            # SELECT FOR UPDATE SKIP LOCKED so two scheduler instances (or two
            # passes that overlap) can't pick up the same retry row. The lock
            # is released when this transaction commits below.
            .with_for_update(skip_locked=True)
        )
        if organization_id is not None:
            stmt = stmt.where(PendingToolRetry.organization_id == organization_id)
        res = await db.execute(stmt)
        rows = list(res.scalars().all())
        report: list[dict[str, Any]] = []
        for row in rows:
            tool = get_tool(row.tool_key)
            if tool is None:
                row.status = "failed"
                row.last_error = "tool_key no longer exists"
                row.resolved_at = now
                db.add(row)
                report.append({"id": str(row.id), "status": "failed", "reason": "tool_missing"})
                continue
            row.attempts = int(row.attempts or 0) + 1
            try:
                result = await PredefinedToolsService.execute(
                    db,
                    row.organization_id,
                    None,
                    tool,
                    dict(row.arguments or {}),
                    session_id=f"retry:{row.id}",
                )
                row.status = "resolved"
                row.resolved_at = now
                row.last_error = None
                db.add(row)
                report.append({"id": str(row.id), "status": "resolved", "result_id": result.get("id")})
            except Exception as exc:  # noqa: BLE001 — broad catch matches in-call path
                logger.warning("Pending retry %s failed (attempt %s): %s", row.id, row.attempts, exc)
                row.last_error = str(exc)[:1000]
                if row.attempts >= MAX_ATTEMPTS:
                    row.status = "failed"
                    row.resolved_at = now
                    report.append({"id": str(row.id), "status": "failed", "attempts": row.attempts})
                else:
                    row.next_attempt_at = _next_attempt_after(row.attempts)
                    report.append({"id": str(row.id), "status": "rescheduled", "attempts": row.attempts})
                db.add(row)
        try:
            await db.commit()
        except Exception:
            await db.rollback()
        return report

    @staticmethod
    async def resolve_manually(
        db: AsyncSession,
        *,
        retry_id: uuid.UUID,
        note: str | None = None,
    ) -> PendingToolRetry | None:
        res = await db.execute(
            select(PendingToolRetry).where(PendingToolRetry.id == retry_id)
        )
        row = res.scalars().first()
        if row is None:
            return None
        row.status = "manually_resolved"
        row.resolved_at = datetime.now(timezone.utc)
        if note:
            row.last_error = f"manual: {note}"[:1000]
        db.add(row)
        await db.commit()
        return row
