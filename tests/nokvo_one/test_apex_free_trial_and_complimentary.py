"""Free Trial charges nothing, and SuperAdmin can gift extra minutes at account creation.

Two things are locked in here:

* ``plan_public_view`` must report ``monthly_inr = 0`` for a non-chargeable plan. It used to
  compute rate x included_minutes regardless of ``chargeable``, so Free Trial advertised
  Rs.9,000/mo in the console and the APEX request form while billing nothing.
* Complimentary minutes are credited at the org's own rate with NO bonus, tagged with their
  own ledger source so gifted minutes stay separable from paid ones, and granted at most
  once per account (credit_apex_wallet dedupes non-payment grants per (org, source)).
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import text

from app.db import session as db_session
from app.models.organization import Organization
from app.services.apex_plans import APEX_PLANS, plan_public_view
from app.services.apex_plans import stamp_org_from_plan


async def _mk_org(db, plan_code="core") -> uuid.UUID:
    org = Organization(
        id=uuid.uuid4(), name="comp-test", region="southindia", environment="staging",
        status="pending_payment", product_tier="nokvo_apex", calling_enabled=True,
    )
    stamp_org_from_plan(org, plan_code)
    db.add(org)
    await db.commit()
    return org.id


async def _cleanup(org_id: uuid.UUID) -> None:
    async with db_session.AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM minute_purchases WHERE organization_id=:o"), {"o": org_id})
        await db.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": org_id})
        await db.commit()


def test_free_trial_reports_no_monthly_charge():
    view = plan_public_view(APEX_PLANS["free_trial"])
    assert view["chargeable"] is False
    # The bug: this was 9000.0 (1000 included minutes x Rs.9), shown as the trial's price.
    assert view["monthly_inr"] == 0.0


def test_chargeable_plans_keep_their_price():
    """Guard against over-fixing — zeroing must apply ONLY to non-chargeable plans."""
    core = plan_public_view(APEX_PLANS["core"])
    assert core["chargeable"] is True
    assert core["monthly_inr"] == 13499.0  # Rs.4,499 fee + 1000 x Rs.9

    # Enterprise has no catalog rate (per-deal), so it stays unpriced rather than 0.
    assert plan_public_view(APEX_PLANS["enterprise"])["monthly_inr"] is None


@pytest.mark.asyncio
async def test_complimentary_minutes_credit_at_rate_without_bonus():
    from app.services.apex_billing_service import credit_apex_wallet
    from app.services.minute_balance_service import purchased_rupees

    async with db_session.AsyncSessionLocal() as db:
        org_id = await _mk_org(db, "core")  # Rs.9/min
    try:
        async with db_session.AsyncSessionLocal() as db:
            ok = await credit_apex_wallet(
                db, organization_id=org_id, minutes=250, rate=Decimal("9"),
                bonus_pct=Decimal("0"), source="complimentary_grant",
            )
            assert ok is True
            # 250 x Rs.9, no bonus applied to a gift.
            assert await purchased_rupees(db, org_id) == Decimal("2250.0000")
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_complimentary_grant_is_not_double_credited():
    """A retried create must not gift the minutes twice — real wallet money."""
    from app.services.apex_billing_service import credit_apex_wallet
    from app.services.minute_balance_service import purchased_rupees

    async with db_session.AsyncSessionLocal() as db:
        org_id = await _mk_org(db, "core")
    try:
        async with db_session.AsyncSessionLocal() as db:
            a = await credit_apex_wallet(
                db, organization_id=org_id, minutes=100, rate=Decimal("9"),
                bonus_pct=Decimal("0"), source="complimentary_grant",
            )
            b = await credit_apex_wallet(
                db, organization_id=org_id, minutes=100, rate=Decimal("9"),
                bonus_pct=Decimal("0"), source="complimentary_grant",
            )
            assert (a, b) == (True, False)
            assert await purchased_rupees(db, org_id) == Decimal("900.0000")
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_complimentary_grant_is_separable_from_the_trial_grant():
    """Gifted minutes carry their own source, so a Free Trial org's ledger shows both."""
    from app.services.apex_billing_service import credit_apex_wallet
    from app.services.minute_balance_service import purchased_rupees

    async with db_session.AsyncSessionLocal() as db:
        org_id = await _mk_org(db, "free_trial")
    try:
        async with db_session.AsyncSessionLocal() as db:
            await credit_apex_wallet(
                db, organization_id=org_id, minutes=1000, rate=Decimal("9"),
                bonus_pct=Decimal("0"), source="trial_grant",
            )
            await credit_apex_wallet(
                db, organization_id=org_id, minutes=500, rate=Decimal("9"),
                bonus_pct=Decimal("0"), source="complimentary_grant",
            )
            # Both landed: 1000 + 500 minutes at Rs.9.
            assert await purchased_rupees(db, org_id) == Decimal("13500.0000")

        async with db_session.AsyncSessionLocal() as db:
            sources = (
                await db.execute(
                    text("SELECT source FROM minute_purchases WHERE organization_id=:o ORDER BY source"),
                    {"o": org_id},
                )
            ).scalars().all()
            assert sources == ["complimentary_grant", "trial_grant"]
    finally:
        await _cleanup(org_id)


def test_free_trial_concurrency_is_operator_adjustable():
    """Free Trial concurrency is settable like Enterprise's, and bounded."""
    from app.services.apex_plans import TRIAL_MAX_CONCURRENCY

    class _Org:
        pass

    default = _Org()
    stamp_org_from_plan(default, "free_trial")
    assert default.apex_concurrency == 1  # catalog default when not overridden

    bumped = _Org()
    stamp_org_from_plan(bumped, "free_trial", trial_concurrency=4)
    assert bumped.apex_concurrency == 4

    for bad in (0, TRIAL_MAX_CONCURRENCY + 1):
        with pytest.raises(ValueError):
            stamp_org_from_plan(_Org(), "free_trial", trial_concurrency=bad)


def test_trial_override_does_not_leak_into_paid_plans():
    """A paid plan's concurrency is what the customer bought — not operator-settable here."""
    class _Org:
        pass

    org = _Org()
    stamp_org_from_plan(org, "core", trial_concurrency=9)
    assert org.apex_concurrency == APEX_PLANS["core"].concurrency == 1


def test_create_request_caps_complimentary_minutes():
    """A slipped digit must not gift a fortune; negatives are rejected outright."""
    import pydantic

    from app.api.superadmin_tenant_provisioning import ApexCreateAccountRequest

    base = {"plan_code": "core", "company_name": "X", "admin_email": "a@b.com"}
    assert ApexCreateAccountRequest(**base).complimentary_minutes == 0
    assert ApexCreateAccountRequest(**base, complimentary_minutes=500).complimentary_minutes == 500

    with pytest.raises(pydantic.ValidationError):
        ApexCreateAccountRequest(**base, complimentary_minutes=-1)
    with pytest.raises(pydantic.ValidationError):
        ApexCreateAccountRequest(**base, complimentary_minutes=100_001)
