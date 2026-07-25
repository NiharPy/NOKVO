"""Prepaid-minute pricing — FLAT-BY-BRACKET tariff.

A customer buys a bundle of voice minutes up front. Unlike the post-paid
``call_cost_calculator`` (which prices each consumed minute MARGINALLY by the
org's month-to-date usage), the prepaid purchase is **flat by bracket**: the
WHOLE quantity bills at the single rate of the bracket the quantity lands in.

    buy 6,000 min  → 6,000 falls in the 5,000–10,000 bracket → ₹9/min   → ₹54,000
    buy 12,000 min → 12,000 falls in the 10,000–20,000 bracket → ₹8.5/min → ₹1,02,000

Brackets are UPPER-INCLUSIVE (≤ the bound):
    ≤ 1,000   → ₹10/min
    ≤ 5,000   → ₹9.5/min
    ≤ 10,000  → ₹9/min
    ≤ 20,000  → ₹8.5/min
    ≤ 25,000  → ₹8/min
    > 25,000  → ₹5.5/min

Each PURCHASE is priced independently on its own quantity (a top-up of 6,000 is
priced as a fresh 6,000, not as minutes 6,001–12,000 of a running total). Money
math is ``Decimal`` end-to-end (no float artefacts); we reuse the ledger/display
quantisation templates from :mod:`call_cost_calculator` so amounts round the same
way across the platform.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP

from app.services.call_cost_calculator import _DISPLAY_Q, _LEDGER_Q, minutes_for_seconds

# (inclusive_upper_minute_bound, rupees_per_minute); the last bound is None
# (open-ended). The whole purchased quantity bills at the matched rate.
PREPAID_SLABS: list[tuple[int | None, Decimal]] = [
    (1000, Decimal("10")),
    (5000, Decimal("9.5")),
    (10000, Decimal("9")),
    (20000, Decimal("8.5")),
    (25000, Decimal("8")),
    (None, Decimal("5.5")),
]

# Smallest bundle a customer may buy (onboarding + top-up). Keeps the cheapest
# purchase meaningful and avoids ₹0 / 1-minute orders.
MINUTES_MIN = 100


def flat_rate_for_minutes(minutes: int) -> Decimal:
    """The single ₹/min rate the WHOLE bundle bills at, by which bracket
    ``minutes`` lands in (upper-inclusive). ``minutes <= 0`` returns the first
    (highest) rate — callers should validate against ``MINUTES_MIN`` first."""
    n = int(minutes)
    for upper, rate in PREPAID_SLABS:
        if upper is None or n <= upper:
            return rate
    return PREPAID_SLABS[-1][1]


def cost_for_minutes(minutes: int) -> Decimal:
    """Ledger rupee cost of buying ``minutes`` at its flat bracket rate
    (``minutes × rate``), quantised to 4 dp ROUND_HALF_UP. Non-positive → ₹0."""
    n = max(int(minutes), 0)
    if n == 0:
        return Decimal("0").quantize(_LEDGER_Q)
    return (Decimal(n) * flat_rate_for_minutes(n)).quantize(_LEDGER_Q, rounding=ROUND_HALF_UP)


def cost_display(minutes: int) -> Decimal:
    """Customer-facing 2-dp rupees for the bundle."""
    return cost_for_minutes(minutes).quantize(_DISPLAY_Q, rounding=ROUND_HALF_UP)


# NOKVO APEX bonus: the customer is BILLED on the selected minutes (at the flat
# slab rate, ``cost_for_minutes``) but CREDITED 50% extra minutes at no extra
# cost. e.g. select 25001 → bill 25001×₹5.5 = ₹137,505.5, credit floor(25001×1.5)
# = 37,501 min. Integer math (×3//2) so a half-minute is dropped, never rounded
# up — the credit is never more generous than 1.5×.
def credited_minutes(selected_minutes: int) -> int:
    """Minutes CREDITED to an APEX account for a purchase of ``selected_minutes``
    (the billed quantity): ``floor(selected × 1.5)``.

    NOTE: APEX now holds a Call CREDITS wallet (rupee-valued), not a minutes
    balance — this stays only for the DISPLAY "≈N minutes". It equals
    ``floor(apex_wallet_credit(selected) / slab_rate)`` so the credits figure and
    the minutes figure reconcile on screen."""
    n = max(int(selected_minutes), 0)
    return (n * 3) // 2


def apex_wallet_credit(selected_minutes: int) -> Decimal:
    """Call Credits granted for an APEX purchase of ``selected_minutes`` — the
    amount BILLED (``cost_for_minutes``) plus the 50% bonus, i.e. ``cost × 1.5``.
    The customer is charged ``cost_for_minutes`` (real ₹ via Razorpay) but the
    wallet receives 1.5× that in spendable Call Credits (1 credit ≡ ₹1).
    Quantised to the 4-dp ledger.

    e.g. select 25001 → bill ₹137,505.5 → credit 206,258.25 Call Credits."""
    return (cost_for_minutes(selected_minutes) * Decimal("1.5")).quantize(
        _LEDGER_Q, rounding=ROUND_HALF_UP
    )


def cost_paise(minutes: int) -> int:
    """Integer paise for Razorpay (amount fields are paise). The bundle cost is a
    whole-rupee-or-half product of int minutes × a ≤1-dp rate, so the 2-dp
    display value converts to paise exactly."""
    return int((cost_display(minutes) * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))


# ── Per-call USAGE pricing (depletes the prepaid rupee balance) ──────────────
# A CONNECTED inbound / non-deterministic-outbound call costs a flat connection
# fee PLUS a per-second rate. The per-second rate is derived from the SAME flat
# bracket the call's bundle was bought at:
#     rate_per_second = (slab_rupees_per_minute − PER_MINUTE_FEE) / 60
# so a full 60-second call costs exactly the slab's per-minute price
# (0.6 + rate×60 == slab). The ₹0.6 base fee applies PER STARTED MINUTE (ceil),
# NOT once per call — a 90s call runs into a 2nd minute, so it pays 0.6×2 + the
# talk seconds. The bracket is chosen by the bundle the rupees are spent from
# (FIFO across purchases). Deterministic / bulk-questionnaire outbound calls do
# NOT deplete this balance.
PER_MINUTE_FEE = Decimal("0.6")
# Back-compat alias (was named CONNECTION_FEE when it was once-per-call).
CONNECTION_FEE = PER_MINUTE_FEE


def usage_rate_per_second(bundle_minutes: int) -> Decimal:
    """Per-second talk rate for a call billed against a bundle of
    ``bundle_minutes`` (its flat bracket): ``(slab − 0.6) / 60``. Full precision
    (no quantize) — the caller quantises the final call cost."""
    return (flat_rate_for_minutes(bundle_minutes) - PER_MINUTE_FEE) / Decimal("60")


def call_usage_cost(seconds, bundle_minutes: int) -> Decimal:
    """Rupee cost of ONE connected call:
        ``0.6 × started_minutes + rate × seconds``
    where ``started_minutes = ceil(seconds / 60)`` and ``rate`` is set by
    ``bundle_minutes``' bracket. The ₹0.6 base fee recurs EVERY started minute
    (so a 90s call = 0.6×2 + talk), while a full 60s call still equals the slab
    (0.6×1 + rate×60 == slab). Quantised to the 4-dp ledger. A non-positive
    duration (never connected) costs ₹0 — no fee for a call that didn't connect."""
    s = Decimal(str(seconds))
    if s <= 0:
        return Decimal("0").quantize(_LEDGER_Q)
    started_minutes = minutes_for_seconds(s)  # ceil(seconds / 60)
    cost = PER_MINUTE_FEE * Decimal(started_minutes) + usage_rate_per_second(bundle_minutes) * s
    return cost.quantize(_LEDGER_Q, rounding=ROUND_HALF_UP)


