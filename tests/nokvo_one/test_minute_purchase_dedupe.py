"""Onboarding minute-bundle credit is once-per-SUBSCRIPTION, not once-per-payment.

The bug: every monthly `subscription.charged` webhook stamps a fresh payment id
onto the Subscription row before spawning ``_bg_activate``, whose
``_record_minute_purchase`` call was idempotent only per payment id — so the
full onboarding bundle was re-credited every renewal cycle. The fix keys
onboarding dedupe on ``razorpay_ref`` (the subscription id); top-ups keep
payment-id-only dedupe (each top-up is its own Order/ref).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest

import app.api.nokvo_one_payments as payments
from app.models.minute_purchase import MinutePurchase
from app.models.subscription import Subscription
from tests.nokvo_one.affiliate_test_utils import FakeDB


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_balance_cache(monkeypatch):
    import app.services.minute_balance_service as mbs

    async def noop(_org_id):
        return None

    monkeypatch.setattr(mbs, "invalidate_balance_cache", noop)


def _db(purchases=()):
    return FakeDB(pools={MinutePurchase: list(purchases), Subscription: []})


def _onboarding_row(*, ref="sub_X", payment_id="pay_month1", minutes=1000):
    return MinutePurchase(
        id=uuid.uuid4(),
        organization_id=uuid.uuid4(),
        minutes=minutes,
        rupees=10000,
        rate_per_minute=10,
        source="onboarding",
        razorpay_payment_id=payment_id,
        razorpay_ref=ref,
        created_at=datetime.now(timezone.utc),
    )


def _record(db, *, source="onboarding", payment_id="pay_month1", ref="sub_X", minutes=1000):
    return payments._record_minute_purchase(
        db,
        organization_id=uuid.uuid4(),
        minutes=minutes,
        source=source,
        razorpay_payment_id=payment_id,
        razorpay_ref=ref,
    )


# ── _record_minute_purchase dedupe keys ──


def test_first_onboarding_credit_lands():
    db = _db()
    _run(_record(db))
    assert len(db.pools[MinutePurchase]) == 1
    assert db.commits == 1


def test_renewal_payment_id_does_not_recredit_onboarding_bundle():
    # THE bug: month 2 arrives with a NEW payment id but the SAME subscription
    # ref — the bundle must not be credited again.
    db = _db([_onboarding_row(ref="sub_X", payment_id="pay_month1")])
    _run(_record(db, payment_id="pay_month2", ref="sub_X"))
    assert len(db.pools[MinutePurchase]) == 1
    assert db.commits == 0


def test_same_payment_id_replay_still_deduped():
    db = _db([_onboarding_row(ref="sub_X", payment_id="pay_month1")])
    _run(_record(db, payment_id="pay_month1", ref="sub_X"))
    assert len(db.pools[MinutePurchase]) == 1


def test_different_subscription_gets_its_own_onboarding_credit():
    # A re-subscribe creates a NEW Razorpay subscription — its bundle credits.
    db = _db([_onboarding_row(ref="sub_OLD", payment_id="pay_old")])
    _run(_record(db, payment_id="pay_new", ref="sub_NEW"))
    assert len(db.pools[MinutePurchase]) == 2


def test_topups_are_not_affected_by_the_ref_check():
    # Two separate top-up Orders (unique refs + payment ids) both credit; a
    # replay of one payment id still dedupes.
    db = _db()
    _run(_record(db, source="topup", payment_id="pay_t1", ref="order_1"))
    _run(_record(db, source="topup", payment_id="pay_t2", ref="order_2"))
    _run(_record(db, source="topup", payment_id="pay_t1", ref="order_1"))
    assert len(db.pools[MinutePurchase]) == 2


# ── the actual bug path: month-2 webhook → _bg_activate ──


class _Ctx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *args):
        return False


def test_bg_activate_month2_webhook_does_not_recredit(monkeypatch):
    org_id = uuid.uuid4()
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        plan="apex",
        amount_paise=649900,
        currency="INR",
        minutes=1000,
        razorpay_subscription_id="sub_X",
        # The month-2 webhook already stamped the NEW payment id (the exact
        # state that used to trigger the re-credit).
        razorpay_payment_id="pay_month2",
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    month1_credit = _onboarding_row(ref="sub_X", payment_id="pay_month1")
    db = FakeDB(pools={Subscription: [sub], MinutePurchase: [month1_credit]})

    import app.db.session as db_session

    monkeypatch.setattr(db_session, "AsyncSessionLocal", lambda: _Ctx(db))

    async def fake_fetch(_sub_id):
        return {"status": "active"}

    monkeypatch.setattr(
        payments.RazorpayService, "fetch_subscription", staticmethod(fake_fetch)
    )

    async def fake_activate(_db, _org_id):
        return {"provisioned": True, "org_status": "active", "idempotent": True}

    monkeypatch.setattr(payments, "activate_and_provision", fake_activate)

    _run(payments._bg_activate(org_id, "sub_X"))
    assert len(db.pools[MinutePurchase]) == 1  # month-1 credit only — no re-credit


def test_bg_activate_first_payment_still_credits(monkeypatch):
    # The failure-safe purpose of _bg_activate survives the fix: a lost client
    # verify still gets the onboarding bundle credited by the webhook path.
    org_id = uuid.uuid4()
    sub = Subscription(
        id=uuid.uuid4(),
        organization_id=org_id,
        plan="apex",
        amount_paise=649900,
        currency="INR",
        minutes=1000,
        razorpay_subscription_id="sub_Y",
        razorpay_payment_id="pay_first",
        status="active",
        created_at=datetime.now(timezone.utc),
    )
    db = FakeDB(pools={Subscription: [sub], MinutePurchase: []})

    import app.db.session as db_session

    monkeypatch.setattr(db_session, "AsyncSessionLocal", lambda: _Ctx(db))

    async def fake_fetch(_sub_id):
        return {"status": "active"}

    monkeypatch.setattr(
        payments.RazorpayService, "fetch_subscription", staticmethod(fake_fetch)
    )

    async def fake_activate(_db, _org_id):
        return {"provisioned": True, "org_status": "onboarding"}

    monkeypatch.setattr(payments, "activate_and_provision", fake_activate)

    _run(payments._bg_activate(org_id, "sub_Y"))
    rows = db.pools[MinutePurchase]
    assert len(rows) == 1
    assert rows[0].source == "onboarding"
    assert rows[0].razorpay_ref == "sub_Y"
    assert rows[0].razorpay_payment_id == "pay_first"
