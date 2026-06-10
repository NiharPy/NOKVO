"""Unit tests for the Follow-Up Pipeline pure logic.

The DB-query assembly in ``pipeline_for_tenant`` is exercised by the live API
smoke test; here we lock down the two bug-prone pure helpers it relies on:
  - ``_due_bucket`` — overdue folds into "today"; tomorrow / this_week / later
    boundaries are correct.
  - ``_conversion_rate`` — ratio with a divide-by-zero guard.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.followup_scheduler_service import FollowupSchedulerService as S


# A fixed "now" mid-day so day-boundary math is unambiguous.
NOW = datetime(2026, 6, 8, 14, 0, tzinfo=timezone.utc)


def test_due_bucket_overdue_folds_into_today():
    assert S._due_bucket(NOW - timedelta(days=3), NOW) == "today"   # overdue
    assert S._due_bucket(NOW - timedelta(hours=2), NOW) == "today"  # overdue earlier today
    assert S._due_bucket(NOW + timedelta(hours=2), NOW) == "today"  # later today


def test_due_bucket_tomorrow_this_week_later():
    tomorrow = (NOW + timedelta(days=1)).replace(hour=10)
    assert S._due_bucket(tomorrow, NOW) == "tomorrow"
    assert S._due_bucket(NOW + timedelta(days=3), NOW) == "this_week"
    assert S._due_bucket(NOW + timedelta(days=5), NOW) == "this_week"
    # this_week upper bound is day_start + 7d (June 15 00:00); from a 14:00
    # "now" that's NOW + 6d10h, so NOW + 8d is firmly "later".
    assert S._due_bucket(NOW + timedelta(days=8), NOW) == "later"


def test_conversion_rate_math_and_zero_guard():
    assert S._conversion_rate(18, 50) == 0.36
    assert S._conversion_rate(0, 0) == 0.0     # no divide-by-zero
    assert S._conversion_rate(0, 12) == 0.0
    assert S._conversion_rate(3, 3) == 1.0
