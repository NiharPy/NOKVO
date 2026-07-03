"""Razorpay payment-gated onboarding: signature verification + tiered usage rates.

The HTTP/DB-bound parts (create-subscription, activate_and_provision) are
exercised end-to-end against Razorpay test mode + a real DB; here we lock down
the pure, security-critical math: checkout + webhook HMAC and the tiered rate
brackets.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
from decimal import Decimal

import pytest

from app.core.config import settings


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)
from app.services.call_cost_calculator import (
    CostBreakdown,
    RATE_TIERS,
    rupees_per_minute_for,
    rupees_per_second_for,
)
from app.services.razorpay_service import PLAN_CATALOG, RazorpayService


# ─────────── plan catalog ───────────
def test_plan_catalog_amounts_and_outbound_gating():
    assert PLAN_CATALOG["inbound_only"]["amount_paise"] == 449900
    assert PLAN_CATALOG["inbound_outbound"]["amount_paise"] == 649900
    assert PLAN_CATALOG["inbound_only"]["outbound"] is False
    assert PLAN_CATALOG["inbound_outbound"]["outbound"] is True


# ─────────── checkout signature (payment_id | subscription_id) ───────────
def _checkout_sig(payment_id: str, subscription_id: str) -> str:
    return hmac.new(
        settings.RAZORPAY_KEY_SECRET.encode(),
        f"{payment_id}|{subscription_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def test_checkout_signature_good_bad_missing():
    sig = _checkout_sig("pay_X", "sub_Y")
    assert RazorpayService.verify_checkout_signature("pay_X", "sub_Y", sig) is True
    # wrong operand order must NOT validate (subscription flow ≠ order flow)
    bad_order = hmac.new(settings.RAZORPAY_KEY_SECRET.encode(), b"sub_Y|pay_X", hashlib.sha256).hexdigest()
    assert RazorpayService.verify_checkout_signature("pay_X", "sub_Y", bad_order) is False
    assert RazorpayService.verify_checkout_signature("pay_X", "sub_Y", "deadbeef") is False
    assert RazorpayService.verify_checkout_signature("", "sub_Y", sig) is False
    assert RazorpayService.verify_checkout_signature("pay_X", "sub_Y", "") is False


# ─────────── webhook signature (HMAC over raw body) ───────────
def test_webhook_signature_good_bad(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "whsec_test")
    body = b'{"event":"subscription.charged"}'
    good = hmac.new(b"whsec_test", body, hashlib.sha256).hexdigest()
    assert RazorpayService.verify_webhook_signature(body, good) is True
    assert RazorpayService.verify_webhook_signature(body, "nope") is False
    assert RazorpayService.verify_webhook_signature(body, None) is False
    # tampered body fails
    assert RazorpayService.verify_webhook_signature(b'{"event":"hacked"}', good) is False


def test_webhook_signature_requires_secret(monkeypatch):
    monkeypatch.setattr(settings, "RAZORPAY_WEBHOOK_SECRET", "")
    assert RazorpayService.verify_webhook_signature(b"x", "anything") is False


# ─────────── cancel subscription (cancel at cycle end) ───────────
def test_cancel_subscription_posts_cycle_end(monkeypatch):
    captured = {}

    async def fake_request(method, path, *, json_body=None):
        captured.update(method=method, path=path, body=json_body)
        return {"id": "sub_1", "status": "active", "current_end": 1750000000}

    monkeypatch.setattr(RazorpayService, "_request", staticmethod(fake_request))
    out = _run(RazorpayService.cancel_subscription("sub_1"))
    assert captured["method"] == "POST"
    assert captured["path"] == "subscriptions/sub_1/cancel"
    assert captured["body"] == {"cancel_at_cycle_end": 1}  # keep the paid month
    assert out["current_end"] == 1750000000


def test_cancel_subscription_immediate(monkeypatch):
    captured = {}

    async def fake_request(method, path, *, json_body=None):
        captured["body"] = json_body
        return {}

    monkeypatch.setattr(RazorpayService, "_request", staticmethod(fake_request))
    _run(RazorpayService.cancel_subscription("sub_2", cancel_at_cycle_end=False))
    assert captured["body"] == {"cancel_at_cycle_end": 0}  # unpaid sub → cancel now


def test_unix_to_dt_helper():
    from app.api.nokvo_one_payments import _unix_to_dt

    assert _unix_to_dt(0) is None
    assert _unix_to_dt(None) is None
    assert _unix_to_dt("bad") is None
    dt = _unix_to_dt(1750000000)
    assert dt is not None and dt.tzinfo is not None  # aware UTC datetime


# ─────────── tiered per-minute rates (per calendar month) ───────────
@pytest.mark.parametrize(
    "minutes,expected",
    [
        (0, "10"), (1, "10"), (999, "10"),
        (1000, "9"), (5000, "9"), (9999, "9"),
        (10000, "8"), (24999, "8"),
        (25000, "6.5"), (1_000_000, "6.5"),
    ],
)
def test_tiered_rate_boundaries(minutes, expected):
    assert rupees_per_minute_for(minutes) == Decimal(expected)


def test_rate_tier_table_is_ordered_and_open_ended():
    bounds = [b for b, _ in RATE_TIERS]
    assert bounds[-1] is None  # last bracket is open-ended
    finite = [b for b in bounds if b is not None]
    assert finite == sorted(finite)


def test_for_duration_at_rate_uses_given_tier():
    # 60s at the ₹10 tier = ₹10.00; at the ₹6.5 tier = ₹6.50.
    top = CostBreakdown.for_duration_at_rate(60, rupees_per_second_for(0))
    assert top.rupees == Decimal("10.0000")
    bottom = CostBreakdown.for_duration_at_rate(60, rupees_per_second_for(30000))
    assert bottom.rupees == Decimal("6.5000")
