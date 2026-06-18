"""Precision call-cost calculator (₹8 / minute).

Tariff
------
The platform bills ₹8 per minute of conversation. That works out to an
**exact** per-second rate of ``₹2 / 15`` ≈ ``₹0.1333…``. The user-facing
shorthand ("₹0.13 per second") is a rounded display — the *actual* charge
must use the precise fraction so 1-hour calls don't drift by paise.

  rate_per_second = ₹8 / 60 = ₹2 / 15 = 0.13333... (recurring)

Why Decimal (and not float)
---------------------------
Floats can't represent 0.1 exactly, let alone 2/15. A naive
``8/60 * 3600`` returns ``479.99999999999994`` and the receipt shows
``₹479.99`` for a 1-hour call that should bill ``₹480.00`` flat. We use
``Decimal`` end-to-end with a 28-digit context (Python's default) and an
explicit per-call ``ROUND_HALF_UP`` quantisation at storage time.

Rounding policy
---------------
- **Internal arithmetic**: full Decimal precision, no rounding.
- **Stored ``rupees``** (the audit / billing value): quantised to 4 dp
  with ``ROUND_HALF_UP``. Four decimals preserve the ⅓-paise tail without
  inflating column width and let us aggregate thousands of calls before
  cumulative round-off drifts a paise. The ledger of truth is the
  ``rupees`` column on ``call_costs``; aggregate views re-sum from it.
- **Display value**: quantised to 2 dp with ``ROUND_HALF_UP`` (standard
  rupees-and-paise display).

Duration policy
---------------
- Subsecond calls are billed for their actual fractional seconds, not
  rounded up to a minute — there's no point pretending a 4-second
  misdial cost a full minute, and the math handles fractions cleanly.
- We accept ``int | float | Decimal | timedelta`` and normalise to
  ``Decimal`` seconds with no float intermediates.
- Negative durations raise ``ValueError`` rather than silently coercing
  to zero — a negative duration is a logic bug upstream, not a billing
  edge case to absorb.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Union


# Legacy flat default — kept as a fallback for any caller that doesn't supply a
# tier-aware rate. The live billing path uses the tiered rates below.
RUPEES_PER_MINUTE: Decimal = Decimal("8")
RUPEES_PER_SECOND: Decimal = RUPEES_PER_MINUTE / Decimal("60")

# Tiered per-minute tariff (post-paid), selected by the org's cumulative billed
# MINUTES in the current calendar month. Each entry is
# (exclusive_upper_minute_bound, rupees_per_minute); the last bound is None
# (open-ended). Source of truth for the live billing path.
RATE_TIERS: list[tuple[int | None, Decimal]] = [
    (1000, Decimal("10")),
    (10000, Decimal("9")),
    (25000, Decimal("8")),
    (None, Decimal("6.5")),
]


def rupees_per_minute_for(cumulative_minutes: Decimal | int | float) -> Decimal:
    """Per-minute rate for the tier the month's cumulative minutes fall into."""
    m = Decimal(str(cumulative_minutes))
    for upper, rate in RATE_TIERS:
        if upper is None or m < upper:
            return rate
    return RATE_TIERS[-1][1]


def rupees_per_second_for(cumulative_minutes: Decimal | int | float) -> Decimal:
    return rupees_per_minute_for(cumulative_minutes) / Decimal("60")

# Quantisation templates. ``_LEDGER_Q`` is what gets persisted, ``_DISPLAY_Q``
# is what the UI renders. The four-decimal ledger value preserves the
# 0.1333… tail so totals re-sum precisely; the two-decimal display value is
# the bog-standard rupees-and-paise format.
_LEDGER_Q = Decimal("0.0001")
_DISPLAY_Q = Decimal("0.01")

DurationLike = Union[int, float, Decimal, timedelta]


def _to_seconds(duration: DurationLike) -> Decimal:
    """Coerce a duration argument into a Decimal number of seconds.

    ``float`` is accepted for ergonomics (``perf_counter()`` deltas are
    floats) but we convert via ``str`` so the binary-float artefacts don't
    leak into Decimal — ``Decimal(0.1)`` is ``0.1000000000000000055…``
    whereas ``Decimal(str(0.1))`` is the literal ``0.1``.
    """
    if isinstance(duration, timedelta):
        # ``total_seconds()`` returns a float; route through str.
        return Decimal(str(duration.total_seconds()))
    if isinstance(duration, Decimal):
        return duration
    if isinstance(duration, bool):
        # ``bool`` is a subclass of int — block it explicitly so a stray
        # ``True`` doesn't bill ₹0.13.
        raise TypeError("duration must be a numeric type, not bool")
    if isinstance(duration, int):
        return Decimal(duration)
    if isinstance(duration, float):
        return Decimal(str(duration))
    raise TypeError(f"duration must be int, float, Decimal, or timedelta; got {type(duration).__name__}")


