"""Prepaid RUPEE balance + FIFO bundle selection.

A customer pre-pays for voice minutes; the rupees they paid become a balance that
connected inbound / non-deterministic-outbound calls deplete (each call costs
``0.6 + rate×seconds`` — see :mod:`app.services.minute_pricing`). Deterministic /
bulk-questionnaire outbound calls do NOT deplete it.

    balance_rupees = SUM(minute_purchases.rupees) − SUM(call_costs.prepaid_rupees)

Each purchase is a BUNDLE bought at a flat bracket rate (by its minute size). The
per-second talk rate for a call is set by the bundle the rupees are currently
being spent from, **FIFO** — oldest bundle first. ``current_bundle_minutes``
returns the size of that bundle so the recorder can pick the right rate.

Consumption reuses the per-call ``CallCost.prepaid_rupees`` already written at
teardown — no separate decrement path.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_cost import CallCost
from app.models.minute_purchase import MinutePurchase


async def purchased_rupees(db: AsyncSession, organization_id: uuid.UUID) -> Decimal:
    res = await db.execute(
        select(func.coalesce(func.sum(MinutePurchase.rupees), 0)).where(
            MinutePurchase.organization_id == organization_id
        )
    )
    return Decimal(str(res.scalar_one() or 0))


async def consumed_rupees(db: AsyncSession, organization_id: uuid.UUID) -> Decimal:
    res = await db.execute(
        select(func.coalesce(func.sum(CallCost.prepaid_rupees), 0)).where(
            CallCost.organization_id == organization_id
        )
    )
    return Decimal(str(res.scalar_one() or 0))


async def balance_rupees(db: AsyncSession, organization_id: uuid.UUID) -> Decimal:
    """Remaining prepaid rupees (purchased − consumed). Can read slightly negative
    if the last call overran the balance — callers treat ``<= 0`` as empty."""
    return (await purchased_rupees(db, organization_id)) - (await consumed_rupees(db, organization_id))


async def has_balance(db: AsyncSession, organization_id: uuid.UUID) -> bool:
    """The call-start gate (inbound + non-deterministic outbound). True when the
    org may place a call.

    GRANDFATHER / fail-open: an org that has NEVER bought a bundle
    (``purchased == 0``) is NOT gated — keeps every pre-billing-overhaul customer
    working on deploy and never wrongly blocks a brand-new org whose onboarding
    purchase failed to record. Once an org has paid for ANY minutes it's on the
    prepaid model and is blocked at a non-positive balance."""
    purchased = await purchased_rupees(db, organization_id)
    if purchased <= 0:
        return True
    consumed = await consumed_rupees(db, organization_id)
    return (purchased - consumed) > 0


async def current_bundle_minutes(db: AsyncSession, organization_id: uuid.UUID) -> int:
    """Size (in minutes) of the prepaid BUNDLE the next rupee will be spent from,
    FIFO (oldest purchase whose rupees aren't yet fully consumed). Drives the
    per-second usage rate for the next call. Returns the most-recent bundle's size
    when everything is spent (so an about-to-be-blocked call still prices), and a
    safe default of 1000 (the ₹10 entry rate) when there are no purchases at all
    (a grandfathered/legacy call that somehow deducts)."""
    consumed = await consumed_rupees(db, organization_id)
    rows = (
        await db.execute(
            select(MinutePurchase.minutes, MinutePurchase.rupees)
            .where(MinutePurchase.organization_id == organization_id)
            .order_by(MinutePurchase.created_at.asc(), MinutePurchase.id.asc())
        )
    ).all()
    if not rows:
        return 1000
    running = Decimal("0")
    last_minutes = int(rows[-1][0] or 0) or 1000
    for minutes, rupees in rows:
        running += Decimal(str(rupees or 0))
        # The first bundle whose cumulative rupees exceed what's been consumed is
        # the one the next rupee comes from.
        if running > consumed:
            return int(minutes or 0) or 1000
    return last_minutes


async def balance_summary(db: AsyncSession, organization_id: uuid.UUID) -> dict:
    """{purchased_rupees, consumed_rupees, remaining_rupees} — for the dashboard
    balance widget. Floats for JSON (2-dp display is the UI's job)."""
    purchased = await purchased_rupees(db, organization_id)
    consumed = await consumed_rupees(db, organization_id)
    return {
        "purchased_rupees": float(purchased),
        "consumed_rupees": float(consumed),
        "remaining_rupees": float(purchased - consumed),
    }
