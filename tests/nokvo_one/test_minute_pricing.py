"""Flat-by-bracket prepaid-minute pricing.

The WHOLE bundle bills at the single rate of the bracket the quantity lands in
(upper-inclusive) — distinct from the marginal post-paid tariff. These pin the
user's worked examples + every bracket boundary + the paise conversion.
"""
from __future__ import annotations

from decimal import Decimal

from app.services.minute_pricing import (
    APEX_CONNECT_FEE,
    MINUTES_MIN,
    PER_MINUTE_FEE,
    apex_call_cost,
    apex_wallet_credit,
    call_usage_cost,
    cost_display,
    cost_for_minutes,
    cost_paise,
    credited_minutes,
    flat_rate_for_minutes,
    slab_ladder,
    usage_rate_per_second,
)


def test_apex_bonus_credited_minutes():
    # The user's worked example: select 25001 → bill 137505.5, credit 37501.
    assert cost_for_minutes(25001) == Decimal("137505.5000")
    assert cost_paise(25001) == 13750550
    assert credited_minutes(25001) == 37501          # floor(25001 * 1.5) = 37501.5 → 37501
    # round-number selections credit exactly 1.5×.
    assert credited_minutes(1000) == 1500
    assert credited_minutes(6000) == 9000
    assert credited_minutes(10000) == 15000
    # odd selection → floor (never rounds up).
    assert credited_minutes(1001) == 1501            # 1501.5 → 1501
    assert credited_minutes(0) == 0
    assert credited_minutes(-5) == 0


def test_user_worked_examples():
    # 6000 → 5000–10000 bracket @ ₹9 → 54,000
    assert cost_for_minutes(6000) == Decimal("54000.0000")
    # 12000 → 10000–20000 bracket @ ₹8.5 → 1,02,000
    assert cost_for_minutes(12000) == Decimal("102000.0000")


def test_flat_rate_brackets_upper_inclusive():
    # boundary values land in the LOWER bracket (≤ bound)
    assert flat_rate_for_minutes(1) == Decimal("10")
    assert flat_rate_for_minutes(1000) == Decimal("10")
    assert flat_rate_for_minutes(1001) == Decimal("9.5")
    assert flat_rate_for_minutes(5000) == Decimal("9.5")
    assert flat_rate_for_minutes(5001) == Decimal("9")
    assert flat_rate_for_minutes(10000) == Decimal("9")
    assert flat_rate_for_minutes(10001) == Decimal("8.5")
    assert flat_rate_for_minutes(20000) == Decimal("8.5")
    assert flat_rate_for_minutes(20001) == Decimal("8")
    assert flat_rate_for_minutes(25000) == Decimal("8")
    assert flat_rate_for_minutes(25001) == Decimal("5.5")
    assert flat_rate_for_minutes(1_000_000) == Decimal("5.5")


def test_cost_at_boundaries():
    assert cost_for_minutes(1000) == Decimal("10000.0000")     # 1000 @ 10
    assert cost_for_minutes(5000) == Decimal("47500.0000")     # 5000 @ 9.5
    assert cost_for_minutes(10000) == Decimal("90000.0000")    # 10000 @ 9
    assert cost_for_minutes(20000) == Decimal("170000.0000")   # 20000 @ 8.5
    assert cost_for_minutes(25000) == Decimal("200000.0000")   # 25000 @ 8
    assert cost_for_minutes(30000) == Decimal("165000.0000")   # 30000 @ 5.5 (cheaper — top bracket)


def test_cost_zero_and_negative():
    assert cost_for_minutes(0) == Decimal("0.0000")
    assert cost_for_minutes(-5) == Decimal("0.0000")


def test_cost_display_and_paise():
    assert cost_display(6000) == Decimal("54000.00")
    assert cost_paise(6000) == 5400000        # ₹54,000 → paise
    assert cost_paise(12000) == 10200000      # ₹1,02,000 → paise
    # a half-rupee rate still converts exactly: 1500 @ 9.5 = 14,250.00 → 1,425,000 paise
    assert cost_for_minutes(1500) == Decimal("14250.0000")
    assert cost_paise(1500) == 1425000


def test_slab_ladder_shape():
    ladder = slab_ladder()
    assert len(ladder) == 6
    assert ladder[0] == {"from_minute": 1, "to_minute": 1000, "rupees_per_minute": Decimal("10")}
    assert ladder[-1]["to_minute"] is None and ladder[-1]["rupees_per_minute"] == Decimal("5.5")


def test_minutes_min_is_sane():
    assert isinstance(MINUTES_MIN, int) and MINUTES_MIN >= 1


# ── Per-call usage pricing (depletes the rupee balance) ─────────────────────

def test_usage_rate_per_second_matches_user_spec():
    # rate = (slab − 0.6)/60. Pin each bracket's per-second rate to the user's
    # stated value (compared to high precision).
    cases = {
        500: "0.15666666666667",      # 0–1000 slab (₹10)
        3000: "0.14833333333333",     # 1000–5000 (₹9.5)
        7000: "0.14",                 # 5000–10000 (₹9)
        15000: "0.13166666666667",    # 10000–20000 (₹8.5)
        22000: "0.12333333333333",    # 20000–25000 (₹8)
        30000: "0.081666666666667",   # 25000+ (₹5.5)
    }
    for bundle, expected in cases.items():
        rate = usage_rate_per_second(bundle)
        assert abs(rate - Decimal(expected)) < Decimal("1e-12"), (bundle, rate)


