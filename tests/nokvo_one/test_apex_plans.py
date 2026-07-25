"""NOKVO APEX plan-driven pricing — catalog + wallet + subscription + call cost.

Pins the per-plan rate/bonus math, the monthly subscription totals from the spec, the
enterprise per-deal validation, and the plan-rate call-cost (incl. the sub-connect-fee
clamp).
"""
from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.services.apex_plans import (
    APEX_PLANS,
    get_apex_concurrency,
    get_apex_rate,
    monthly_subscription_paise,
    stamp_org_from_plan,
    wallet_credit_for,
)
from app.services.minute_pricing import APEX_CONNECT_FEE, apex_call_cost_at_rate


# ── plan-rate call cost ──────────────────────────────────────────────────────
def test_full_minute_equals_plan_rate():
    # A full 60s call costs exactly the plan's ₹/min (connect fee + talk == rate).
    assert apex_call_cost_at_rate(60, Decimal("9")) == Decimal("9.0000")
    assert apex_call_cost_at_rate(60, Decimal("7.5")) == Decimal("7.5000")
    assert apex_call_cost_at_rate(60, Decimal("5.5")) == Decimal("5.5000")


def test_connect_fee_charged_once_not_per_minute():
    # 90s at ₹5.5: 1.5 + (5.5-1.5)/60*90 = 1.5 + 6 = 7.5 (fee not re-applied at 60s).
    assert apex_call_cost_at_rate(90, Decimal("5.5")) == Decimal("7.5000")
    # 30s at ₹5.5: 1.5 + 4/60*30 = 3.5.
    assert apex_call_cost_at_rate(30, Decimal("5.5")) == Decimal("3.5000")


def test_never_connected_is_free():
    assert apex_call_cost_at_rate(0, Decimal("9")) == Decimal("0.0000")
    assert apex_call_cost_at_rate(-5, Decimal("9")) == Decimal("0.0000")


def test_rate_below_connect_fee_clamps_to_fee():
    # A rate under the ₹1.5 connect fee must never produce a negative talk rate — the
    # per-second term clamps to 0, so any duration costs just the connect fee.
    assert apex_call_cost_at_rate(60, Decimal("1")) == Decimal("1.5000")
    assert apex_call_cost_at_rate(3600, Decimal("1")) == Decimal("1.5000")


# ── wallet credit (rupee credits, plan bonus) ────────────────────────────────
def test_wallet_credit_bonus_tiers():
    # Core / Growth: no bonus → billed == credited.
    assert wallet_credit_for(1000, Decimal("9"), Decimal("0")) == Decimal("9000.0000")
    assert wallet_credit_for(5000, Decimal("7.5"), Decimal("0")) == Decimal("37500.0000")
    # Pinnacle monthly: 25000*5.5=137500 → +20% → 165000.
    assert wallet_credit_for(25000, Decimal("5.5"), Decimal("20")) == Decimal("165000.0000")
    # Enterprise example: 100000*4.5=450000 → +50% → 675000.
    assert wallet_credit_for(100000, Decimal("4.5"), Decimal("50")) == Decimal("675000.0000")
    # Pinnacle top-up bonus 10%: 1000*5.5=5500 → 6050.
    assert wallet_credit_for(1000, Decimal("5.5"), Decimal("10")) == Decimal("6050.0000")


def test_wallet_credit_requires_concrete_rate():
    with pytest.raises(ValueError):
        wallet_credit_for(1000, None, Decimal("0"))
    with pytest.raises(ValueError):
        wallet_credit_for(1000, Decimal("9"), None)


# ── monthly subscription totals (spec sanity-check) ──────────────────────────
def test_monthly_subscription_totals():
    # Core ₹13,499 · Growth ₹44,999 · Pinnacle ₹147,499 (in paise).
    assert monthly_subscription_paise(Decimal("9"), 1000, 449900) == 1349900
    assert monthly_subscription_paise(Decimal("7.5"), 5000, 749900) == 4499900
    assert monthly_subscription_paise(Decimal("5.5"), 25000, 999900) == 14749900
    # Enterprise with a per-deal ₹4.5 rate: ₹20,000 fee + 100000*4.5 = ₹4,70,000.
    assert monthly_subscription_paise(Decimal("4.5"), 100000, 2000000) == 47000000


# ── stamping an org from a plan ──────────────────────────────────────────────
def test_stamp_core_writes_concrete_config():
    org = SimpleNamespace()
    stamp_org_from_plan(org, "core")
    assert org.apex_plan_code == "core"
    assert org.apex_rate_per_minute == Decimal("9")
    assert org.apex_concurrency == 1
    assert org.apex_included_minutes == 1000
    assert org.apex_platform_fee_paise == 449900
    assert org.apex_support_tier == "basic_onboarding"


def test_stamp_enterprise_requires_and_validates_overrides():
    org = SimpleNamespace()
    # Missing overrides → error.
    with pytest.raises(ValueError):
        stamp_org_from_plan(org, "enterprise")
    # Rate must be < ₹5.
    with pytest.raises(ValueError):
        stamp_org_from_plan(org, "enterprise", enterprise_rate=Decimal("5.5"), enterprise_concurrency=5)
    # Concurrency must be ≥ 5.
    with pytest.raises(ValueError):
        stamp_org_from_plan(org, "enterprise", enterprise_rate=Decimal("4.5"), enterprise_concurrency=3)
    # Rate below the connect fee → error.
    with pytest.raises(ValueError):
        stamp_org_from_plan(org, "enterprise", enterprise_rate=Decimal("1"), enterprise_concurrency=5)
    # Valid per-deal values stamp through.
    stamp_org_from_plan(org, "enterprise", enterprise_rate=Decimal("4.5"), enterprise_concurrency=8)
    assert org.apex_rate_per_minute == Decimal("4.5")
    assert org.apex_concurrency == 8
    assert org.apex_billed_bonus_pct == Decimal("50")


def test_get_apex_rate_and_concurrency_fallback_to_catalog():
    # Stamped column wins.
    org = SimpleNamespace(apex_rate_per_minute=Decimal("5.5"), apex_concurrency=4, apex_plan_code="pinnacle")
    assert get_apex_rate(org) == Decimal("5.5")
    assert get_apex_concurrency(org) == 4
    # Legacy row (NULL columns) falls back to the catalog by plan code.
    legacy = SimpleNamespace(apex_rate_per_minute=None, apex_concurrency=None, apex_plan_code="growth")
    assert get_apex_rate(legacy) == APEX_PLANS["growth"].rate_per_minute
    assert get_apex_concurrency(legacy) == APEX_PLANS["growth"].concurrency


def test_all_chargeable_plans_have_a_rate_at_or_above_connect_fee():
    for code, plan in APEX_PLANS.items():
        if plan.rate_per_minute is not None:
            assert plan.rate_per_minute >= APEX_CONNECT_FEE, code
