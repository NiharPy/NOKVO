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

import json
import logging
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.call_cost import CallCost
from app.models.minute_purchase import MinutePurchase

logger = logging.getLogger(__name__)


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


# The gate runs on EVERY dial refill (each ended call re-triggers the dialer) and
# on every campaign-ticker pass, and the two SUMs it needs scan the org's whole
# call_costs history — linear in lifetime calls. Cache the (purchased, consumed)
# pair per org for a few seconds so the steady-state gate is one Redis GET; the
# write paths (call-cost recorded, bundle purchased) invalidate so a just-emptied
# or just-topped-up balance is seen immediately. Staleness within the TTL only
# lets a call or two through at the zero boundary — the same tolerance the ledger
# already has ("balance can read slightly negative"). Best-effort: any Redis
# hiccup falls back to the direct SUMs.
_GATE_CACHE_TTL_S = 30


def _gate_cache_key(organization_id: uuid.UUID) -> str:
    return f"mbal:sums:{organization_id}"


async def invalidate_balance_cache(organization_id: uuid.UUID) -> None:
    """Drop the cached gate sums for an org. Called after a CallCost that depletes
    the balance is written and after a minute-bundle purchase is credited."""
    try:
        from app.services.agent_session_store import AgentSessionStore

        await AgentSessionStore.client().delete(_gate_cache_key(organization_id))
    except Exception:
        logger.debug("NOKVO-BALANCE: cache invalidation failed (best-effort)", exc_info=True)


