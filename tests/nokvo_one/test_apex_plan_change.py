"""NOKVO APEX plan change (SuperAdmin upgrade/downgrade) — money-safety + re-stamp.

The console can move a live account between plans. What must hold:

* EVERY stamped column is rewritten from the catalog (a half-applied change would bill at
  one plan's rate while capping concurrency at another's).
* A rejected change (bad enterprise/trial override, unknown plan, non-APEX org) leaves the
  row EXACTLY as it was — validation happens on a probe object before anything is mutated.
* The Call Credits wallet is never touched: credits were bought at the old rate and stay.
* ``rebill`` replaces the Razorpay mandate — the NEW subscription is opened before anything
  is written, so a Razorpay failure cannot leave a re-priced account with no subscription.
* Re-stamping the SAME plan repairs a drifted row without any billing churn.

DB-backed (real test DB, throwaway orgs). Razorpay and email are always stubbed — a test
must never open a real subscription or mail a customer.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

import pytest
from sqlalchemy import select, text

from app.db import session as db_session
from app.models.organization import Organization
from app.models.subscription import Subscription
from app.services import apex_account_service
from app.services.apex_account_service import ApexAccountError, change_apex_plan
from app.services.apex_plans import APEX_PLANS, stamp_org_from_plan


async def _async_value(value):
    """Awaitable stand-in so a stub can be a one-line lambda."""
    return value


async def _mk_org(plan: str = "core", *, tier: str = "nokvo_apex", status: str = "active") -> uuid.UUID:
    async with db_session.AsyncSessionLocal() as db:
        org = Organization(
            id=uuid.uuid4(), name="plan-change-test", admin_email="plan-change@test.invalid",
            region="southindia", environment="staging", status=status,
            product_tier=tier, calling_enabled=True,
        )
        if tier == "nokvo_apex":
            stamp_org_from_plan(org, plan)
        db.add(org)
        await db.commit()
        return org.id


async def _get(org_id: uuid.UUID) -> Organization:
    async with db_session.AsyncSessionLocal() as db:
        return (await db.execute(select(Organization).where(Organization.id == org_id))).scalars().first()


async def _cleanup(org_id: uuid.UUID) -> None:
    async with db_session.AsyncSessionLocal() as db:
        await db.execute(text("DELETE FROM minute_purchases WHERE organization_id=:o"), {"o": org_id})
        await db.execute(text("DELETE FROM subscriptions WHERE organization_id=:o"), {"o": org_id})
        await db.execute(text("DELETE FROM organizations WHERE id=:o"), {"o": org_id})
        await db.commit()


@pytest.fixture(autouse=True)
def _no_external_calls(monkeypatch):
    """Every test in this file runs with Razorpay OFF and email stubbed by default; the
    re-bill tests opt back in with their own stubs."""
    monkeypatch.setattr(apex_account_service, "_razorpay_configured", lambda: False)

    async def _no_mail(*a, **k):
        return None

    monkeypatch.setattr(apex_account_service.EmailService, "send_apex_plan_change_email", _no_mail)


def _assert_stamped(org: Organization, code: str, *, concurrency: int | None = None) -> None:
    plan = APEX_PLANS[code]
    assert org.apex_plan_code == code
    assert Decimal(str(org.apex_rate_per_minute)) == Decimal(str(plan.rate_per_minute))
    assert int(org.apex_concurrency) == int(concurrency if concurrency is not None else plan.concurrency)
    assert int(org.apex_included_minutes) == plan.included_minutes
    assert int(org.apex_platform_fee_paise) == plan.platform_fee_paise
    assert Decimal(str(org.apex_billed_bonus_pct)) == plan.billed_bonus_pct
    assert Decimal(str(org.apex_topup_bonus_pct)) == plan.topup_bonus_pct
    assert org.apex_support_tier == plan.support_tier


@pytest.mark.asyncio
async def test_upgrade_restamps_every_plan_column_and_leaves_the_wallet_alone():
    from app.services.apex_billing_service import credit_apex_wallet
    from app.services.minute_balance_service import purchased_rupees

    org_id = await _mk_org("core")
    try:
        async with db_session.AsyncSessionLocal() as db:
            await credit_apex_wallet(
                db, organization_id=org_id, minutes=1000, rate=Decimal("9"),
                bonus_pct=Decimal("0"), source="monthly_grant", razorpay_invoice_id="inv-before",
            )
            before_wallet = await purchased_rupees(db, org_id)

            result = await change_apex_plan(db, org_id, plan_code="pinnacle", rebill=False)

        assert result["changed"] is True
        assert result["before"]["plan_code"] == "core"
        assert result["after"]["plan_code"] == "pinnacle"
        _assert_stamped(await _get(org_id), "pinnacle")

        # Credits bought at ₹9 are untouched — they simply buy more minutes at ₹5.50.
        async with db_session.AsyncSessionLocal() as db:
            assert await purchased_rupees(db, org_id) == before_wallet
        assert result["wallet_unchanged"] is True
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_downgrade_is_allowed_and_re_stamps_downward():
    org_id = await _mk_org("pinnacle")
    try:
        async with db_session.AsyncSessionLocal() as db:
            result = await change_apex_plan(db, org_id, plan_code="core", rebill=False)
        assert (result["before"]["concurrency"], result["after"]["concurrency"]) == (4, 1)
        _assert_stamped(await _get(org_id), "core")
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_rejected_change_leaves_the_row_exactly_as_it_was():
    """Enterprise without its negotiated overrides must not half-apply the plan."""
    org_id = await _mk_org("core")
    try:
        async with db_session.AsyncSessionLocal() as db:
            with pytest.raises(ApexAccountError):
                await change_apex_plan(db, org_id, plan_code="enterprise", rebill=False)
            with pytest.raises(ApexAccountError):  # rate at/over the ₹5 ceiling
                await change_apex_plan(
                    db, org_id, plan_code="enterprise", enterprise_rate=5.5,
                    enterprise_concurrency=8, rebill=False,
                )
            with pytest.raises(ApexAccountError):  # concurrency below the enterprise floor
                await change_apex_plan(
                    db, org_id, plan_code="enterprise", enterprise_rate=4.5,
                    enterprise_concurrency=2, rebill=False,
                )
            with pytest.raises(ApexAccountError):
                await change_apex_plan(db, org_id, plan_code="platinum", rebill=False)
        _assert_stamped(await _get(org_id), "core")
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_enterprise_change_stamps_the_negotiated_values():
    org_id = await _mk_org("growth")
    try:
        async with db_session.AsyncSessionLocal() as db:
            result = await change_apex_plan(
                db, org_id, plan_code="enterprise", enterprise_rate=4.25,
                enterprise_concurrency=12, rebill=False,
            )
        org = await _get(org_id)
        assert org.apex_plan_code == "enterprise"
        assert Decimal(str(org.apex_rate_per_minute)) == Decimal("4.25")
        assert int(org.apex_concurrency) == 12
        # The non-negotiated fields still come from the catalog.
        assert int(org.apex_included_minutes) == APEX_PLANS["enterprise"].included_minutes
        assert Decimal(str(org.apex_topup_bonus_pct)) == APEX_PLANS["enterprise"].topup_bonus_pct
        assert result["after"]["rate_per_minute"] == 4.25
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_trial_concurrency_override_is_capped():
    org_id = await _mk_org("core")
    try:
        async with db_session.AsyncSessionLocal() as db:
            with pytest.raises(ApexAccountError):
                await change_apex_plan(db, org_id, plan_code="free_trial", trial_concurrency=99, rebill=False)
            await change_apex_plan(db, org_id, plan_code="free_trial", trial_concurrency=4, rebill=False)
        _assert_stamped(await _get(org_id), "free_trial", concurrency=4)
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_non_apex_org_is_rejected():
    org_id = await _mk_org(tier="nokvo_one")
    try:
        async with db_session.AsyncSessionLocal() as db:
            with pytest.raises(ApexAccountError, match="Not an APEX organization"):
                await change_apex_plan(db, org_id, plan_code="growth", rebill=False)
        org = await _get(org_id)
        assert org.apex_plan_code is None  # no APEX config leaked onto a Nokvo One org
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_restamping_the_same_plan_repairs_drift_without_billing_churn():
    org_id = await _mk_org("growth")
    try:
        # Simulate a hand-edited row: wrong rate + wrong concurrency for its plan.
        async with db_session.AsyncSessionLocal() as db:
            await db.execute(
                text("UPDATE organizations SET apex_rate_per_minute=3, apex_concurrency=9 WHERE id=:o"),
                {"o": org_id},
            )
            await db.commit()
            result = await change_apex_plan(db, org_id, plan_code="growth", rebill=True)
        assert result["changed"] is True          # the drift was corrected
        assert result["rebilled"] is False        # ...but the monthly amount didn't move
        _assert_stamped(await _get(org_id), "growth")
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_putting_a_comped_account_on_a_real_mandate_still_mails_the_link(monkeypatch):
    """Same plan, but the account never had a subscription: nothing about the plan config
    changes, yet a mandate is opened — the customer must still get the link."""
    org_id = await _mk_org("core")
    mailed: list[dict] = []

    async def _mail(to_email, admin_name, org_name, old_label, new_label, monthly_paise, payment_url):
        mailed.append({"url": payment_url, "monthly_paise": monthly_paise})

    monkeypatch.setattr(apex_account_service, "_razorpay_configured", lambda: True)
    monkeypatch.setattr(
        apex_account_service.RazorpayService, "ensure_monthly_plan",
        lambda name, amount: _async_value("plan_c"),
    )
    monkeypatch.setattr(
        apex_account_service.RazorpayService, "create_subscription",
        lambda plan_id, **kw: _async_value({"id": "sub_comp", "short_url": "https://rzp.test/comp"}),
    )
    monkeypatch.setattr(apex_account_service.EmailService, "send_apex_plan_change_email", _mail)

    try:
        async with db_session.AsyncSessionLocal() as db:
            result = await change_apex_plan(db, org_id, plan_code="core", rebill=True)
        assert result["changed"] is False          # the plan config was already correct
        assert result["rebilled"] is True          # ...but it now has a real mandate
        assert mailed and mailed[0]["url"] == "https://rzp.test/comp"
        assert mailed[0]["monthly_paise"] == 1349900
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_rebill_opens_the_new_subscription_and_cancels_the_old_one(monkeypatch):
    org_id = await _mk_org("core")
    cancelled: list[tuple[str, bool]] = []
    mailed: list[dict] = []

    async def _ensure_plan(name, amount_paise):
        return f"plan_{amount_paise}"

    async def _create_sub(plan_id, **kw):
        return {"id": "sub_new", "short_url": "https://rzp.test/new"}

    async def _cancel(sub_id, *, cancel_at_cycle_end=True):
        cancelled.append((sub_id, cancel_at_cycle_end))
        return {"id": sub_id, "status": "cancelled"}

    async def _mail(to_email, admin_name, org_name, old_label, new_label, monthly_paise, payment_url):
        mailed.append({"to": to_email, "old": old_label, "new": new_label, "url": payment_url})

    monkeypatch.setattr(apex_account_service, "_razorpay_configured", lambda: True)
    monkeypatch.setattr(apex_account_service.RazorpayService, "ensure_monthly_plan", _ensure_plan)
    monkeypatch.setattr(apex_account_service.RazorpayService, "create_subscription", _create_sub)
    monkeypatch.setattr(apex_account_service.RazorpayService, "cancel_subscription", _cancel)
    monkeypatch.setattr(apex_account_service.EmailService, "send_apex_plan_change_email", _mail)

    try:
        async with db_session.AsyncSessionLocal() as db:
            db.add(Subscription(
                id=uuid.uuid4(), organization_id=org_id, plan="apex", amount_paise=1349900,
                minutes=1000, razorpay_subscription_id="sub_old", status="active",
            ))
            await db.commit()

            result = await change_apex_plan(db, org_id, plan_code="growth", rebill=True)

        # Growth = ₹7,499 platform + 5000 × ₹7.50 = ₹44,999/mo.
        assert result["rebilled"] is True
        assert result["monthly_inr"] == 44999.0
        assert result["previous_monthly_inr"] == 13499.0
        assert result["payment_url"] == "https://rzp.test/new"
        assert result["old_subscription_cancel_error"] is None
        # A PAID subscription is cancelled at cycle end — the customer keeps the month
        # they already bought.
        assert cancelled == [("sub_old", True)]
        assert mailed and mailed[0]["url"] == "https://rzp.test/new"

        async with db_session.AsyncSessionLocal() as db:
            subs = {
                s.razorpay_subscription_id: s
                for s in (await db.execute(
                    select(Subscription).where(Subscription.organization_id == org_id)
                )).scalars().all()
            }
        assert subs["sub_new"].amount_paise == 4499900
        assert subs["sub_new"].minutes == 5000
        assert subs["sub_old"].cancel_at_period_end is True
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_unpaid_subscription_is_cancelled_immediately(monkeypatch):
    org_id = await _mk_org("core", status="pending_payment")
    cancelled: list[tuple[str, bool]] = []

    async def _cancel(sub_id, *, cancel_at_cycle_end=True):
        cancelled.append((sub_id, cancel_at_cycle_end))
        return {"id": sub_id}

    monkeypatch.setattr(apex_account_service, "_razorpay_configured", lambda: True)
    monkeypatch.setattr(
        apex_account_service.RazorpayService, "ensure_monthly_plan",
        lambda name, amount: _async_value("plan_x"),
    )
    monkeypatch.setattr(
        apex_account_service.RazorpayService, "create_subscription",
        lambda plan_id, **kw: _async_value({"id": "sub_new2", "short_url": "u"}),
    )
    monkeypatch.setattr(apex_account_service.RazorpayService, "cancel_subscription", _cancel)

    try:
        async with db_session.AsyncSessionLocal() as db:
            db.add(Subscription(
                id=uuid.uuid4(), organization_id=org_id, plan="apex", amount_paise=1349900,
                minutes=1000, razorpay_subscription_id="sub_unpaid", status="created",
            ))
            await db.commit()
            await change_apex_plan(db, org_id, plan_code="pinnacle", rebill=True)
        assert cancelled == [("sub_unpaid", False)]  # never charged → kill it now
        async with db_session.AsyncSessionLocal() as db:
            old = (await db.execute(
                select(Subscription).where(Subscription.razorpay_subscription_id == "sub_unpaid")
            )).scalars().first()
        assert old.status == "cancelled"
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_razorpay_failure_leaves_the_account_untouched(monkeypatch):
    org_id = await _mk_org("core")

    async def _boom(*a, **k):
        raise RuntimeError("razorpay down")

    monkeypatch.setattr(apex_account_service, "_razorpay_configured", lambda: True)
    monkeypatch.setattr(apex_account_service.RazorpayService, "ensure_monthly_plan", _boom)

    try:
        async with db_session.AsyncSessionLocal() as db:
            db.add(Subscription(
                id=uuid.uuid4(), organization_id=org_id, plan="apex", amount_paise=1349900,
                minutes=1000, razorpay_subscription_id="sub_keep", status="active",
            ))
            await db.commit()
            with pytest.raises(ApexAccountError, match="new subscription"):
                await change_apex_plan(db, org_id, plan_code="growth", rebill=True)
            await db.rollback()

        _assert_stamped(await _get(org_id), "core")   # still on the old plan
        async with db_session.AsyncSessionLocal() as db:
            sub = (await db.execute(
                select(Subscription).where(Subscription.razorpay_subscription_id == "sub_keep")
            )).scalars().first()
        assert sub.status == "active"                  # old mandate still live
    finally:
        await _cleanup(org_id)


@pytest.mark.asyncio
async def test_failed_cancel_still_lands_the_change_but_reports_it(monkeypatch):
    """A cancel failure must be surfaced loudly — the customer is on TWO mandates until
    someone kills the old one by hand."""
    org_id = await _mk_org("core")

    async def _cancel_boom(*a, **k):
        raise RuntimeError("cancel failed")

    monkeypatch.setattr(apex_account_service, "_razorpay_configured", lambda: True)
    monkeypatch.setattr(
        apex_account_service.RazorpayService, "ensure_monthly_plan",
        lambda name, amount: _async_value("plan_y"),
    )
    monkeypatch.setattr(
        apex_account_service.RazorpayService, "create_subscription",
        lambda plan_id, **kw: _async_value({"id": "sub_new3", "short_url": "u"}),
    )
    monkeypatch.setattr(apex_account_service.RazorpayService, "cancel_subscription", _cancel_boom)

    try:
        async with db_session.AsyncSessionLocal() as db:
            db.add(Subscription(
                id=uuid.uuid4(), organization_id=org_id, plan="apex", amount_paise=1349900,
                minutes=1000, razorpay_subscription_id="sub_stuck", status="active",
            ))
            await db.commit()
            result = await change_apex_plan(db, org_id, plan_code="growth", rebill=True)
        assert "cancel failed" in (result["old_subscription_cancel_error"] or "")
        assert result["rebilled"] is True
        _assert_stamped(await _get(org_id), "growth")
    finally:
        await _cleanup(org_id)

