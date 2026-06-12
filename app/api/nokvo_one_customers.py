"""Customer base API.

The customer_base table records every inbound caller (deduplicated per
tenant); the endpoints here are business-type-generic, but the UI only
exposes them for clinic organizations — clinics have no Leads tab, and
follow-ups happen exclusively on the admin's command from this surface:

  * ``POST /{id}/call-now``  — place the follow-up immediately (clamped to
    the legal call window; off-hours requests are scheduled at window start
    and the effective time is returned so the UI can say so).
  * ``POST /{id}/followups`` — schedule for a chosen date/time.

Both require a typed purpose note: the outbound agent opens the call with
it (REASON FOR THIS CALL), backed by the customer's last-call summary and
the clinic services catalog. There is no automated retry for customers.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func as sa_func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.customer_base import CustomerBase
from app.models.lead_followup_schedule import (
    FollowupReason,
    FollowupStatus,
    LeadFollowupSchedule,
)
from app.models.nokvo_one_tool_record import NokvoOneToolRecord
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources


router = APIRouter()

_NOTE_MAX_CHARS = 500


def _admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["pending_approval", "active"],
        allowed_roles=["admin", "manager"],
    )


async def _tenant_for_user(db: AsyncSession, user: OrganizationUser) -> TenantResources:
    res = await db.execute(
        select(TenantResources).where(TenantResources.organization_id == user.organization_id)
    )
    tr = res.scalars().first()
    if not tr:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")
    return tr


async def _customer_for_user(
    db: AsyncSession, user: OrganizationUser, customer_id: str
) -> tuple[CustomerBase, TenantResources]:
    try:
        cid = uuid.UUID(str(customer_id))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Invalid customer id")
    tr = await _tenant_for_user(db, user)
    res = await db.execute(
        select(CustomerBase).where(
            CustomerBase.id == cid, CustomerBase.tenant_id == tr.tenant_id
        )
    )
    customer = res.scalars().first()
    if customer is None:
        raise HTTPException(status_code=404, detail="Customer not found")
    return customer, tr


def _customer_response(row: CustomerBase, *, pending_followups: int = 0) -> dict:
    return {
        "id": str(row.id),
        "phone_e164": row.phone_e164,
        "name": row.name,
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_call_at": row.last_call_at.isoformat() if row.last_call_at else None,
        "call_count": int(row.call_count or 0),
        "last_call_id": row.last_call_id,
        "last_call_summary": row.last_call_summary,
        "opt_out": bool(row.opt_out),
        "pending_followups": pending_followups,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _followup_response(row: LeadFollowupSchedule) -> dict:
    return {
        "id": str(row.id),
        "customer_id": str(row.customer_id) if row.customer_id else None,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        "reason": row.reason.value if row.reason else None,
        "note": row.note,
        "status": row.status.value if row.status else None,
        "placed_call_id": row.placed_call_id,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _require_note(body: dict) -> str:
    note = str(body.get("note") or "").strip()
    if not note:
        raise HTTPException(status_code=400, detail="A purpose note is required")
    return note[:_NOTE_MAX_CHARS]


@router.get("")
async def list_customers(
    q: str | None = None,
    limit: int = 50,
    offset: int = 0,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Paginated customer list, most-recent caller first, with a pending-
    follow-up count per row (drives the chip + disables double-scheduling)."""
    tr = await _tenant_for_user(db, user)
    limit = max(1, min(200, int(limit)))
    offset = max(0, int(offset))

    stmt = select(CustomerBase).where(CustomerBase.tenant_id == tr.tenant_id)
    count_stmt = select(sa_func.count(CustomerBase.id)).where(
        CustomerBase.tenant_id == tr.tenant_id
    )
    if q and q.strip():
        needle = f"%{q.strip()}%"
        cond = or_(CustomerBase.phone_e164.ilike(needle), CustomerBase.name.ilike(needle))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)
    total = int((await db.execute(count_stmt)).scalar() or 0)
    rows = (
        (
            await db.execute(
                stmt.order_by(CustomerBase.last_call_at.desc().nullslast())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )

    pending_by_customer: dict[uuid.UUID, int] = {}
    if rows:
        pending_rows = (
            await db.execute(
                select(LeadFollowupSchedule.customer_id, sa_func.count(LeadFollowupSchedule.id))
                .where(LeadFollowupSchedule.customer_id.in_([r.id for r in rows]))
                .where(
                    LeadFollowupSchedule.status.in_(
                        [FollowupStatus.pending, FollowupStatus.in_flight]
                    )
                )
                .group_by(LeadFollowupSchedule.customer_id)
            )
        ).all()
        pending_by_customer = {cid: int(n) for cid, n in pending_rows}

    return {
        "total": total,
        "customers": [
            _customer_response(r, pending_followups=pending_by_customer.get(r.id, 0))
            for r in rows
        ],
    }


@router.get("/{customer_id}")
async def get_customer(
    customer_id: str,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Customer detail: profile + appointment history + follow-up history.

    Appointments match on ``data.related_customer_id`` (written by the booking
    macro) with a phone-match fallback for appointments booked before that
    link existed.
    """
    customer, _tr = await _customer_for_user(db, user, customer_id)

    appt_rows = (
        (
            await db.execute(
                select(NokvoOneToolRecord)
                .where(NokvoOneToolRecord.organization_id == user.organization_id)
                .where(NokvoOneToolRecord.record_type == "appointment")
                .where(
                    or_(
                        NokvoOneToolRecord.data["related_customer_id"].astext
                        == str(customer.id),
                        NokvoOneToolRecord.contact_phone == customer.phone_e164,
                    )
                )
                .order_by(NokvoOneToolRecord.created_at.desc())
                .limit(50)
            )
        )
        .scalars()
        .all()
    )
    followup_rows = (
        (
            await db.execute(
                select(LeadFollowupSchedule)
                .where(LeadFollowupSchedule.customer_id == customer.id)
                .order_by(LeadFollowupSchedule.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    pending = sum(
        1
        for r in followup_rows
        if r.status in (FollowupStatus.pending, FollowupStatus.in_flight)
    )
    return {
        **_customer_response(customer, pending_followups=pending),
        "appointments": [
            {
                "id": str(rec.id),
                "status": rec.status,
                "data": rec.data or {},
                "created_at": rec.created_at.isoformat() if rec.created_at else None,
            }
            for rec in appt_rows
        ],
        "followups": [_followup_response(r) for r in followup_rows],
    }


@router.patch("/{customer_id}")
async def update_customer(
    customer_id: str,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Admin edits. This is the ONLY path allowed to overwrite an existing
    name (call data is fill-only because STT mishears names)."""
    customer, _tr = await _customer_for_user(db, user, customer_id)
    body = await request.json()
    if "name" in body:
        name = str(body.get("name") or "").strip()[:120]
        customer.name = name or None
    if "opt_out" in body:
        customer.opt_out = bool(body.get("opt_out"))
    db.add(customer)
    await db.commit()
    await db.refresh(customer)
    return _customer_response(customer)


@router.post("/{customer_id}/call-now")
async def call_customer_now(
    customer_id: str,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Place a follow-up call to this customer immediately.

    Body: {note} (required — the call's purpose, rendered as REASON FOR THIS
    CALL in the outbound prompt).

    Implementation: insert a manual follow-up row at ``now`` and dispatch it
    inline through the scheduler's own ``_dispatch_one`` (reuses the entire
    placement + webhook-close loop). The dispatcher clamps into the legal
    call window, so an off-hours "Call now" is scheduled at the next window
    start — the response carries the effective ``scheduled_at`` and a
    ``deferred`` flag so the UI can say "scheduled for 9:00 AM" instead of
    appearing to fail.
    """
    customer, tr = await _customer_for_user(db, user, customer_id)
    if customer.opt_out:
        raise HTTPException(status_code=409, detail="Customer has opted out of calls")
    body = await request.json()
    note = _require_note(body)

    row = LeadFollowupSchedule(
        id=uuid.uuid4(),
        tenant_id=tr.tenant_id,
        lead_id=None,
        customer_id=customer.id,
        campaign_id=None,
        source_call_id=customer.last_call_id,
        scheduled_at=datetime.now(timezone.utc),
        reason=FollowupReason.manual,
        note=note,
        attempts=0,
        status=FollowupStatus.pending,
    )
    db.add(row)
    await db.commit()

    from app.services.followup_scheduler import _dispatch_one

    try:
        await _dispatch_one(row.id)
    except Exception:
        # The row stays pending; the scheduler loop retries on its next tick.
        pass

    await db.refresh(row)
    deferred = (
        row.status == FollowupStatus.pending
        and row.scheduled_at is not None
        and row.scheduled_at > datetime.now(timezone.utc)
    )
    return {**_followup_response(row), "deferred": deferred}


@router.post("/{customer_id}/followups")
async def schedule_customer_followup(
    customer_id: str,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Schedule a follow-up call at an admin-chosen time.

    Body: {scheduled_at (ISO-8601), note (required)}. The time is clamped
    into the default legal call window before insert.
    """
    customer, tr = await _customer_for_user(db, user, customer_id)
    if customer.opt_out:
        raise HTTPException(status_code=409, detail="Customer has opted out of calls")
    body = await request.json()
    note = _require_note(body)
    iso = body.get("scheduled_at")
    if not iso:
        raise HTTPException(status_code=400, detail="scheduled_at required")
    try:
        when = datetime.fromisoformat(str(iso))
    except ValueError:
        raise HTTPException(status_code=400, detail="scheduled_at must be ISO-8601")
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)

    from app.services.followup_scheduler_service import (
        clamp_to_call_window,
        effective_followup_rules,
    )

    when = clamp_to_call_window(when, effective_followup_rules(None))

    row = LeadFollowupSchedule(
        id=uuid.uuid4(),
        tenant_id=tr.tenant_id,
        lead_id=None,
        customer_id=customer.id,
        campaign_id=None,
        source_call_id=customer.last_call_id,
        scheduled_at=when,
        reason=FollowupReason.manual,
        note=note,
        attempts=0,
        status=FollowupStatus.pending,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _followup_response(row)


@router.delete("/{customer_id}/followups")
async def cancel_customer_followups(
    customer_id: str,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Cancel all pending follow-ups for this customer."""
    customer, _tr = await _customer_for_user(db, user, customer_id)
    rows = (
        (
            await db.execute(
                select(LeadFollowupSchedule)
                .where(LeadFollowupSchedule.customer_id == customer.id)
                .where(LeadFollowupSchedule.status == FollowupStatus.pending)
            )
        )
        .scalars()
        .all()
    )
    for row in rows:
        row.status = FollowupStatus.cancelled
        row.cancelled_reason = "admin"
        db.add(row)
    await db.commit()
    return {"cancelled": len(rows)}
