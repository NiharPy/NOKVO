"""Retry cadence policy — and the guarantee that it ships dark.

A no-answer used to be terminal, so most of a paid-for list was never reached by
anyone. This adds a scheduled second and third attempt, which is the largest
single lift available — and also the change most capable of embarrassing a
customer if it misfires, because its failure mode is re-dialing someone who
already said no.

The SQL that stamps ``next_attempt_at`` (a CASE over a Postgres array indexed by
the row's own attempt column) is verified against a real Postgres, including the
1-based/0-based boundary and the case that matters most: an ANSWERED call must
never be scheduled for a retry. What's locked here is the policy that feeds it.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.core.config import settings
from app.services import outbound_retry as r


@pytest.fixture
def _enabled(monkeypatch):
    monkeypatch.setattr(settings, "APEX_RETRY_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(settings, "APEX_RETRY_OFFSETS_HOURS", "3,26", raising=False)


NOW = datetime(2026, 8, 14, 10, 0, tzinfo=timezone.utc)


# ── ships dark ───────────────────────────────────────────────────────────────


def test_retries_are_off_by_default():
    """The offsets cannot be chosen without a week of hangup-cause data, so the
    mechanism ships wired and inert rather than guessing."""
    assert settings.APEX_RETRY_ATTEMPTS == 1
    assert settings.APEX_RETRY_OFFSETS_HOURS == ""


def test_disabled_schedules_nothing():
    assert r.next_attempt_at(attempt=1, hangup_cause="NO_ANSWER", now=NOW) is None


def test_attempts_without_offsets_still_schedules_nothing(monkeypatch):
    """Belt and braces: raising the attempt budget alone must not start dialing
    people on a schedule nobody chose."""
    monkeypatch.setattr(settings, "APEX_RETRY_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(settings, "APEX_RETRY_OFFSETS_HOURS", "", raising=False)
    assert r.next_attempt_at(attempt=1, hangup_cause="NO_ANSWER", now=NOW) is None


# ── the cadence ──────────────────────────────────────────────────────────────


def test_offsets_move_the_retry_to_a_different_time_and_day(_enabled):
    """Re-dialing at the same hour tomorrow mostly re-tests the same
    unavailability — hence a same-day retry, then a next-day one."""
    second = r.next_attempt_at(attempt=1, hangup_cause="NO_ANSWER", now=NOW)
    third = r.next_attempt_at(attempt=2, hangup_cause="NO_ANSWER", now=NOW)
    assert (second - NOW).total_seconds() / 3600 == 3      # later the same day
    assert (third - NOW).total_seconds() / 3600 == 26      # next day, different hour
    assert third.date() > NOW.date()


def test_budget_is_respected(_enabled):
    assert r.next_attempt_at(attempt=3, hangup_cause="NO_ANSWER", now=NOW) is None
    assert r.next_attempt_at(attempt=9, hangup_cause="NO_ANSWER", now=NOW) is None


def test_no_offset_configured_for_that_attempt_means_no_retry(monkeypatch):
    """A budget of 3 with only one offset stops after the one retry it can
    actually schedule, rather than inventing a time."""
    monkeypatch.setattr(settings, "APEX_RETRY_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(settings, "APEX_RETRY_OFFSETS_HOURS", "3", raising=False)
    assert r.next_attempt_at(attempt=1, hangup_cause="NO_ANSWER", now=NOW) is not None
    assert r.next_attempt_at(attempt=2, hangup_cause="NO_ANSWER", now=NOW) is None


def test_junk_offsets_are_ignored_not_fatal(monkeypatch):
    monkeypatch.setattr(settings, "APEX_RETRY_ATTEMPTS", 3, raising=False)
    monkeypatch.setattr(settings, "APEX_RETRY_OFFSETS_HOURS", "3, , abc, -5, 26", raising=False)
    assert r._offsets() == [3.0, 26.0]


# ── which numbers are worth re-dialing ───────────────────────────────────────


def test_dead_numbers_are_never_retried(_enabled):
    """A disconnected line is not a missed call. Retrying it burns dials, credits
    and the DID's reputation on a contact that can never answer."""
    for cause in ("INVALID_NUMBER", "UNALLOCATED_NUMBER", "NUMBER_CHANGED"):
        assert r.next_attempt_at(attempt=1, hangup_cause=cause, now=NOW) is None
        assert r.is_permanent(cause) is True


def test_busy_and_rejected_are_not_treated_as_permanent(_enabled):
    """Deliberately absent from the default denylist: whether a busy or rejected
    call is worth a second try is exactly what the cause histogram should settle,
    not something guessed in a config default."""
    for cause in ("USER_BUSY", "CALL_REJECTED", "NORMAL_CLEARING"):
        assert r.is_permanent(cause) is False
        assert r.next_attempt_at(attempt=1, hangup_cause=cause, now=NOW) is not None


def test_unknown_cause_is_not_evidence_of_a_dead_number(_enabled):
    assert r.is_permanent(None) is False
    assert r.is_permanent("") is False
    assert r.next_attempt_at(attempt=1, hangup_cause=None, now=NOW) is not None


def test_denylist_is_config_driven(monkeypatch, _enabled):
    """Set from the observed histogram once there is one."""
    monkeypatch.setattr(settings, "APEX_RETRY_SKIP_CAUSES", "USER_BUSY", raising=False)
    assert r.is_permanent("USER_BUSY") is True
    assert r.is_permanent("INVALID_NUMBER") is False   # no longer in the list


def test_denylist_matches_case_insensitively_on_substrings(_enabled):
    assert r.is_permanent("sip;cause=invalid_number") is True


def test_max_attempts_floors_at_one(monkeypatch):
    monkeypatch.setattr(settings, "APEX_RETRY_ATTEMPTS", 0, raising=False)
    assert r.max_attempts() == 1
    monkeypatch.setattr(settings, "APEX_RETRY_ATTEMPTS", "nonsense", raising=False)
    assert r.max_attempts() == 1
