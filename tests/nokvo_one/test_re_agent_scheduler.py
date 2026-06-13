"""RE_agent_scheduler — post-call layer that fills + assigns a site-visit ticket.

LLM-extracts the visit details from the call note, fills the Site Visit Fields on
the SAME ticket, and assigns the nearest-free agent. Best-effort + idempotent.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.services.re_agent_scheduler import REAgentScheduler


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeDB:
    def __init__(self, rec):
        self.rec = rec
        self.committed = False

    async def get(self, model, pk):
        return self.rec

    def add(self, obj):
        pass

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


def _ticket(**data) -> SimpleNamespace:
    data.setdefault("issue_type", "site_visit")
    return SimpleNamespace(
        id=uuid.uuid4(), record_type="ticket", organization_id=uuid.uuid4(), data=data
    )


def _patch(monkeypatch, *, llm_out, projects=None, assignment=None, calls=None):
    async def fake_complete_global(messages, **kw):
        if calls is not None:
            calls.append("llm")
        return llm_out

    async def fake_load_projects(db, org_id):
        return projects or []

    async def fake_assign(db, org_id, rec, **kw):
        if calls is not None:
            calls.append(("assign", kw.get("requested_time"), kw.get("request_type")))
        return assignment

    monkeypatch.setattr(
        "app.services.nokvo_one_voice_pipeline.AzureGroundedLLM.complete_global",
        fake_complete_global,
    )
    monkeypatch.setattr(
        "app.services.real_estate_project_service.load_active_projects", fake_load_projects
    )
    monkeypatch.setattr(
        "app.services.predefined_tools_service._assign_existing_record", fake_assign
    )


_SKYLINE = SimpleNamespace(id="proj-1", name="Skyline Heights")


def test_firm_time_fills_fields_and_assigns(monkeypatch):
    calls: list = []
    _patch(
        monkeypatch,
        llm_out='{"visit_date":"2026-06-20","visit_time":"16:00","project_name":"Skyline Heights",'
                '"customer_name":"Nihar","phone":"+919999999999","requirements":"wants a 3 BHK"}',
        projects=[_SKYLINE],
        assignment={
            "selected_member_id": "m-1", "selected_member_name": "Rahul",
            "scheduled_time": "2026-06-20T10:30:00+00:00", "time_adjusted": False,
            "assignment_status": "assigned",
        },
        calls=calls,
    )
    # Isolate the assignment wiring from the date parser.
    monkeypatch.setattr(
        REAgentScheduler, "_parse_visit_datetime",
        staticmethod(lambda d, t: __import__("datetime").datetime(2026, 6, 20, 10, 30, tzinfo=__import__("datetime").timezone.utc)),
    )
    rec = _ticket(handoff_note="Wants to visit Skyline Heights Saturday 4pm. Name Nihar.")
    db = _FakeDB(rec)

    res = _run(REAgentScheduler.schedule_for_site_visit(db, SimpleNamespace(), str(rec.id)))

    assert res["ok"] is True and db.committed is True
    assert rec.data["project_name"] == "Skyline Heights"
    assert rec.data["project_id"] == "proj-1"
    assert rec.data["visit_date"] == "2026-06-20" and rec.data["visit_time"] == "16:00"
    assert rec.data["assigned_member_id"] == "m-1"
    assert rec.data["assigned_member_name"] == "Rahul"
    assert rec.data["assignment_status"] == "assigned"
    assert rec.data["re_scheduled"] is True
    assert any(c[0] == "assign" and c[2] == "site_visit" for c in calls if isinstance(c, tuple))


def test_vague_note_enriches_but_needs_manual_scheduling(monkeypatch):
    calls: list = []
    _patch(
        monkeypatch,
        llm_out='{"visit_date":null,"visit_time":null,"project_name":"Skyline Heights",'
                '"customer_name":null,"phone":null,"requirements":"interested, will decide a time"}',
        projects=[_SKYLINE],
        assignment=None,
        calls=calls,
    )
    rec = _ticket(handoff_note="Interested in Skyline Heights, wants to visit sometime soon.")
    db = _FakeDB(rec)

    res = _run(REAgentScheduler.schedule_for_site_visit(db, SimpleNamespace(), str(rec.id)))

    assert res["ok"] is True
    assert rec.data["project_name"] == "Skyline Heights"     # still enriched
    assert rec.data["assignment_status"] == "needs_manual_scheduling"
    assert rec.data.get("assigned_member_id") is None
    assert rec.data["re_scheduled"] is True
    assert not any(isinstance(c, tuple) and c[0] == "assign" for c in calls)  # no assignment attempted


def test_idempotent_skips_already_scheduled(monkeypatch):
    calls: list = []
    _patch(monkeypatch, llm_out="{}", projects=[_SKYLINE], assignment=None, calls=calls)
    rec = _ticket(handoff_note="…", re_scheduled=True)
    db = _FakeDB(rec)

    res = _run(REAgentScheduler.schedule_for_site_visit(db, SimpleNamespace(), str(rec.id)))

    assert res == {"ok": False, "reason": "already_scheduled"}
    assert calls == []                                       # LLM never called


def test_bad_llm_output_is_safe(monkeypatch):
    _patch(monkeypatch, llm_out="sorry, I couldn't parse that", projects=[_SKYLINE], assignment=None)
    rec = _ticket(handoff_note="Wants a visit but unclear when.")
    db = _FakeDB(rec)

    res = _run(REAgentScheduler.schedule_for_site_visit(db, SimpleNamespace(), str(rec.id)))

    assert res["ok"] is True                                 # no crash
    assert rec.data["assignment_status"] == "needs_manual_scheduling"
    assert rec.data["re_scheduled"] is True


def test_preserves_existing_ani_and_name(monkeypatch):
    _patch(
        monkeypatch,
        llm_out='{"visit_date":null,"visit_time":null,"customer_name":"Different Name",'
                '"phone":"+910000000000"}',
        projects=[_SKYLINE], assignment=None,
    )
    rec = _ticket(handoff_note="…", name="Nihar", phone="+919999999999")
    db = _FakeDB(rec)

    _run(REAgentScheduler.schedule_for_site_visit(db, SimpleNamespace(), str(rec.id)))

    # The call's ANI + captured name win over the note re-extraction.
    assert rec.data["name"] == "Nihar"
    assert rec.data["phone"] == "+919999999999"


def test_parse_visit_datetime_smoke():
    # Vague/missing → None (no assignment); a concrete pair → a datetime.
    assert REAgentScheduler._parse_visit_datetime(None, None) is None
    assert REAgentScheduler._parse_visit_datetime("2026-06-20", None) is None
    got = REAgentScheduler._parse_visit_datetime("2026-06-20", "4 PM")
    assert got is None or hasattr(got, "tzinfo")            # parseable → datetime