# ── NOKVO APEX per-call USAGE (depletes the Call Credits wallet) ─────────────
# APEX is the deterministic-outbound product; EVERY connected call (incl. the
# questionnaire) bills the Call Credits wallet at the SELLING rate. Unlike the
# Nokvo One usage above, the connection fee is charged ONCE per connected call
# (NOT per started minute):
#     cost = APEX_CONNECT_FEE + (slab − APEX_CONNECT_FEE)/60 × seconds
# so a full 60s call costs exactly the bundle's slab (one "slab-minute"), and a
# 30s call at the ₹5.5 slab costs 1.5 + 30×(4/60) = 3.5 credits; a 90s call costs
# 1.5 + 90×(4/60) = 7.5 (the fee does NOT recur). The slab is the bracket the
# bundle was BOUGHT at (FIFO), so usable-minutes ≈ credited-minutes.
APEX_CONNECT_FEE = Decimal("1.5")


def apex_usage_rate_per_second(bundle_minutes: int) -> Decimal:
    """APEX per-second talk rate for a call billed against a bundle of
    ``bundle_minutes`` (its flat bracket): ``(slab − 1.5) / 60`` Call Credits.
    Full precision (no quantize) — the caller quantises the final call cost."""
    return (flat_rate_for_minutes(bundle_minutes) - APEX_CONNECT_FEE) / Decimal("60")


