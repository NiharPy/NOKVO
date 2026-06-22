"""LeadFollowupNoteScheduler — post-call layer that reads a lead's call note,
extracts a callback time the prospect asked for, and configures the follow-up
callback (carrying the note).

Mirrors test_re_agent_scheduler.py: monkeypatch the extraction LLM + the
downstream service and use lightweight fakes (these unit tests run without a
DB, per tests/nokvo_one/conftest.py).
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.lead_followup_note_scheduler import LeadFollowupNoteScheduler


@pytest.fixture(autouse=True)
def _enable_followup_agent(monkeypatch):
    """Production ships the Follow-up agent OFF (FOLLOWUP_AGENT_ENABLED=False);
    enable it so these tests exercise the note→callback logic."""
    monkeypatch.setattr(settings, "FOLLOWUP_AGENT_ENABLED", True)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


_TENANT = SimpleNamespace(tenant_id="tenant-1")


def _future_date() -> str:
    """An always-future ISO date so the lenient date parser never rejects it."""
    return (datetime.now() + timedelta(days=5)).date().isoformat()


def _patch_llm(monkeypatch, llm_out):
    async def fake_complete_nano(messages, **kw):
        return llm_out

    # The extractor runs on the gpt-4.1-nano pool (complete_nano).
    monkeypatch.setattr(
        "app.services.nokvo_one_voice_pipeline.AzureGroundedLLM.complete_nano",
        fake_complete_nano,
    )


def _patch_not_clinic(monkeypatch):
    async def fake_clinic(*, tenant_id, db):
        return False

    monkeypatch.setattr(
        "app.services.followup_scheduler_service.FollowupSchedulerService._tenant_is_clinic",
        staticmethod(fake_clinic),
    )


def _capture_upsert(monkeypatch, calls):
    async def fake_upsert(**kwargs):
        calls.append(kwargs)
        return SimpleNamespace(id=uuid.uuid4(), scheduled_at=kwargs.get("promised_at"))

    monkeypatch.setattr(
        "app.services.followup_scheduler_service.FollowupSchedulerService.upsert_promise_from_note",
        staticmethod(fake_upsert),
    )


# ── _parse_callback_datetime (pure) ──────────────────────────────────────────


def test_parse_callback_datetime_date_and_time():
    dt = LeadFollowupNoteScheduler._parse_callback_datetime(_future_date(), "16:00")
    assert dt is not None
    # 16:00 IST → 10:30 UTC (IST is a fixed +5:30, no DST).
    assert (dt.hour, dt.minute) == (10, 30)


def test_parse_callback_datetime_date_only_defaults_10am_ist():
    dt = LeadFollowupNoteScheduler._parse_callback_datetime(_future_date(), None)
    assert dt is not None
    # default 10:00 IST → 04:30 UTC
    assert (dt.hour, dt.minute) == (4, 30)


def test_parse_callback_datetime_no_date_returns_none():
    assert LeadFollowupNoteScheduler._parse_callback_datetime(None, "16:00") is None
    assert LeadFollowupNoteScheduler._parse_callback_datetime("", None) is None


# ── schedule_from_note decision logic ────────────────────────────────────────


def test_schedule_from_note_with_callback_dispatches(monkeypatch):
    _patch_llm(
        monkeypatch,
        '{"callback_requested": true, "callback_date": "%s", "callback_time": "16:00", '
        '"callback_reason": "discuss 3BHK pricing"}' % _future_date(),
    )
    _patch_not_clinic(monkeypatch)
    calls = []
    _capture_upsert(monkeypatch, calls)
    lead_id = uuid.uuid4()
    res = _run(
        LeadFollowupNoteScheduler.schedule_from_note(
            db=object(),
            tenant_res=_TENANT,
            note="Prospect asked us to call back at 4pm.",
            source_call_id="call-1",
            lead_id=lead_id,
        )
    )
    assert res["ok"] is True
    assert len(calls) == 1
    assert calls[0]["lead_id"] == lead_id
    assert calls[0]["note"] == "discuss 3BHK pricing"
    assert calls[0]["promised_at"] is not None


def test_schedule_from_note_no_callback_does_not_dispatch(monkeypatch):
    _patch_llm(
        monkeypatch,
        '{"callback_requested": false, "callback_date": null, "callback_time": null, '
        '"callback_reason": null}',
    )
    _patch_not_clinic(monkeypatch)
    calls = []
    _capture_upsert(monkeypatch, calls)
    res = _run(
        LeadFollowupNoteScheduler.schedule_from_note(
            db=object(),
            tenant_res=_TENANT,
            note="Prospect browsing, no callback requested.",
            source_call_id="call-1",
            lead_id=uuid.uuid4(),
        )
    )
    assert res["ok"] is False and res["reason"] == "no_callback_in_note"
    assert calls == []


def test_schedule_from_note_clinic_skips_llm_and_dispatch(monkeypatch):
    seen = {"llm": False}

    async def fake_llm(messages, **kw):
        seen["llm"] = True
        return "{}"

    monkeypatch.setattr(
        "app.services.nokvo_one_voice_pipeline.AzureGroundedLLM.complete_nano", fake_llm
    )

    async def fake_clinic(*, tenant_id, db):
        return True

    monkeypatch.setattr(
        "app.services.followup_scheduler_service.FollowupSchedulerService._tenant_is_clinic",
        staticmethod(fake_clinic),
    )
    calls = []
    _capture_upsert(monkeypatch, calls)
    res = _run(
        LeadFollowupNoteScheduler.schedule_from_note(
            db=object(),
            tenant_res=_TENANT,
            note="call me tomorrow",
            source_call_id="c",
            lead_id=uuid.uuid4(),
        )
    )
    assert res["reason"] == "clinic_admin_only"
    assert seen["llm"] is False  # gated before spending an LLM call
    assert calls == []


def test_schedule_from_note_empty_note_no_target():
    res = _run(
        LeadFollowupNoteScheduler.schedule_from_note(
            db=object(),
            tenant_res=_TENANT,
            note="   ",
            source_call_id="c",
            lead_id=uuid.uuid4(),
        )
    )
    assert res["reason"] == "no_note"


def test_schedule_from_note_disabled_when_followup_agent_off(monkeypatch):
    # Master kill switch short-circuits BEFORE the extraction LLM is ever called.
    monkeypatch.setattr(settings, "FOLLOWUP_AGENT_ENABLED", False)
    seen = {"llm": False}

    async def fake_llm(messages, **kw):
        seen["llm"] = True
        return "{}"

    monkeypatch.setattr(
        "app.services.nokvo_one_voice_pipeline.AzureGroundedLLM.complete_nano", fake_llm
    )
    res = _run(
        LeadFollowupNoteScheduler.schedule_from_note(
            db=object(),
            tenant_res=_TENANT,
            note="call me tomorrow at 4pm",
            source_call_id="c",
            lead_id=uuid.uuid4(),
        )
    )
    assert res["reason"] == "followup_disabled"
    assert seen["llm"] is False


def test_schedule_from_note_extracts_json_from_prose(monkeypatch):
    # LLM wraps the JSON in prose — _JSON_OBJ must still pull it out, and with no
    # callback_reason the note falls back to the summary's first line.
    _patch_llm(
        monkeypatch,
        'Sure! Here you go:\n{"callback_requested": true, "callback_date": "%s", '
        '"callback_time": null, "callback_reason": null}\nHope that helps' % _future_date(),
    )
    _patch_not_clinic(monkeypatch)
    calls = []
    _capture_upsert(monkeypatch, calls)
    res = _run(
        LeadFollowupNoteScheduler.schedule_from_note(
            db=object(),
            tenant_res=_TENANT,
            note="Caller asked to be rung next Saturday.",
            source_call_id="c",
            lead_id=uuid.uuid4(),
        )
    )
    assert res["ok"] is True
    assert len(calls) == 1
    assert calls[0]["note"]  # fell back to the note's first line
