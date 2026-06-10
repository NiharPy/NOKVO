"""S0 of the booking-engine unification: the slot parsers now delegate to the
single canonical date/time parser (datetime_parse → hardened pipeline parser).

These pin that the previously-divergent voice_turn_policy slot parsers now match
the hardened behaviour (so a fix in one place fixes both paths)."""
from __future__ import annotations

import datetime as dt

from app.services import voice_turn_policy as vtp
from app.services.datetime_parse import DateTimeParseError, try_parse_date, try_parse_time
from app.services.nokvo_one_voice_pipeline import _AppointmentToolInputError


def test_appointment_error_is_canonical_subclass():
    # So the appointment handler's `except _AppointmentToolInputError` still
    # catches errors raised by the canonical parser.
    assert issubclass(_AppointmentToolInputError, DateTimeParseError)


def test_facade_matches_hardened_behaviour():
    assert try_parse_time("midnight") is None          # was the 19:00 substring bug
    assert try_parse_time("11") == dt.time(11, 0)       # daytime inference
    assert try_parse_time("half past 4") == dt.time(16, 30)
    assert try_parse_date("next week") is None          # vague → stays a lead


def test_slot_parsers_now_use_canonical_logic(monkeypatch):
    # Freeze "now" to a Monday so weekday math is deterministic.
    fake = dt.datetime(2026, 6, 8, 12, 0, tzinfo=vtp._APPOINTMENT_LOCAL_TZ)

    class _Frozen(dt.datetime):
        @classmethod
        def now(cls, tz=None):
            return fake.astimezone(tz) if tz else fake.replace(tzinfo=None)

    monkeypatch.setattr(vtp, "datetime", _Frozen)

    # Upgraded vs the old lighter duplicate:
    assert vtp._parse_slot_date("monday") == dt.date(2026, 6, 15)   # next Monday, not today
    assert vtp._parse_slot_date("the 15th") == dt.date(2026, 6, 15)  # bare day-of-month
    assert vtp._parse_slot_date("next week") is None                 # vague
    assert vtp._parse_slot_time("midnight") is None                  # was 7 PM bug
    assert vtp._parse_slot_time("5:30 pm") == dt.time(17, 30)
