"""Budget parsing helpers for real-estate lead routing.

The voice layer stores the caller-facing value ("80 lakhs") and a numeric
companion in rupees. This module keeps the parsing rules in one place so
memory extraction, tool records, and assignment escalation compare the same
number.
"""

from __future__ import annotations

import re

_NUMBER_RE = re.compile(r"(?<!\w)(\d+(?:,\d+)*(?:\.\d+)?)(?!\w)")
_AMOUNT_WITH_UNIT_RE = re.compile(
    r"(?<!\w)"
    r"(?:rs\.?|inr|₹|rupees?)?\s*"
    r"(\d+(?:,\d+)*(?:\.\d+)?)\s*"
    r"(crores?|cr|lakhs?|lacs?|l|thousands?|k)?"
    r"(?!\w)",
    re.IGNORECASE,
)

_UNIT_MULTIPLIERS = {
    "crore": 10_000_000,
    "crores": 10_000_000,
    "cr": 10_000_000,
    "lakh": 100_000,
    "lakhs": 100_000,
    "lac": 100_000,
    "lacs": 100_000,
    "l": 100_000,
    "thousand": 1_000,
    "thousands": 1_000,
    "k": 1_000,
}


def _parse_number(value: str) -> float | None:
    try:
        return float(value.replace(",", ""))
    except (TypeError, ValueError):
        return None


def normalize_budget_to_rupees(value: str | int | float | None) -> int | None:
    """Return an integer rupee amount from common Indian budget phrases.

    Examples: ``"1.2 crore"`` -> ``12000000``, ``"80 lakhs"`` ->
    ``8000000``, ``"7500000"`` -> ``7500000``. Small bare numbers such as
    ``"80"`` are intentionally ignored because they are ambiguous without a
    unit.
    """

    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        amount = float(value)
        return int(round(amount)) if amount >= 10_000 else None

    text = str(value or "").strip().lower()
    if not text:
        return None

    for match in _AMOUNT_WITH_UNIT_RE.finditer(text):
        number = _parse_number(match.group(1))
        if number is None:
            continue
        unit = (match.group(2) or "").lower()
        if unit:
            return int(round(number * _UNIT_MULTIPLIERS[unit]))

    # Bare currency amounts are accepted only once they are large enough to be
    # a plausible rupee value. This avoids interpreting "budget 80" as Rs 80.
    for match in _NUMBER_RE.finditer(text):
        number = _parse_number(match.group(1))
        if number is not None and number >= 10_000:
            return int(round(number))

    return None


def format_rupees_short(rupees: int | float | None) -> str | None:
    """Render rupees as a compact Indian budget label."""

    if rupees is None:
        return None
    try:
        amount = int(round(float(rupees)))
    except (TypeError, ValueError):
        return None
    if amount <= 0:
        return None

    if amount >= 10_000_000:
        value = amount / 10_000_000
        unit = "crore"
    elif amount >= 100_000:
        value = amount / 100_000
        unit = "lakhs"
    elif amount >= 1_000:
        value = amount / 1_000
        unit = "thousand"
    else:
        return f"Rs {amount}"

    label = f"{value:.2f}".rstrip("0").rstrip(".")
    return f"{label} {unit}"
