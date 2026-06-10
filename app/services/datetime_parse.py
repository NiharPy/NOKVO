"""Canonical date/time parsing facade for the voice slot engines.

Booking historically had THREE date/time code paths:
  * ``NokvoOneVoicePipeline._parse_appointment_date/_parse_appointment_time``
    (the hardened one — weekday/"next"/relative/ordinal/spoken-time/midnight
    fixes from the diabolical-case hardening pass),
  * ``voice_turn_policy._parse_slot_date/_parse_slot_time`` (a lighter, partially
    buggy duplicate — e.g. ``midnight`` resolved to 7 PM),
  * the date/time substring matchers in ``extract_turn_entities``.

A fix to one didn't fix the others — which is exactly how the harness found
inconsistent date/time bugs. This module makes the hardened pipeline parser the
**single source of truth**: everything else delegates here. ``try_*`` are the
non-raising form the slot/past-time checks want; ``parse_*`` raise the canonical
``DateTimeParseError`` (which the appointment handler already catches, since
``_AppointmentToolInputError`` is aliased to it).

The pipeline parser is imported lazily (function-local) to avoid an import
cycle (pipeline → voice_turn_policy → datetime_parse → pipeline). Stage 4 of the
unification will relocate the implementation here outright; for now this is the
behaviour-preserving seam.
"""
from __future__ import annotations

import datetime as _dt
from typing import Optional


class DateTimeParseError(ValueError):
    """Raised when a date/time can't be parsed clearly. Carries the slot + a
    caller-facing clarification + which field(s) to clear, matching the legacy
    ``_AppointmentToolInputError`` contract so the appointment handler's
    ``except`` clauses keep working unchanged."""

    def __init__(self, slot: str, answer: str, *, clear_time: bool = False, clear_date: bool = False):
        super().__init__(answer)
        self.slot = slot
        self.answer = answer
        self.clear_time = clear_time
        self.clear_date = clear_date


def parse_date(text, *, now: Optional[_dt.datetime] = None) -> _dt.date:
    """Parse a spoken/typed date to a local date. Raises on failure.

    Single source of truth = the hardened pipeline parser.
    """
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    return NokvoOneVoicePipeline._parse_appointment_date(text, now=now)


def parse_time(text) -> _dt.time:
    """Parse a spoken/typed time to a local time. Raises on failure."""
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    return NokvoOneVoicePipeline._parse_appointment_time(text)


def try_parse_date(text, *, now: Optional[_dt.datetime] = None) -> Optional[_dt.date]:
    """Non-raising :func:`parse_date` — returns ``None`` for vague/unparseable
    input (the form the slot-fill + past-time checks want)."""
    if not text:
        return None
    try:
        return parse_date(text, now=now)
    except Exception:
        return None


def try_parse_time(text) -> Optional[_dt.time]:
    """Non-raising :func:`parse_time`."""
    if not text:
        return None
    try:
        return parse_time(text)
    except Exception:
        return None
