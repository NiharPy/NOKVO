"""Affiliate TOTP auth: enrollment verify → active, number+code login round-trip,
uniform 401 anti-enumeration, suspended 403, and cryptographic JWT-tier
isolation from org tokens in both directions.
"""
from __future__ import annotations

import asyncio
import uuid

import pyotp
import pytest
from fastapi import HTTPException

import app.api.nokvo_one_affiliate as api
from app.core import security
from app.core.secret_crypto import encrypt_secret
from tests.nokvo_one.affiliate_test_utils import FakeDB, FakeRequest, make_affiliate


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)


def _enrolled_affiliate(status="pending_totp"):
    secret = security.generate_totp_secret()
    affiliate = make_affiliate(status=status, totp_secret_encrypted_v2=encrypt_secret(secret))
    return affiliate, secret


# ── signup TOTP verify (activation) ──


def test_signup_totp_verify_activates_and_reveals_number():
    affiliate, secret = _enrolled_affiliate()
    db = FakeDB(affiliates=[affiliate])
    payload = api.AffiliateTOTPVerifyRequest(
        setup_token=api._issue_setup_token(affiliate.id),
        code=pyotp.TOTP(secret).now(),
    )
    res = _run(api.affiliate_signup_totp_verify(FakeRequest(), payload, db=db))
    assert res["affiliate_number"] == affiliate.affiliate_number
    assert res["access_token"]
    assert affiliate.status == "active"


def test_signup_totp_verify_rejects_wrong_code():
    affiliate, _secret = _enrolled_affiliate()
    db = FakeDB(affiliates=[affiliate])
    payload = api.AffiliateTOTPVerifyRequest(
        setup_token=api._issue_setup_token(affiliate.id), code="000000"
    )
    with pytest.raises(HTTPException) as ei:
        _run(api.affiliate_signup_totp_verify(FakeRequest(), payload, db=db))
    assert ei.value.status_code == 401
    assert affiliate.status == "pending_totp"


# ── login ──


def test_login_round_trip_with_live_totp():
    affiliate, secret = _enrolled_affiliate(status="active")
    db = FakeDB(affiliates=[affiliate])
    payload = api.AffiliateLoginRequest(
        affiliate_number=f" {affiliate.affiliate_number.lower()} ",  # typo-tolerant
        code=pyotp.TOTP(secret).now(),
    )
    res = _run(api.affiliate_login(FakeRequest(), payload, db=db))
    assert res["access_token"]
    assert res["affiliate"]["affiliate_number"] == affiliate.affiliate_number
    assert affiliate.last_login_at is not None
    # The minted session passes the dashboard dependency.
    me = _run(_current(db, res["access_token"]))
    assert me.id == affiliate.id


async def _current(db, token):
    return await api.get_current_affiliate(db=db, token=token)


def test_login_uniform_401_for_unknown_pending_and_bad_code():
    affiliate, secret = _enrolled_affiliate(status="active")
    pending, pending_secret = _enrolled_affiliate(status="pending_totp")
    pending.affiliate_number = "NKVPENDING"
    pending.email = "pending@example.com"
    db = FakeDB(affiliates=[affiliate, pending])
    cases = [
        ("NKVNOSUCH1", "123456"),                                  # unknown number
        (pending.affiliate_number, pyotp.TOTP(pending_secret).now()),  # unverified signup
        (affiliate.affiliate_number, "000000"),                    # wrong code
    ]
    for number, code in cases:
        with pytest.raises(HTTPException) as ei:
            _run(api.affiliate_login(FakeRequest(), api.AffiliateLoginRequest(affiliate_number=number, code=code), db=db))
        assert ei.value.status_code == 401
        assert ei.value.detail == "Invalid affiliate number or code"


def test_login_suspended_403():
    affiliate, secret = _enrolled_affiliate(status="suspended")
    db = FakeDB(affiliates=[affiliate])
    with pytest.raises(HTTPException) as ei:
        _run(api.affiliate_login(
            FakeRequest(),
            api.AffiliateLoginRequest(affiliate_number=affiliate.affiliate_number, code=pyotp.TOTP(secret).now()),
            db=db,
        ))
    assert ei.value.status_code == 403


def test_login_accepts_previous_window_code_rejects_older():
    # valid_window=1: a code read just before the 30s boundary (typed slowly
    # into six boxes) still verifies; anything two windows old does not.
    import time

    affiliate, secret = _enrolled_affiliate(status="active")
    db = FakeDB(affiliates=[affiliate])
    totp = pyotp.TOTP(secret)
    previous = totp.at(time.time() - 30)
    res = _run(api.affiliate_login(
        FakeRequest(),
        api.AffiliateLoginRequest(affiliate_number=affiliate.affiliate_number, code=previous),
        db=db,
    ))
    assert res["access_token"]

    stale = totp.at(time.time() - 90)
    if stale != totp.now() and stale != totp.at(time.time() - 30):  # 1-in-10^6 collision guard
        with pytest.raises(HTTPException) as ei:
            _run(api.affiliate_login(
                FakeRequest(),
                api.AffiliateLoginRequest(affiliate_number=affiliate.affiliate_number, code=stale),
                db=db,
            ))
        assert ei.value.status_code == 401


# ── JWT tier isolation ──


def test_org_token_rejected_by_affiliate_dep():
    org_token = security.create_access_token(
        subject=str(uuid.uuid4()),
        mfa_completed=True,
        token_tier=security.JWT_TIER_ORGANIZATION,
        extra_claims={"principal_type": "organization_user"},
    )
    with pytest.raises(HTTPException) as ei:
        _run(_current(FakeDB(), org_token))
    assert ei.value.status_code == 401  # signature verified against a DIFFERENT tier secret


def test_affiliate_token_rejected_by_org_tier_decode():
    affiliate = make_affiliate()
    token = api._issue_session_token(affiliate)
    import jwt as pyjwt

    with pytest.raises(pyjwt.PyJWTError):
        security.decode_access_token(
            token, expected_tiers=[security.JWT_TIER_ORGANIZATION], allow_legacy_secret=False
        )


def test_setup_token_cannot_reach_dashboard():
    affiliate, _ = _enrolled_affiliate(status="active")
    db = FakeDB(affiliates=[affiliate])
    setup_token = api._issue_setup_token(affiliate.id)
    with pytest.raises(HTTPException) as ei:
        _run(_current(db, setup_token))
    assert ei.value.status_code == 403  # right tier, wrong principal_type
