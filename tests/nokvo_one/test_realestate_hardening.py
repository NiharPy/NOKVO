"""Regression tests for the real-estate diabolical-case hardening.

Each test pins a bug found by the adversarial harness so it can't regress.
"""
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace as N

import pytest

from app.services.voice_turn_policy import normalize_phone_number, extract_turn_entities
from app.services.tool_flow_policy import _extract_value, _parse_budget_amount
from app.services.real_estate_project_service import find_project_match
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline as P
from app.services.language_intent import detect_language_switch

# Fixed "now" = Mon 8 Jun 2026 (IST), so weekday math is deterministic.
NOW = dt.datetime(2026, 6, 8, 10, 0, tzinfo=dt.timezone.utc)


def _d(v):
    return P._parse_appointment_date(v, now=NOW).isoformat()


def _t(v):
    return P._parse_appointment_time(v).isoformat()


# ── Date parser ─────────────────────────────────────────────────────────────


def test_weekday_never_books_today_and_handles_next():
    # Today is Monday → "monday"/"next monday" must be the UPCOMING Monday.
    assert _d("monday") == "2026-06-15"
    assert _d("next monday") == "2026-06-15"
    # A non-today weekday is this week.
    assert _d("by friday") == "2026-06-12"
    # Word-boundary: "mondayish" must NOT resolve to today.
    with pytest.raises(Exception):
        _d("mondayish")


def test_relative_and_ordinal_dates():
    assert _d("this weekend") == "2026-06-13"   # upcoming Saturday
    assert _d("in 2 days") == "2026-06-10"
    assert _d("15th") == "2026-06-15"            # bare day-of-month
    assert _d("the 1st") == "2026-07-01"         # next occurrence (June 1 passed)
    assert _d("first of july") == "2026-07-01"   # word ordinal + month
    assert _d("2026-06-20") == "2026-06-20"      # ISO still works


def test_vague_dates_still_fail_so_they_stay_leads():
    for vague in ("next week", "end of the month", "sometime", "morning"):
        with pytest.raises(Exception):
            _d(vague)


# ── Time parser ─────────────────────────────────────────────────────────────


def test_midnight_no_longer_maps_to_evening():
    with pytest.raises(Exception):
        _t("midnight")  # used to silently become 19:00 via "night" substring


def test_spoken_and_bare_times():
    assert _t("11") == "11:00:00"        # bare hour → daytime AM inference
    assert _t("11 am") == "11:00:00"
    assert _t("half past 4") == "16:30:00"
    assert _t("quarter to 5") == "16:45:00"
    assert _t("5ish") == "17:00:00"
    assert _t("12") == "12:00:00"        # noon


# ── Budget ───────────────────────────────────────────────────────────────────


def test_budget_units_to_rupees():
    assert _parse_budget_amount("around 1.5 cr") == 15000000.0
    assert _parse_budget_amount("50 lakhs to a crore") == 10000000.0  # max = 1 crore
    assert _parse_budget_amount("80 lakh") == 8000000.0
    assert _parse_budget_amount("not too expensive") == "not too expensive"
    assert _extract_value("1.5 crore", "budget", "budget") == 15000000.0


# ── Phone ────────────────────────────────────────────────────────────────────


def test_phone_strict_under_expected():
    assert normalize_phone_number("9876543210", expected=True) == "9876543210"
    assert normalize_phone_number("0 98765 43210", expected=True) == "9876543210"  # trunk 0
    assert normalize_phone_number("+91 98765 43210", expected=True) == "+919876543210"
    # STT mangled (9 digits / dropped digit) must NOT be accepted silently.
    assert normalize_phone_number("98765 4321", expected=True) is None
    assert normalize_phone_number("1234567", expected=True) is None


# ── Project matching (STT robustness) ────────────────────────────────────────


def test_project_match_despaced():
    projects = [N(id="p1", name="Skyline Heights"), N(id="p2", name="Green Acres")]
    assert find_project_match(projects, project_name="sky line heights").name == "Skyline Heights"
    assert find_project_match(projects, project_name="Greenacres").name == "Green Acres"


# ── Entity extraction (combined date+time in one turn) ───────────────────────


def test_tomorrow_at_eleven_yields_both():
    ent = extract_turn_entities("tomorrow at 11")
    assert ent.get("date_text") and ent.get("time_text")


# ── Language switch precision ────────────────────────────────────────────────


def test_language_switch_ignores_descriptive_mention():
    assert detect_language_switch("I speak Telugu at home but continue") is None
    assert detect_language_switch("please speak in telugu") == "te"
    assert detect_language_switch("switch to tamil") == "ta"
