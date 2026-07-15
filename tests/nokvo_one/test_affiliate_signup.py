"""Affiliate public signup: IST 18+ gate, pending_totp reclaim (keeps
id+number, rotates secret, clears KYC approval), and the unambiguous
affiliate-number alphabet + collision retry.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

import app.api.nokvo_one_affiliate as api
from app.services import affiliate_service
from tests.nokvo_one.affiliate_test_utils import FakeDB, FakeRequest, make_affiliate


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _no_rate_limit(monkeypatch):
    from app.core.rate_limit import limiter

    monkeypatch.setattr(limiter, "enabled", False)


def _signup(db, **overrides):
    kwargs = dict(
        request=FakeRequest(),
        full_name="Priya Sharma",
        date_of_birth="1995-05-05",
        email="priya@example.com",
        db=db,
    )
    kwargs.update(overrides)
    return api.affiliate_signup(**kwargs)


# ── 18+ gate against IST "today" ──


class _Frozen(datetime):
    """2026-07-13 20:00 UTC == 2026-07-14 01:30 IST — the dates DIFFER."""

    @classmethod
    def now(cls, tz=None):
        base = datetime(2026, 7, 13, 20, 0, tzinfo=timezone.utc)
        return base.astimezone(tz) if tz else base.replace(tzinfo=None)


def test_dob_18th_birthday_passes_in_ist_not_utc(monkeypatch):
    monkeypatch.setattr(api, "datetime", _Frozen)
    # Born 2008-07-14: 18 by IST today (2026-07-14), still 17 by UTC (2026-07-13).
    db = FakeDB()
    res = _run(_signup(db, date_of_birth="2008-07-14"))
    assert res["setup_token"] and res["totp_uri"].startswith("otpauth://")
    # A day younger is still 17 in IST → rejected.
    with pytest.raises(HTTPException) as ei:
        _run(_signup(FakeDB(), email="other@example.com", date_of_birth="2008-07-15"))
    assert ei.value.status_code == 403


def test_dob_validation_errors():
    for bad, status in (("not-a-date", 400), ("2099-01-01", 400), ("1900-01-01", 400)):
        with pytest.raises(HTTPException) as ei:
            _run(_signup(FakeDB(), date_of_birth=bad))
        assert ei.value.status_code == status


def test_bad_email_rejected():
    with pytest.raises(HTTPException) as ei:
        _run(_signup(FakeDB(), email="not-an-email"))
    assert ei.value.status_code == 400


# ── duplicate email / reclaim ──


def test_active_email_conflicts_409():
    db = FakeDB(affiliates=[make_affiliate(email="priya@example.com", status="active")])
    with pytest.raises(HTTPException) as ei:
        _run(_signup(db))
    assert ei.value.status_code == 409


def test_pending_totp_reclaim_keeps_identity_and_secret_clears_kyc():
    from app.core.secret_crypto import encrypt_secret
    from app.core.security import generate_totp_secret

    original_secret = generate_totp_secret()
    existing = make_affiliate(
        email="priya@example.com",
        status="pending_totp",
        totp_secret_encrypted_v2=encrypt_secret(original_secret),
        kyc_verified_at=datetime.now(timezone.utc),
        kyc_verified_by="op@nokvo.ai",
    )
    db = FakeDB(affiliates=[existing])
    res = _run(_signup(db, full_name="Priya S"))
    assert res["setup_token"]
    assert len(db.affiliates) == 1  # reclaimed, not duplicated
    assert db.affiliates[0].id == existing.id
    assert db.affiliates[0].affiliate_number == existing.affiliate_number
    # The pending secret is KEPT — a re-submitted step 1 must not invalidate a
    # QR the user already scanned. The response hands back the same secret.
    assert res["secret"] == original_secret
    # Identity fields may have changed → the old KYC approval no longer applies.
    assert db.affiliates[0].kyc_verified_at is None
    assert db.affiliates[0].kyc_verified_by is None
    assert db.affiliates[0].full_name == "Priya S"


def test_pending_totp_reclaim_issues_fresh_secret_when_undecryptable():
    # A stored blob encrypted under an old/rotated key can't be reused — the
    # reclaim falls back to a fresh secret instead of failing the signup.
    existing = make_affiliate(
        email="priya@example.com",
        status="pending_totp",
        totp_secret_encrypted_v2="not-a-valid-fernet-blob",
    )
    db = FakeDB(affiliates=[existing])
    res = _run(_signup(db))
    assert res["secret"]
    assert db.affiliates[0].totp_secret_encrypted_v2 != "not-a-valid-fernet-blob"


# ── affiliate-number generation ──


def test_number_alphabet_excludes_ambiguous_chars():
    for _ in range(200):
        number = affiliate_service.generate_affiliate_number()
        assert number.startswith("NKV") and len(number) == 10
        assert all(ch not in "IO01" for ch in number[3:])
        assert all(ch in affiliate_service.AFFILIATE_NUMBER_ALPHABET for ch in number[3:])


def test_allocate_retries_on_collision(monkeypatch):
    taken = make_affiliate(affiliate_number="NKVAAAAAAA", email="taken@example.com")
    db = FakeDB(affiliates=[taken])
    seq = iter(["NKVAAAAAAA", "NKVBBBBBBB"])  # first collides, second is free
    monkeypatch.setattr(affiliate_service, "generate_affiliate_number", lambda: next(seq))
    assert _run(affiliate_service.allocate_affiliate_number(db)) == "NKVBBBBBBB"


def test_normalize_affiliate_number():
    assert affiliate_service.normalize_affiliate_number(" nkv-7xq2 mrt ") == "NKV7XQ2MRT"
