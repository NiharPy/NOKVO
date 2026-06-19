"""Cost-summary + notifications HTTP/WS endpoints.

Mounted at ``/api/nokvo-one``. Two narrow concerns share this file
because both surfaces are owned by the same dashboard widget set (the
top-of-page bell + the cost card next to it) and they both lean on
``RequireNokvoOneOrganization`` for auth.

Cost surface
------------
``GET /cost-summary`` returns three numbers — today, this-month,
all-time — plus the most recent calls. Aggregates sum the persisted
``call_costs.rupees`` column directly (NOT the duration) so the displayed
bill always reconciles against the ledger, even after a tariff change.

Notifications surface
---------------------
``GET    /notifications``               — newest-first list (paged)
``GET    /notifications/unread-count``  — bell badge number
``POST   /notifications/{id}/read``     — single-row mark-read
``POST   /notifications/read-all``      — bulk mark-read
``WS     /notifications/ws``            — live fanout (in-process bus)

The WebSocket route subscribes to the per-org bus exposed by
``NotificationService``; live events arrive as ``{"type":"notification",
"data":{...}}`` frames. Reconnect logic is on the client — on reconnect
it should pull ``GET /notifications`` to backfill anything missed.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.call_cost import CallCost
from app.models.organization_user import OrganizationUser
from app.services.call_cost_calculator import (
    RATE_TIERS,
    rupees_display,
    rupees_per_minute_for,
)
from app.services.notification_service import NotificationService, notification_bus


logger = logging.getLogger(__name__)
router = APIRouter()


def _viewer_dep():
    """Any Nokvo One org user may read the inbox + cost summary."""
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["active"],
        allowed_roles=["admin", "manager", "member"],
    )


def _admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["active"],
        allowed_roles=["admin", "manager"],
    )


# ── Cost summary ────────────────────────────────────────────────────────────


async def _sum_for_window(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    since: datetime | None,
) -> tuple[Decimal, Decimal, int]:
    """Return (total_rupees, total_seconds, call_count) over ``[since, now]``.

    ``since`` of ``None`` means "all time". We sum the persisted rupee
    column directly — never recompute from duration, because the row
    immortalises the rate that was in force when the call happened.
    """
    stmt = select(
        func.coalesce(func.sum(CallCost.rupees), 0),
        func.coalesce(func.sum(CallCost.duration_seconds), 0),
        func.count(CallCost.id),
    ).where(CallCost.organization_id == organization_id)
    if since is not None:
        stmt = stmt.where(CallCost.started_at >= since)
    res = await db.execute(stmt)
    rupees, seconds, count = res.first() or (0, 0, 0)
    return (
        Decimal(str(rupees or 0)),
        Decimal(str(seconds or 0)),
        int(count or 0),
    )


@router.get("/cost-summary")
async def cost_summary(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
    recent_limit: int = Query(default=10, ge=0, le=50),
):
    """Today / this-month / all-time call cost totals for the dashboard.

    ``today`` is bounded by the caller's UTC midnight — good enough for
    the in-product card; an IST cut-over would need the caller to send
    its TZ. ``this_month`` is the first of the current UTC month.

    Returns:
      ``{"rate": {...}, "today": {...}, "this_month": {...}, "all_time": {...}, "recent_calls": [...]}``

    Each total carries: ``rupees`` (ledger Decimal as string),
    ``rupees_display`` (2-dp string, what the UI shows),
    ``rupees_formatted`` (``₹X.XX``), ``seconds`` (Decimal as string),
    ``call_count`` (int).
    """
    org_id = user.organization_id
    now = datetime.now(timezone.utc)
    today_start = datetime.combine(now.date(), time.min, tzinfo=timezone.utc)
    month_start = today_start.replace(day=1)

    today = await _sum_for_window(db, org_id, since=today_start)
    this_month = await _sum_for_window(db, org_id, since=month_start)
    all_time = await _sum_for_window(db, org_id, since=None)

    def _format(bucket: tuple[Decimal, Decimal, int]) -> dict[str, Any]:
        rupees, seconds, count = bucket
        display = rupees_display(rupees)
        return {
            "rupees": str(rupees),
            "rupees_display": f"{display:.2f}",
            "rupees_formatted": f"₹{display:.2f}",
            "seconds": str(seconds),
            "call_count": count,
        }

    recent_calls: list[dict[str, Any]] = []
    if recent_limit > 0:
        stmt = (
            select(CallCost)
            .where(CallCost.organization_id == org_id)
            .order_by(CallCost.started_at.desc())
            .limit(recent_limit)
        )
        res = await db.execute(stmt)
        for row in res.scalars().all():
            display = rupees_display(Decimal(str(row.rupees or 0)))
            recent_calls.append(
                {
                    "id": str(row.id),
                    "call_id": row.call_id,
                    "kind": row.kind,
                    "campaign_id": str(row.campaign_id) if row.campaign_id else None,
                    "duration_seconds": str(row.duration_seconds),
                    "rupees": str(row.rupees),
                    "rupees_display": f"{display:.2f}",
                    "rupees_formatted": f"₹{display:.2f}",
                    "started_at": row.started_at.isoformat() if row.started_at else None,
                    "ended_at": row.ended_at.isoformat() if row.ended_at else None,
                }
            )

    # Current per-minute rate = the tier this month's minutes-to-date fall into.
    month_minutes = this_month[1] / Decimal("60")
    current_rate = rupees_per_minute_for(month_minutes)
    tier_ladder = [
        {"up_to_minutes": upper, "rupees_per_minute": str(rate)} for upper, rate in RATE_TIERS
    ]

    return {
        "rate": {
            "rupees_per_minute": str(current_rate),
            "rupees_per_second": str(current_rate / Decimal("60")),
            "rupees_per_second_display": f"{(current_rate / Decimal('60')):.2f}",
            "month_minutes_to_date": str(month_minutes),
            "tiers": tier_ladder,
        },
        "today": _format(today),
        "this_month": _format(this_month),
        "all_time": _format(all_time),
        "recent_calls": recent_calls,
    }


# ── Notifications inbox ─────────────────────────────────────────────────────


@router.get("/notifications")
async def list_notifications(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
    limit: int = Query(default=50, ge=1, le=200),
    unread_only: bool = Query(default=False),
):
    rows = await NotificationService.list_for_org(
        db,
        organization_id=user.organization_id,
        limit=limit,
        unread_only=unread_only,
    )
    return {"items": [NotificationService.serialise(r) for r in rows]}


@router.get("/notifications/unread-count")
async def unread_count(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    return {"unread": await NotificationService.unread_count(db, organization_id=user.organization_id)}


@router.post("/notifications/{notification_id}/read")
async def mark_notification_read(
    notification_id: str,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    try:
        nid = uuid.UUID(notification_id)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Malformed notification id")
    row = await NotificationService.mark_read(
        db,
        organization_id=user.organization_id,
        notification_id=nid,
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    return NotificationService.serialise(row)


@router.post("/notifications/read-all")
async def mark_all_notifications_read(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    marked = await NotificationService.mark_all_read(db, organization_id=user.organization_id)
    return {"marked_read": marked}


# ── Live notification fanout ────────────────────────────────────────────────


async def _ws_authenticate(websocket: WebSocket, db: AsyncSession) -> OrganizationUser | None:
    """Validate the JWT off the ``?token=`` query param.

    WebSockets can't ride the same Authorization header the REST routes
    use (browsers don't let you set headers on ``new WebSocket(...)``),
    so the frontend appends the token as a query string. We re-use the
    same decode + session-revoke checks the REST deps run.
    """
    from app.core import security
    from app.models.organization_session import OrganizationSession

    token = websocket.query_params.get("token")
    if not token:
        return None
    try:
        payload = security.decode_access_token(
            token, expected_tiers=[security.JWT_TIER_ORGANIZATION]
        )
    except Exception:
        return None
    if payload.get("principal_type") != "organization_user":
        return None
    user_id = payload.get("sub")
    organization_id = payload.get("organization_id")
    session_id = payload.get("sid")
    if not user_id or not organization_id:
        return None
    if session_id:
        sres = await db.execute(
            select(OrganizationSession).where(OrganizationSession.id == session_id)
        )
        session = sres.scalars().first()
        if not session or session.revoked_at is not None:
            return None
    res = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.id == user_id,
            OrganizationUser.organization_id == organization_id,
        )
    )
    user = res.scalars().first()
    if user is None or user.status == "disabled":
        return None
    return user


@router.websocket("/notifications/ws")
async def notifications_websocket(websocket: WebSocket):
    """Live notifications stream.

    Flow:
      1. Client connects with ``?token=<JWT>``.
      2. We subscribe an in-process queue to the org's notification bus.
      3. Every emitted notification is forwarded as a JSON frame; the
         caller is responsible for any client-side dedup.

    The bus is in-process; this works on the single-uvicorn deployment.
    When we shard, swap the bus impl for Redis pub/sub — this endpoint
    won't have to change.
    """
    async for db in deps.get_db():
        user = await _ws_authenticate(websocket, db)
        if user is None:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        bus = notification_bus()
        queue = bus.subscribe(user.organization_id)
        try:
            await websocket.send_json({"type": "ready"})
            while True:
                # We race a queue.get against a websocket.receive so a
                # client disconnect cancels the pull cleanly. The
                # receive coroutine raises when the socket closes.
                recv_task = asyncio.create_task(websocket.receive_text())
                queue_task = asyncio.create_task(queue.get())
                done, pending = await asyncio.wait(
                    {recv_task, queue_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if recv_task in done:
                    try:
                        recv_task.result()
                    except Exception:
                        return
                    # Drain the queue task we just cancelled so the
                    # message isn't lost — re-push if it had completed.
                    if queue_task in done:
                        try:
                            msg = queue_task.result()
                            await websocket.send_json(msg)
                        except Exception:
                            pass
                    continue
                if queue_task in done:
                    try:
                        msg = queue_task.result()
                    except Exception:
                        continue
                    try:
                        await websocket.send_json(msg)
                    except Exception:
                        return
        except Exception:
            logger.exception("NOKVO-NOTIF-WS: unexpected error")
        finally:
            try:
                bus.unsubscribe(user.organization_id, queue)
            except Exception:
                pass
        return