def apex_call_cost(seconds, bundle_minutes: int) -> Decimal:
    """Call Credits consumed by ONE connected APEX call:
        ``1.5 + rate × seconds``
    where ``rate`` is set by ``bundle_minutes``' bracket and the 1.5 connect fee
    is charged ONCE (not per minute). A full 60s call equals the slab exactly; a
    non-positive duration (never connected) costs 0 — no fee for a call that
    didn't connect. Quantised to the 4-dp ledger."""
    s = Decimal(str(seconds))
    if s <= 0:
        return Decimal("0").quantize(_LEDGER_Q)
    cost = APEX_CONNECT_FEE + apex_usage_rate_per_second(bundle_minutes) * s
    return cost.quantize(_LEDGER_Q, rounding=ROUND_HALF_UP)


def apex_call_cost_at_rate(seconds, rate) -> Decimal:
    """PLAN-DRIVEN APEX call cost (NOKVO APEX plans — Core/Growth/Pinnacle/Enterprise).

    Same connect-fee shape as :func:`apex_call_cost`, but the per-minute ``rate`` is the
    org's FIXED plan rate (``apex_plans.get_apex_rate``), not the quantity slab:
        ``1.5 + max(rate − 1.5, 0)/60 × seconds``
    A full 60s call equals the plan rate exactly. The ``max(…, 0)`` clamps the talk rate at
    0 so a rate below the ₹1.5 connect fee can never produce a negative per-second charge
    (``stamp_org_from_plan`` also refuses to stamp a sub-connect-fee rate). A non-positive
    duration (never connected) costs 0. Quantised to the 4-dp ledger."""
    s = Decimal(str(seconds))
    if s <= 0:
        return Decimal("0").quantize(_LEDGER_Q)
    per_second = max(Decimal(str(rate)) - APEX_CONNECT_FEE, Decimal("0")) / Decimal("60")
    cost = APEX_CONNECT_FEE + per_second * s
    return cost.quantize(_LEDGER_Q, rounding=ROUND_HALF_UP)


def slab_ladder() -> list[dict]:
    """The published bracket ladder, for the onboarding/top-up UI fine print."""
    rows: list[dict] = []
    lower = 0
    for upper, rate in PREPAID_SLABS:
        rows.append(
            {
                "from_minute": lower + 1,
                "to_minute": upper,  # None = open-ended
                "rupees_per_minute": rate,
            }
        )
        if upper is None:
            break
        lower = upper
    return rows
