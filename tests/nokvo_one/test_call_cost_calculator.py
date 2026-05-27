"""Precision unit tests for the ₹8/minute call-cost calculator.

The tariff is ₹8 / minute, which is the recurring fraction ₹2/15 per
second. A naive float pipeline drifts by paise over an hour; the test
suite locks the Decimal contract so that drift can never silently come
back. We also exercise the type-coercion paths because the production
callers pass ints, floats, Decimals, and timedeltas interchangeably.
"""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from app.services.call_cost_calculator import (
    RUPEES_PER_MINUTE,
    RUPEES_PER_SECOND,
    CostBreakdown,
    format_rupees,
    rupees_display,
    rupees_for,
    sum_rupees,
)


def test_per_second_rate_is_exact_decimal_fraction() -> None:
    """₹8/60 must stay as the precise recurring fraction — not a float."""
    assert RUPEES_PER_MINUTE == Decimal("8")
    # 8/60 → 0.1333... (recurring). Decimal preserves it at full context
    # precision; the rounded paise display is a separate concern.
    assert RUPEES_PER_SECOND == Decimal("8") / Decimal("60")


def test_one_second_quantises_to_four_decimals() -> None:
    assert rupees_for(1) == Decimal("0.1333")


def test_one_minute_is_exactly_eight_rupees() -> None:
    """The headline tariff. If this drifts the bill is wrong."""
    assert rupees_for(60) == Decimal("8.0000")


def test_one_hour_does_not_drift() -> None:
    """The float bug case. ``8/60 * 3600`` returns 479.9999... — Decimal
    must yield ₹480.0000 flat."""
    assert rupees_for(3600) == Decimal("480.0000")


def test_subsecond_durations_bill_fractionally() -> None:
    """Half a second bills half a second — we don't round up to a full
    second or to a full minute."""
    assert rupees_for(Decimal("0.5")) == Decimal("0.0667")


def test_zero_duration_is_zero_rupees() -> None:
    assert rupees_for(0) == Decimal("0.0000")


def test_negative_duration_raises() -> None:
    with pytest.raises(ValueError):
        rupees_for(-1)


def test_timedelta_accepted() -> None:
    assert rupees_for(timedelta(minutes=2, seconds=30)) == Decimal("20.0000")


def test_float_routed_via_str_no_binary_noise() -> None:
    """``Decimal(0.1)`` is 0.10000000000000000555... — the calculator
    must route floats via str() so binary-float artefacts never leak
    into the ledger value."""
    # 0.1 seconds → 0.1 × (8/60) = 0.01333... → quantises to 0.0133
    assert rupees_for(0.1) == Decimal("0.0133")


def test_bool_rejected_explicitly() -> None:
    """``bool`` is an int subclass; a stray ``True`` must not bill ₹0.13."""
    with pytest.raises(TypeError):
        rupees_for(True)  # type: ignore[arg-type]


def test_display_quantises_to_two_decimals() -> None:
    assert rupees_display(Decimal("0.1333")) == Decimal("0.13")
    assert rupees_display(60) == Decimal("8.00")
    assert rupees_display(3600) == Decimal("480.00")


def test_format_rupees_uses_rupee_glyph() -> None:
    assert format_rupees(60) == "₹8.00"
    assert format_rupees(1) == "₹0.13"


def test_breakdown_carries_full_precision_and_display() -> None:
    cb = CostBreakdown.for_duration(90)
    # 1.5 minutes → ₹12 flat.
    assert cb.seconds == Decimal("90")
    assert cb.rupees == Decimal("12.0000")
    assert cb.rupees_display_value == Decimal("12.00")
    assert cb.rupees_str == "₹12.00"


def test_breakdown_subsecond_preserves_tail() -> None:
    cb = CostBreakdown.for_duration(Decimal("1.5"))
    # 1.5s × ₹2/15 = ₹0.2 → ledger 0.2000, display 0.20
    assert cb.rupees == Decimal("0.2000")
    assert cb.rupees_str == "₹0.20"


def test_sum_rupees_no_drift_over_many_short_calls() -> None:
    """100 × 1-second calls must sum to exactly 100 × ₹0.1333 = ₹13.33
    (not 13.32 from per-row paise rounding)."""
    ledger = [rupees_for(1) for _ in range(100)]
    assert sum_rupees(ledger) == Decimal("13.3300")
    assert rupees_display(sum_rupees(ledger)) == Decimal("13.33")


def test_sum_rupees_skips_none() -> None:
    assert sum_rupees([Decimal("1.0000"), None, Decimal("2.0000")]) == Decimal(
        "3.0000"
    )
