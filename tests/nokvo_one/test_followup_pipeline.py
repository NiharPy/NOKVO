"""Unit tests for the Follow-Up Pipeline pure logic.

The DB-query assembly in ``pipeline_for_tenant`` is exercised by the live API
smoke test; here we lock down the two bug-prone pure helpers it relies on:
  - ``_due_bucket`` — overdue folds into "today"; tomorrow / this_week / later
    boundaries are correct.
  - ``_conversion_rate`` — ratio with a divide-by-zero guard.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.models.lead_followup_schedule import FollowupReason, FollowupStatus
from app.models.outgoing_lead import LeadConsentStatus
from app.services.followup_scheduler_service import FollowupSchedulerService as S


@pytest.fixture(autouse=True)
def _enable_followup_agent(monkeypatch):
    """These tests exercise the Follow-up agent's scheduling logic. Production
    ships it OFF (FOLLOWUP_AGENT_ENABLED=False), so turn it on for the feature
    tests; monkeypatch reverts after each."""
    monkeypatch.setattr(settings, "FOLLOWUP_AGENT_ENABLED", True)


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


# ── upsert_promise_from_note — single-pending refine/insert + kill switches ───
#
# The query helpers are stubbed (these unit tests run without a DB); we verify
# the decision logic: promise overrides → exactly one row, gates skip.


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeDB:
    def __init__(self, target):
        self._target = target
        self.added = []
        self.committed = False

    async def get(self, model, pk):
        return self._target

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def refresh(self, obj):
        pass


def _lead():
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id="t1",
        consent_status=LeadConsentStatus.granted,
        campaign_id=None,
    )


def _stub_helpers(monkeypatch, *, pending=None, attempts=0, clinic=False, paused=False):
    async def f_clinic(*, tenant_id, db):
        return clinic

    async def f_pending(*, lead_id, customer_id, db):
        return pending

    async def f_attempts(*, lead_id, customer_id, campaign_id, db):
        return attempts

    async def f_paused(*, lead_id, db):
        return paused

    monkeypatch.setattr(S, "_tenant_is_clinic", staticmethod(f_clinic))
    monkeypatch.setattr(S, "_pending_row_for_target", staticmethod(f_pending))
    monkeypatch.setattr(S, "_attempts_for_target", staticmethod(f_attempts))
    monkeypatch.setattr(S, "_lead_is_paused", staticmethod(f_paused))


# A promised time inside the default 09:00–19:00 IST window (12:00 IST = 06:30 UTC)
# so the call-window clamp leaves it unchanged. Relative to now (+5 days) so the
# past-promise guard never rewrites it — a hardcoded date silently rots into the
# past and the assertion breaks once "today" rolls past it.
PROMISED = (datetime.now(timezone.utc) + timedelta(days=5)).replace(
    hour=6, minute=30, second=0, microsecond=0
)


def test_upsert_noop_when_followup_agent_disabled(monkeypatch):
    # Master kill switch: nothing is scheduled and the DB is never touched.
    monkeypatch.setattr(settings, "FOLLOWUP_AGENT_ENABLED", False)
    _stub_helpers(monkeypatch, pending=None)
    lead = _lead()
    db = _FakeDB(lead)
    row = _run(
        S.upsert_promise_from_note(
            db=db, promised_at=PROMISED, note="x", source_call_id="c", lead_id=lead.id,
        )
    )
    assert row is None
    assert not db.committed


def test_upsert_inserts_when_no_pending(monkeypatch):
    _stub_helpers(monkeypatch, pending=None)
    lead = _lead()
    db = _FakeDB(lead)
    row = _run(
        S.upsert_promise_from_note(
            db=db, promised_at=PROMISED, note="call back re pricing",
            source_call_id="c1", lead_id=lead.id,
        )
    )
    assert row is not None
    assert row.reason == FollowupReason.promise_extracted
    assert row.note == "call back re pricing"
    assert row.lead_id == lead.id
    assert row.status == FollowupStatus.pending
    assert row.scheduled_at == PROMISED  # in-window → unchanged
    assert db.committed


def test_upsert_refines_existing_pending_in_place(monkeypatch):
    existing = SimpleNamespace(
        id=uuid.uuid4(), scheduled_at=None, reason=FollowupReason.no_answer_retry,
        note=None, source_call_id=None, campaign_id=None,
    )
    _stub_helpers(monkeypatch, pending=existing)
    lead = _lead()
    db = _FakeDB(lead)
    row = _run(
        S.upsert_promise_from_note(
            db=db, promised_at=PROMISED, note="refined reason",
            source_call_id="c2", lead_id=lead.id,
        )
    )
    # Same row, upgraded to a promise — no second row created.
    assert row is existing
    assert existing.reason == FollowupReason.promise_extracted
    assert existing.note == "refined reason"
    assert existing.scheduled_at == PROMISED
    # Re-adds the SAME row to flush the update; never constructs a new one.
    assert db.added == [existing]


def test_upsert_skips_clinic_tenant(monkeypatch):
    _stub_helpers(monkeypatch, clinic=True)
    lead = _lead()
    row = _run(
        S.upsert_promise_from_note(
            db=_FakeDB(lead), promised_at=PROMISED, note="x", source_call_id="c",
            lead_id=lead.id,
        )
    )
    assert row is None


def test_upsert_skips_revoked_consent(monkeypatch):
    _stub_helpers(monkeypatch)
    lead = _lead()
    lead.consent_status = LeadConsentStatus.revoked
    row = _run(
        S.upsert_promise_from_note(
            db=_FakeDB(lead), promised_at=PROMISED, note="x", source_call_id="c",
            lead_id=lead.id,
        )
    )
    assert row is None


def test_upsert_past_promise_pushed_into_future(monkeypatch):
    _stub_helpers(monkeypatch, pending=None)
    lead = _lead()
    db = _FakeDB(lead)
    past = datetime(2000, 1, 1, 10, 0, tzinfo=timezone.utc)
    row = _run(
        S.upsert_promise_from_note(
            db=db, promised_at=past, note="n", source_call_id="c", lead_id=lead.id,
        )
    )
    assert row is not None
    assert row.scheduled_at > datetime.now(timezone.utc)