def test_per_minute_fee_full_minute_equals_slab():
    # A full 60s call = the slab's per-minute price (0.6×1 + rate*60 == slab).
    assert PER_MINUTE_FEE == Decimal("0.6")
    for bundle in (500, 3000, 7000, 15000, 22000, 30000):
        cost60 = call_usage_cost(60, bundle)
        slab = flat_rate_for_minutes(bundle)
        assert abs(cost60 - slab) < Decimal("0.01"), (bundle, cost60, slab)


def test_per_minute_fee_recurs_every_started_minute():
    # The ₹0.6 base fee applies per STARTED minute (ceil), not once per call.
    # 5000–10000 bundle (₹9, rate 0.14/s).
    # 90s → 2 started minutes → 0.6×2 + 0.14×90 = 1.2 + 12.6 = ₹13.80.
    assert call_usage_cost(90, 7000) == Decimal("13.8000")
    # 120s → 2 minutes exactly → 0.6×2 + 0.14×120 = 1.2 + 16.8 = ₹18.00 (= 2×slab).
    assert call_usage_cost(120, 7000) == Decimal("18.0000")
    # 121s → rolls into a 3rd minute → 0.6×3 + 0.14×121 = 1.8 + 16.94 = ₹18.74.
    assert call_usage_cost(121, 7000) == Decimal("18.7400")


def test_validate_minutes_strict_bounds():
    """Every purchase (onboarding + top-up) passes through _validate_minutes — a
    positive int in [MINUTES_MIN, _MINUTES_MAX]. This is the server-side gate that
    makes the flat-bracket price authoritative (the client can't buy 0 / negative /
    absurd quantities)."""
    from fastapi import HTTPException
    from app.api.nokvo_one_payments import _MINUTES_MAX, _validate_minutes

    assert _validate_minutes(MINUTES_MIN) == MINUTES_MIN
    assert _validate_minutes(6000) == 6000
    assert _validate_minutes(_MINUTES_MAX) == _MINUTES_MAX
    for bad in (0, -100, MINUTES_MIN - 1, _MINUTES_MAX + 1, "x", None):
        try:
            _validate_minutes(bad)
            assert False, f"expected 400 for {bad!r}"
        except HTTPException as e:
            assert e.status_code == 400


def test_strict_credit_is_minutes_times_flat_rate():
    """The credited rupees for a purchase are ALWAYS minutes × the flat bracket
    rate, computed server-side — so the amount can't be inflated client-side.
    cost_paise(minutes) is what the order is created for AND re-checked at verify."""
    for n in (100, 1000, 6000, 12000, 25001):
        assert cost_for_minutes(n) == Decimal(n) * flat_rate_for_minutes(n)
        # the paise amount the order is created + verified against
        assert cost_paise(n) == int((cost_for_minutes(n)) * 100)


def test_call_usage_cost_examples():
    # Sub-minute calls are unchanged (1 started minute).
    # 0–1000 bundle: a 30-second call = 0.6 + 0.15666…×30 ≈ ₹5.30.
    c = call_usage_cost(30, 500)
    assert abs(c - Decimal("5.3000")) < Decimal("0.01")
    # never connected (0s) → ₹0, no fee.
    assert call_usage_cost(0, 500) == Decimal("0.0000")
    assert call_usage_cost(-3, 500) == Decimal("0.0000")
    # a 1-second connected call still pays one minute's base fee + 1s.
    assert call_usage_cost(1, 7000) == Decimal("0.7400")  # 0.6 + 0.14


# ── NOKVO APEX: Call Credits wallet + selling-rate per-call deduction ─────────

def test_apex_wallet_credit_is_cost_times_1_5():
    # The user's worked example: select 25001 → bill 137,505.5 → credit 206,258.25.
    assert apex_wallet_credit(25001) == Decimal("206258.2500")
    # round example: 25000 @ ₹8 = 200,000 → credit 300,000.
    assert apex_wallet_credit(25000) == Decimal("300000.0000")
    # 6000 @ ₹9 = 54,000 → credit 81,000.
    assert apex_wallet_credit(6000) == Decimal("81000.0000")
    assert apex_wallet_credit(0) == Decimal("0.0000")


def test_apex_call_cost_selling_rate_fee_once():
    assert APEX_CONNECT_FEE == Decimal("1.5")
    # 25001+ bundle (₹5.5 slab) → the user's 1.50 + sec×4/60.
    assert apex_call_cost(60, 25001) == Decimal("5.5000")   # full minute == slab
    assert apex_call_cost(30, 25001) == Decimal("3.5000")   # 1.5 + 30×4/60
    assert apex_call_cost(12, 25001) == Decimal("2.3000")   # dealbreaker cut: 1.5 + 12×4/60
    # Fee charged ONCE per call (NOT per started minute): 90s = 1.5 + 90×4/60 = 7.5 (not 9).
    assert apex_call_cost(90, 25001) == Decimal("7.5000")
    # never connected → 0 (no fee for a call that didn't connect).
    assert apex_call_cost(0, 25001) == Decimal("0.0000")
    assert apex_call_cost(-3, 25001) == Decimal("0.0000")


def test_apex_60s_call_equals_slab_every_bracket():
    # A full 60s APEX call always costs exactly the bundle's slab ("one slab-minute").
    for bundle in (500, 3000, 7000, 15000, 22000, 30000):
        assert apex_call_cost(60, bundle) == flat_rate_for_minutes(bundle)


def test_apex_estimated_minutes_reconciles_with_credited():
    # At full balance, estimated minutes (credits ÷ slab) == the advertised credited
    # minutes — so the dashboard's two figures always agree.
    for n in (1000, 6000, 25001, 30000):
        wallet = apex_wallet_credit(n)
        assert int(wallet / flat_rate_for_minutes(n)) == credited_minutes(n)
