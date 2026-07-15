"""Operator settlement: payout gated on KYC-verified + bank details + active,
the 48h T+2 due boundary, batch stamping under one UTR, and idempotent
nothing-due behavior.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

import app.api.superadmin_tenant_provisioning as sa
from app.services.affiliate_service import settlement_eligible
from tests.nokvo_one.affiliate_test_utils import FakeDB, FakeRequest, make_affiliate, make_commission


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _operator():
    return SimpleNamespace(id=uuid.uuid4(), email="op@nokvo.ai")


def _payable_affiliate():
    return make_affiliate(
        status="active",
        kyc_verified_at=datetime.now(timezone.utc),
        bank_account_holder="Priya Sharma",
        bank_account_number="123456789012",
        bank_ifsc="HDFC0001234",
    )


def _due_commission(affiliate, *, hours_ago=72, amount="129.98"):
    from decimal import Decimal

    return make_commission(
        affiliate_id=affiliate.id,
        amount_rupees=Decimal(amount),
        created_at=datetime.now(timezone.utc) - timedelta(hours=hours_ago),
    )


def _settle(db, affiliate, utr="UTR123456"):
    return sa.settle_affiliate_commissions(
        affiliate.id,
        sa.AffiliateSettleRequest(utr=utr),
        FakeRequest(),
        db=db,
        current_user=_operator(),
    )


def test_settlement_eligibility_is_derived():
    assert settlement_eligible(_payable_affiliate())
    assert not settlement_eligible(make_affiliate(status="active"))  # no KYC, no bank
    kyc_only = make_affiliate(kyc_verified_at=datetime.now(timezone.utc))
    assert not settlement_eligible(kyc_only)  # no bank
    suspended = _payable_affiliate()
    suspended.status = "suspended"
    assert not settlement_eligible(suspended)


def test_settle_409_without_kyc_or_bank():
    no_kyc = make_affiliate(
        status="active",
        bank_account_holder="P", bank_account_number="123456789", bank_ifsc="HDFC0001234",
    )
    db = FakeDB(affiliates=[no_kyc], commissions=[_due_commission(no_kyc)])
    with pytest.raises(HTTPException) as ei:
        _run(_settle(db, no_kyc))
    assert ei.value.status_code == 409

    no_bank = make_affiliate(status="active", kyc_verified_at=datetime.now(timezone.utc))
    db = FakeDB(affiliates=[no_bank], commissions=[_due_commission(no_bank)])
    with pytest.raises(HTTPException) as ei:
        _run(_settle(db, no_bank))
    assert ei.value.status_code == 409


def test_settle_409_when_nothing_due_yet():
    affiliate = _payable_affiliate()
    # Only a FRESH commission (1h old) — inside the T+2 window, not yet due.
    db = FakeDB(affiliates=[affiliate], commissions=[_due_commission(affiliate, hours_ago=1)])
    with pytest.raises(HTTPException) as ei:
        _run(_settle(db, affiliate))
    assert ei.value.status_code == 409
    assert "due" in ei.value.detail.lower()


def test_settle_stamps_only_due_rows_under_one_utr():
    affiliate = _payable_affiliate()
    due_a = _due_commission(affiliate, hours_ago=72, amount="324.95")
    due_b = _due_commission(affiliate, hours_ago=49, amount="129.98")
    fresh = _due_commission(affiliate, hours_ago=1, amount="129.98")   # NOT due yet
    settled_already = _due_commission(affiliate, hours_ago=200)
    settled_already.settlement_id = uuid.uuid4()                        # already paid
    db = FakeDB(affiliates=[affiliate], commissions=[due_a, due_b, fresh, settled_already])

    res = _run(_settle(db, affiliate, utr="UTR-XYZ-1"))
    assert res["commission_count"] == 2
    assert res["amount_rupees"] == pytest.approx(454.93)
    assert res["utr_reference"] == "UTR-XYZ-1"
    settlement_id = uuid.UUID(res["settlement_id"])
    assert due_a.settlement_id == settlement_id and due_b.settlement_id == settlement_id
    assert fresh.settlement_id is None                       # stays pending
    assert settled_already.settlement_id != settlement_id    # untouched
    assert len(db.settlements) == 1
    assert db.settlements[0].settled_by == "op@nokvo.ai"

    # Re-running immediately finds nothing due — the batch is not double-paid.
    with pytest.raises(HTTPException) as ei:
        _run(_settle(db, affiliate, utr="UTR-XYZ-2"))
    assert ei.value.status_code == 409


def test_settle_requires_utr():
    affiliate = _payable_affiliate()
    db = FakeDB(affiliates=[affiliate], commissions=[_due_commission(affiliate)])
    with pytest.raises(HTTPException) as ei:
        _run(_settle(db, affiliate, utr="   "))
    assert ei.value.status_code == 400


def test_kyc_verify_is_idempotent():
    affiliate = make_affiliate(status="active")
    db = FakeDB(affiliates=[affiliate])
    res1 = _run(sa.verify_affiliate_kyc(affiliate.id, FakeRequest(), db=db, current_user=_operator()))
    stamp = res1["kyc_verified_at"]
    assert stamp is not None and res1["kyc_verified_by"] == "op@nokvo.ai"
    res2 = _run(sa.verify_affiliate_kyc(affiliate.id, FakeRequest(), db=db, current_user=_operator()))
    assert res2["kyc_verified_at"] == stamp  # unchanged on the second call


def test_reset_totp_rotates_secret_and_pends():
    affiliate = make_affiliate(status="active", totp_secret_encrypted_v2="old")
    db = FakeDB(affiliates=[affiliate])
    res = _run(sa.reset_affiliate_totp(affiliate.id, FakeRequest(), db=db, current_user=_operator()))
    assert res["status"] == "pending_totp"
    assert affiliate.totp_secret_encrypted_v2 != "old"