def rupees_for(duration: DurationLike) -> Decimal:
    """Return the **ledger** rupee amount for a given duration.

    Quantised to 4 dp with ROUND_HALF_UP. Use this when persisting to the
    ``call_costs.rupees`` column.

    Examples
    --------
    >>> rupees_for(60)             # 1 minute
    Decimal('8.0000')
    >>> rupees_for(1)              # 1 second
    Decimal('0.1333')
    >>> rupees_for(3600)           # 1 hour
    Decimal('480.0000')
    >>> rupees_for(0)              # zero-length
    Decimal('0.0000')
    """
    seconds = _to_seconds(duration)
    if seconds < 0:
        raise ValueError(f"duration cannot be negative (got {seconds!s} seconds)")
    return (seconds * RUPEES_PER_SECOND).quantize(_LEDGER_Q, rounding=ROUND_HALF_UP)


def rupees_display(amount: Decimal | DurationLike) -> Decimal:
    """Quantise to the customer-facing 2-dp rupees-and-paise form.

    Accepts either a Decimal (an already-computed ledger amount — common
    on the read path when aggregating ``call_costs.rupees``) or any
    duration type (so callers can go from "raw seconds" to "display
    rupees" in one hop).

    >>> rupees_display(Decimal("8.0000"))
    Decimal('8.00')
    >>> rupees_display(90)              # 1.5 minutes
    Decimal('12.00')
    >>> rupees_display(Decimal("0.1333"))
    Decimal('0.13')
    """
    if isinstance(amount, Decimal):
        value = amount
    else:
        value = rupees_for(amount)
    return value.quantize(_DISPLAY_Q, rounding=ROUND_HALF_UP)


def format_rupees(amount: Decimal | DurationLike) -> str:
    """Format a cost as ``₹X.XX`` for human display.

    The ``₹`` glyph is U+20B9 (Indian Rupee Sign). Avoids the ``Rs.``
    prefix to stay consistent with the rest of the dashboard copy.
    """
    return f"₹{rupees_display(amount):.2f}"


@dataclass(frozen=True, slots=True)
class CostBreakdown:
    """A small immutable record of a single call's billing math.

    Carries the full precision side-by-side with the display value so
    the receipt / API can show both without re-running the calculation:

      * ``seconds`` — what we billed for, as Decimal.
      * ``rupees`` — ledger value (4 dp).
      * ``rupees_display`` — UI value (2 dp).
      * ``rupees_str`` — pre-formatted ``₹X.XX``.

    This is **not** the persistence model — that's ``CallCost`` in
    ``app.models.call_cost``. ``CostBreakdown`` is a calculator return
    value; the model row is built from these fields by the recorder.
    """

    seconds: Decimal
    rupees: Decimal
    rupees_display_value: Decimal
    rupees_str: str

    @classmethod
    def for_duration(cls, duration: DurationLike) -> "CostBreakdown":
        return cls.for_duration_at_rate(duration, RUPEES_PER_SECOND)

    @classmethod
    def for_duration_at_rate(cls, duration: DurationLike, rate_per_second: Decimal) -> "CostBreakdown":
        """Cost for a duration at an explicit per-second rate (the tiered path
        passes the rate chosen from the org's month-to-date minutes)."""
        seconds = _to_seconds(duration)
        if seconds < 0:
            raise ValueError(f"duration cannot be negative (got {seconds!s} seconds)")
        ledger = (seconds * rate_per_second).quantize(_LEDGER_Q, rounding=ROUND_HALF_UP)
        display = ledger.quantize(_DISPLAY_Q, rounding=ROUND_HALF_UP)
        return cls(
            seconds=seconds,
            rupees=ledger,
            rupees_display_value=display,
            rupees_str=f"₹{display:.2f}",
        )


def sum_rupees(amounts) -> Decimal:
    """Sum an iterable of ledger amounts at 4-dp precision.

    Used by the cost-summary endpoint when rolling up today's / this
    month's / all-time totals. Re-summing ledger values is exact —
    re-quantising to 2 dp per row before summing would drift.
    """
    total = Decimal("0")
    for amount in amounts:
        if amount is None:
            continue
        total += amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return total.quantize(_LEDGER_Q, rounding=ROUND_HALF_UP)
