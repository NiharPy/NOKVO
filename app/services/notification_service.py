"""Notification emission + live fanout.

Responsibilities
----------------
1. Persist a notification row so the inbox survives reconnects and the
   user sees historical events on next login.
2. Push it live to any WebSocket subscribers for that organization so
   the bell in the header updates without polling.
3. Suppress duplicate fires within a short window — a flapping
   integration health check shouldn't carpet-bomb the inbox.

Design notes
------------
- The pubsub layer is *in-process*. The deployment is single-uvicorn
  today (same assumption as the retry / lead-sync schedulers); when we
  shard, swap this for Redis pub/sub keyed by ``organization_id``.
- Persistence happens **before** fanout. If the DB write fails the
  caller learns about it; if the WS push fails, we still have the row
  and the next page load will pick it up.
- ``emit`` is fire-and-forget from the caller's perspective: any error
  is logged but does not raise, so a notification problem can't take
  down the call path that triggered it.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import (
    NOTIFICATION_TYPES,
    Notification,
    SEVERITIES,
    SEVERITY_P2,
)


logger = logging.getLogger(__name__)


# ── In-process WebSocket fanout ─────────────────────────────────────────────


class _NotificationBus:
    """Per-organization pub/sub for live notifications.

    Subscribers register an asyncio.Queue keyed by their organization_id;
    publishers drop the serialised notification into every matching queue.
    The queues are bounded so a slow subscriber can't grow memory without
    limit — when a queue fills, we drop the oldest item and log so we
    notice. The user can refresh to re-fetch from the DB.
    """

    _QUEUE_MAX = 64

    def __init__(self) -> None:
        # organization_id (str) → set[asyncio.Queue]. We use str keys so
        # callers can pass UUID or str interchangeably without a cast.
        self._subscribers: dict[str, set[asyncio.Queue]] = {}

    def subscribe(self, organization_id: str | uuid.UUID) -> asyncio.Queue:
        key = str(organization_id)
        queue: asyncio.Queue = asyncio.Queue(maxsize=self._QUEUE_MAX)
        self._subscribers.setdefault(key, set()).add(queue)
        return queue

    def unsubscribe(self, organization_id: str | uuid.UUID, queue: asyncio.Queue) -> None:
        key = str(organization_id)
        bucket = self._subscribers.get(key)
        if not bucket:
            return
        bucket.discard(queue)
        if not bucket:
            self._subscribers.pop(key, None)

    def publish(self, organization_id: str | uuid.UUID, message: dict[str, Any]) -> None:
        key = str(organization_id)
        bucket = self._subscribers.get(key)
        if not bucket:
            return
        for queue in list(bucket):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                # Drop the oldest, push the new one — better to lose the
                # stalest item than silently drop the freshest event.
                try:
                    queue.get_nowait()
                    queue.put_nowait(message)
                except Exception:
                    logger.warning(
                        "NOKVO-NOTIF: dropped notification for org %s (queue full)", key
                    )


_bus = _NotificationBus()


def notification_bus() -> _NotificationBus:
    """Return the process-wide notification bus.

    Exposed as a function (not the module-level singleton directly) so
    tests can monkeypatch it cleanly.
    """
    return _bus


# ── Service ─────────────────────────────────────────────────────────────────


class NotificationService:
    """Emit and read notifications for an organization.

    All emit helpers take an ``AsyncSession`` and *commit only the
    notification row*. We do NOT commit the caller's outer transaction —
    if the call path needs the notification + other writes atomic, it
    flushes both and the caller commits.
    """

    # How long the same (org, type, dedup_key) combination is suppressed
    # before re-firing. Generous default so a flapping integration check
    # at "every 5 minutes" doesn't notify five times in five minutes.
    DEFAULT_DEDUP_WINDOW = timedelta(minutes=15)

    @staticmethod
    def _serialise(notification: Notification) -> dict[str, Any]:
        return {
            "id": str(notification.id),
            "organization_id": str(notification.organization_id),
            "tenant_id": notification.tenant_id,
            "type": notification.type,
            "severity": notification.severity,
            "title": notification.title,
            "body": notification.body,
            "payload": notification.payload or {},
            "dedup_key": notification.dedup_key,
            "read_at": notification.read_at.isoformat() if notification.read_at else None,
            "created_at": notification.created_at.isoformat() if notification.created_at else None,
        }

    @staticmethod
    async def _is_duplicate(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        type_: str,
        dedup_key: str,
        window: timedelta,
    ) -> bool:
        cutoff = datetime.now(timezone.utc) - window
        stmt = (
            select(Notification.id)
            .where(Notification.organization_id == organization_id)
            .where(Notification.type == type_)
            .where(Notification.dedup_key == dedup_key)
            .where(Notification.created_at >= cutoff)
            .limit(1)
        )
        res = await db.execute(stmt)
        return res.first() is not None

    @staticmethod
    async def emit(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        tenant_id: str,
        type: str,
        title: str,
        body: str | None = None,
        severity: str = SEVERITY_P2,
        payload: dict[str, Any] | None = None,
        dedup_key: str | None = None,
        dedup_window: timedelta | None = None,
    ) -> Notification | None:
        """Create + fanout one notification.

        Returns the row on success, ``None`` when the dedup window
        suppressed it. Raises only on a programmer error
        (unknown ``type`` / ``severity``); transient DB / WS errors are
        logged and swallowed so the trigger path stays unaffected.
        """
        if type not in NOTIFICATION_TYPES:
            raise ValueError(f"unknown notification type: {type!r}")
        if severity not in SEVERITIES:
            raise ValueError(f"unknown severity: {severity!r}")

        if dedup_key:
            window = dedup_window or NotificationService.DEFAULT_DEDUP_WINDOW
            try:
                if await NotificationService._is_duplicate(
                    db,
                    organization_id=organization_id,
                    type_=type,
                    dedup_key=dedup_key,
                    window=window,
                ):
                    return None
            except Exception:
                # Dedup is best-effort. If the lookup itself failed we'd
                # rather emit a duplicate than swallow a hot lead.
                logger.warning("NOKVO-NOTIF: dedup lookup failed", exc_info=True)

        row = Notification(
            id=uuid.uuid4(),
            organization_id=organization_id,
            tenant_id=tenant_id,
            type=type,
            severity=severity,
            title=title[:500],
            body=(body or None) if body is None else body[:4000],
            payload=payload or {},
            dedup_key=dedup_key,
            created_at=datetime.now(timezone.utc),
        )
        try:
            db.add(row)
            await db.flush()
            await db.commit()
        except Exception:
            await db.rollback()
            logger.exception("NOKVO-NOTIF: failed to persist notification")
            return None

        try:
            notification_bus().publish(
                organization_id,
                {"type": "notification", "data": NotificationService._serialise(row)},
            )
        except Exception:
            logger.exception("NOKVO-NOTIF: failed to publish notification to bus")
        return row

    @staticmethod
    async def list_for_org(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        limit: int = 50,
        unread_only: bool = False,
    ) -> list[Notification]:
        stmt = (
            select(Notification)
            .where(Notification.organization_id == organization_id)
            .order_by(Notification.created_at.desc())
            .limit(max(1, min(int(limit), 200)))
        )
        if unread_only:
            stmt = stmt.where(Notification.read_at.is_(None))
        res = await db.execute(stmt)
        return list(res.scalars().all())

    @staticmethod
    async def unread_count(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
    ) -> int:
        from sqlalchemy import func

        stmt = (
            select(func.count(Notification.id))
            .where(Notification.organization_id == organization_id)
            .where(Notification.read_at.is_(None))
        )
        res = await db.execute(stmt)
        return int(res.scalar_one() or 0)

    @staticmethod
    async def mark_read(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
        notification_id: uuid.UUID,
    ) -> Notification | None:
        stmt = (
            select(Notification)
            .where(Notification.id == notification_id)
            .where(Notification.organization_id == organization_id)
        )
        res = await db.execute(stmt)
        row = res.scalars().first()
        if row is None:
            return None
        if row.read_at is None:
            row.read_at = datetime.now(timezone.utc)
            db.add(row)
            await db.commit()
        return row

    @staticmethod
    async def mark_all_read(
        db: AsyncSession,
        *,
        organization_id: uuid.UUID,
    ) -> int:
        """Bulk-mark every unread row for the org. Returns the count."""
        from sqlalchemy import update

        now = datetime.now(timezone.utc)
        stmt = (
            update(Notification)
            .where(Notification.organization_id == organization_id)
            .where(Notification.read_at.is_(None))
            .values(read_at=now)
        )
        res = await db.execute(stmt)
        await db.commit()
        return int(res.rowcount or 0)

    @staticmethod
    def serialise(notification: Notification) -> dict[str, Any]:
        """Public alias of :meth:`_serialise` for API responses."""
        return NotificationService._serialise(notification)