async def _gate_sums(db: AsyncSession, organization_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    """(purchased, consumed) for the call-start gate, Redis-cached."""
    key = _gate_cache_key(organization_id)
    try:
        from app.services.agent_session_store import AgentSessionStore

        cached = await AgentSessionStore.client().get(key)
        if cached:
            data = json.loads(cached)
            return Decimal(data["p"]), Decimal(data["c"])
    except Exception:
        pass
    purchased = await purchased_rupees(db, organization_id)
    consumed = await consumed_rupees(db, organization_id)
    try:
        from app.services.agent_session_store import AgentSessionStore

        await AgentSessionStore.client().setex(
            key, _GATE_CACHE_TTL_S, json.dumps({"p": str(purchased), "c": str(consumed)})
        )
    except Exception:
        pass
    return purchased, consumed


async def has_balance(
    db: AsyncSession, organization_id: uuid.UUID, *, grandfather: bool = True
) -> bool:
    """The call-start gate (inbound + non-deterministic outbound). True when the
    org may place a call. Reads through the short-TTL gate cache (see above) so
    the per-call hot path never re-aggregates the org's full call history.

    GRANDFATHER / fail-open (``grandfather=True``, the default): an org that has
    NEVER bought a bundle (``purchased == 0``) is NOT gated — keeps every
    pre-billing-overhaul Nokvo One customer working on deploy and never wrongly
    blocks one whose onboarding purchase failed to record. Once an org has paid for
    ANY minutes it's on the prepaid model and is blocked at a non-positive balance.

    Pass ``grandfather=False`` for NOKVO APEX: it's a new, prepaid-only product with
    no legacy orgs to protect, so a zero balance MUST block — otherwise a comped /
    bypassed / credit-lost org (``purchased == 0``) would dial free and unmetered."""
    purchased, consumed = await _gate_sums(db, organization_id)
    if purchased <= 0:
        return grandfather
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


# ── NOKVO APEX: a MINUTES balance (separate product, separate model) ─────────
# APEX bills the customer on selected minutes but CREDITS floor(1.5×) minutes
# (``minute_purchases.minutes`` holds the CREDITED amount). Its balance is in
# MINUTES — credited minus consumed — where consumption is the org's total
# ``CallCost.billed_minutes`` (ceil sec/60, recorded per call at teardown). No
# rupee deduction; APEX calls just burn whole billed minutes.

async def apex_minutes_purchased(db: AsyncSession, organization_id: uuid.UUID) -> int:
    res = await db.execute(
        select(func.coalesce(func.sum(MinutePurchase.minutes), 0)).where(
            MinutePurchase.organization_id == organization_id
        )
    )
    return int(res.scalar_one() or 0)


async def apex_minutes_used(db: AsyncSession, organization_id: uuid.UUID) -> int:
    res = await db.execute(
        select(func.coalesce(func.sum(CallCost.billed_minutes), 0)).where(
            CallCost.organization_id == organization_id
        )
    )
    return int(res.scalar_one() or 0)


async def apex_minutes_balance(db: AsyncSession, organization_id: uuid.UUID) -> int:
    """Remaining APEX minutes (credited − used). May read 0 / slightly negative if
    the last call overran — callers treat ``<= 0`` as empty."""
    return (await apex_minutes_purchased(db, organization_id)) - (await apex_minutes_used(db, organization_id))


async def apex_has_minutes(db: AsyncSession, organization_id: uuid.UUID) -> bool:
    """The APEX dialer gate. True when the org may place a call.

    GRANDFATHER / fail-open: an org that never bought minutes (purchased==0) is
    NOT gated — keeps a brand-new org working if its onboarding credit hasn't
    landed yet. Once it has bought ANY minutes, it's blocked at a non-positive
    balance."""
    purchased = await apex_minutes_purchased(db, organization_id)
    if purchased <= 0:
        return True
    used = await apex_minutes_used(db, organization_id)
    return (purchased - used) > 0


async def apex_balance_summary(db: AsyncSession, organization_id: uuid.UUID) -> dict:
    """{credited_minutes, used_minutes, remaining_minutes} — APEX dashboard widget.

    LEGACY (minutes model). The live APEX wallet is now Call CREDITS — use
    ``apex_credit_summary``. Kept only so any old caller still resolves."""
    credited = await apex_minutes_purchased(db, organization_id)
    used = await apex_minutes_used(db, organization_id)
    return {
        "credited_minutes": credited,
        "used_minutes": used,
        "remaining_minutes": credited - used,
    }


# ── NOKVO APEX: the live CALL CREDITS wallet (rupee-valued, relabelled) ───────
# APEX now holds the same rupee ledger as Nokvo One (purchases.rupees −
# CallCost.prepaid_rupees, FIFO bundle by minutes) but the customer is CREDITED
# 1.5× the billed amount (``apex_wallet_credit``) and EVERY connected call —
# including the deterministic questionnaire — deducts at the APEX selling rate
# (``apex_call_cost``). The balance/gate reuse ``balance_rupees`` / ``has_balance``;
# this summary adds the dashboard's estimated-minutes readout.

async def apex_credit_summary(db: AsyncSession, organization_id: uuid.UUID) -> dict:
    """{credits_purchased, credits_used, credits_remaining,
    estimated_minutes_remaining, slab_rate} — the APEX Call Credits widget.

    ``estimated_minutes_remaining`` divides the remaining credits by the CURRENT
    bundle's slab rate (FIFO); it equals the advertised credited minutes at full
    balance, so the credits and minutes figures reconcile on screen. Floats for
    JSON — the UI formats (no ₹; the label is 'Call Credits')."""
    from app.services.minute_pricing import flat_rate_for_minutes

    purchased = await purchased_rupees(db, organization_id)
    consumed = await consumed_rupees(db, organization_id)
    remaining = purchased - consumed
    bundle = await current_bundle_minutes(db, organization_id)
    slab = flat_rate_for_minutes(bundle)
    est_minutes = int(remaining / slab) if slab > 0 and remaining > 0 else 0
    return {
        "credits_purchased": float(purchased),
        "credits_used": float(consumed),
        "credits_remaining": float(remaining),
        "estimated_minutes_remaining": est_minutes,
        "slab_rate": float(slab),
    }
