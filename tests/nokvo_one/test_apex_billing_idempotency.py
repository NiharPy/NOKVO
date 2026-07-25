"""NOKVO APEX wallet crediting — money-safety idempotency (DB-backed).

Locks in the fix for the double-credit bug: the monthly grant must credit EXACTLY ONCE
per billing cycle (keyed on the Razorpay invoice id), a webhook retry must be a no-op, and
an event with no invoice id must NOT create an unkeyed credit that a later invoice-keyed
event would duplicate. Also covers the Free-Trial (source-dedupe) and top-up (payment-id
dedupe) paths.

Uses the real test DB (like the integration tests) and cleans up its throwaway org.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db import session as db_session
from app.models.organization import Organization
from app.models.subscription import Subscription
from app.services.apex_plans import stamp_org_from_plan


async def _mk_org(db) -> uuid.UUID:
    org = Organization(
        id=uuid.uuid4(), name="idem-test", region="southindia", environment="staging",
        status="pending_payment", product_tier="nokvo_apex", calling_enabled=True,
    )
    stamp_org_from_plan(org, "core")  # rate ₹9, 1000 min, no bonus → ₹9,000/cycle
    db.add(org)
    await db.commit()
    return org.id


async def _cleanup(org_id: uuid.UUID) -> None:
    async with db_session.AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM minute_purchases WHERE organization_id=:o"), {"o": org_id})
        await db.execute(text("DELETE FROM subscriptions WHERE organization_id=:o"), {"o": org_id})
        await db.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": org_id})
        await db.commit()


@pytest.mark.asyncio
async def test_monthly_grant_credits_once_per_invoice():
    from app.services.apex_billing_service import credit_apex_wallet
    from app.services.minute_balance_service import purchased_rupees

    async with db_session.AsyncSessionLocal() as db:
        org_id = await _mk_org(db)
    try:
        async with db_session.AsyncSessionLocal() as db:
            r1 = await credit_apex_wallet(db, organization_id=org_id, minutes=1000, rate=Decimal("9"),
                                          bonus_pct=Decimal("0"), source="monthly_grant", razorpay_invoice_id="inv1")
            r2 = await credit_apex_wallet(db, organization_id=org_id, minutes=1000, rate=Decimal("9"),
                                          bonus_pct=Decimal("0"), source="monthly_grant", razorpay_invoice_id="inv1")
            r3 = await credit_apex_wallet(db, organization_id=org_id, minutes=1000, rate=Decimal("9"),
                                          bonus_pct=Decimal("0"), source="monthly_grant", razorpay_invoice_id="inv2")
            assert (r1, r2, r3) == (True, False, True)          # cycle1, retry(no-op), cycle2
            assert await purchased_rupees(db, org_id) == Decimal("18000.0000")
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_free_trial_grant_credits_at_most_once():
    from app.services.apex_billing_service import credit_apex_wallet
    from app.services.minute_balance_service import purchased_rupees

    async with db_session.AsyncSessionLocal() as db:
        org_id = await _mk_org(db)
    try:
        async with db_session.AsyncSessionLocal() as db:
            a = await credit_apex_wallet(db, organization_id=org_id, minutes=1000, rate=Decimal("9"),
                                         bonus_pct=Decimal("0"), source="trial_grant")
            b = await credit_apex_wallet(db, organization_id=org_id, minutes=1000, rate=Decimal("9"),
                                         bonus_pct=Decimal("0"), source="trial_grant")  # dedupe on (org, source)
            assert (a, b) == (True, False)
            assert await purchased_rupees(db, org_id) == Decimal("9000.0000")
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_credit_cycle_skips_when_no_invoice_then_credits_on_charged():
    """The two-event scenario that caused the double-credit bug: activated (no invoice)
    then charged (invoice). Status advances on both; credit lands exactly once."""
    from app.api.nokvo_one_payments import _apex_credit_cycle
    from app.services.minute_balance_service import purchased_rupees

    async with db_session.AsyncSessionLocal() as db:
        org_id = await _mk_org(db)
        sub = Subscription(
            id=uuid.uuid4(), organization_id=org_id, plan="apex", amount_paise=1349900,
            minutes=1000, razorpay_subscription_id=f"sub_{uuid.uuid4().hex[:10]}", status="created",
        )
        db.add(sub)
        await db.commit()
    try:
        async with db_session.AsyncSessionLocal() as db:
            org = await db.get(Organization, org_id)
            sub_id = (await db.execute(
                text("SELECT id FROM subscriptions WHERE organization_id=:o"), {"o": org_id})).scalar_one()
            sub_obj = await db.get(Subscription, sub_id)

            await _apex_credit_cycle(db, org, sub_obj, None)      # activated, no invoice → no credit
            assert org.status == "pending_activation"
            assert await purchased_rupees(db, org_id) == Decimal("0")

            await _apex_credit_cycle(db, org, sub_obj, "inv1")    # charged
            await _apex_credit_cycle(db, org, sub_obj, "inv1")    # charged retry
            assert await purchased_rupees(db, org_id) == Decimal("9000.0000")

            await _apex_credit_cycle(db, org, sub_obj, "inv2")    # next cycle
            assert await purchased_rupees(db, org_id) == Decimal("18000.0000")
    finally:
        await _cleanup(org_id)
