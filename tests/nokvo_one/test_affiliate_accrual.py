"""Commission accrual: 5% first invoice vs 2% recurring (decided by prior rows
per subscription id), idempotency on the unique payment id, no-attribution and
suspended no-ops, IntegrityError race yield, and the best-effort contract.
"""
from __future__ import annotations

import asyncio
import uuid
from decimal import Decimal

from app.services.affiliate_service import accrue_affiliate_commission, mask_account_number, mask_org_name
from tests.nokvo_one.affiliate_test_utils import FakeDB, make_affiliate, make_commission, make_org


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _attributed():
    affiliate = make_affiliate()
    org = make_org(affiliate_id=affiliate.id, status="active")
    return affiliate, org


def _accrue(db, org, *, payment_id="pay_1", sub_id="sub_1", amount=649900 + 550000):
    return accrue_affiliate_commission(
        db,
        organization_id=org.id,
        razorpay_payment_id=payment_id,
        razorpay_subscription_id=sub_id,
        amount_paise=amount,
    )


def test_first_month_then_recurring_rates():
    affiliate, org = _attributed()
    db = FakeDB(affiliates=[affiliate], orgs=[org])
    # First invoice: platform fee + minutes addon (₹6499 + ₹5500) at 5%.
    _run(_accrue(db, org, payment_id="pay_1", amount=649900 + 550000))
    assert len(db.commissions) == 1
    first = db.commissions[0]
    assert first.commission_type == "first_month"
    assert first.rate == Decimal("0.05")
    assert first.amount_rupees == Decimal("599.9500")  # (1199900/100) * 0.05
    assert first.billed_paise == 1199900
    # Month 2: plan only (₹6499) at 2%.
    _run(_accrue(db, org, payment_id="pay_2", amount=649900))
    assert len(db.commissions) == 2
    second = db.commissions[1]
    assert second.commission_type == "recurring"
    assert second.rate == Decimal("0.02")
    assert second.amount_rupees == Decimal("129.9800")
    assert db.commits == 2


def test_duplicate_payment_id_accrues_once():
    affiliate, org = _attributed()
    db = FakeDB(affiliates=[affiliate], orgs=[org])
    _run(_accrue(db, org, payment_id="pay_dup"))
    _run(_accrue(db, org, payment_id="pay_dup"))  # webhook replay / verify race
    assert len(db.commissions) == 1
    assert db.commits == 1


def test_integrity_race_loser_yields_without_raising():
    affiliate, org = _attributed()
    db = FakeDB(affiliates=[affiliate], orgs=[org])
    db.raise_integrity_on_commit = 1
    _run(_accrue(db, org))  # must swallow the IntegrityError
    assert db.rollbacks == 1
    assert db.commits == 0


def test_no_attribution_no_row():
    org = make_org(affiliate_id=None)
    db = FakeDB(orgs=[org])
    _run(_accrue(db, org))
    assert db.commissions == [] and db.commits == 0


def test_suspended_affiliate_stops_new_accrual():
    affiliate = make_affiliate(status="suspended")
    org = make_org(affiliate_id=affiliate.id)
    db = FakeDB(affiliates=[affiliate], orgs=[org])
    _run(_accrue(db, org))
    assert db.commissions == []


def test_unusable_inputs_are_ignored():
    affiliate, org = _attributed()
    db = FakeDB(affiliates=[affiliate], orgs=[org])
    _run(_accrue(db, org, payment_id="", amount=649900))
    _run(_accrue(db, org, payment_id="pay_x", amount=0))
    _run(_accrue(db, org, payment_id="pay_y", amount=-5))
    assert db.commissions == []


def test_first_vs_recurring_is_per_subscription():
    affiliate, org = _attributed()
    # A prior commission on a DIFFERENT subscription must not demote this one.
    other = make_commission(razorpay_subscription_id="sub_other")
    db = FakeDB(affiliates=[affiliate], orgs=[org], commissions=[other])
    _run(_accrue(db, org, payment_id="pay_new", sub_id="sub_new", amount=649900))
    added = [c for c in db.commissions if c.razorpay_payment_id == "pay_new"]
    assert added[0].commission_type == "first_month"


def test_best_effort_never_raises():
    class ExplodingDB(FakeDB):
        async def get(self, model, pk):  # noqa: ARG002
            raise RuntimeError("db down")

    affiliate, org = _attributed()
    _run(_accrue(ExplodingDB(), org))  # swallowed + logged, never raised


# ── privacy masks (dashboard display) ──


def test_masks():
    assert mask_org_name("Acme Corp") == "Ac•••"
    assert mask_org_name("") == "•••"
    assert mask_account_number("123456789012") == "••••9012"
    assert mask_account_number("") == ""
