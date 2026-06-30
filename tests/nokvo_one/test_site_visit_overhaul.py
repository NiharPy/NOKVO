"""Inbound real-estate site visits: created on caller agreement, stored as
ANI + name (if captured) + the post-call note — NO structured date/time/project
fields. The date/time the agent clarifies lives in the call note. Outbound
campaign booking (structured ``visit_at``) is intentionally untouched.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any

from app.services.tool_flow_policy import caller_agreed_to_site_visit
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── visit-agreement detector ────────────────────────────────────────────────


def test_detects_explicit_visit_intent():
    assert caller_agreed_to_site_visit(
        [{"role": "user", "content": "I'd like to visit the property"}]
    ) is True


def test_detects_yes_after_agent_offered_visit():
    hist = [
        {"role": "assistant", "content": "Would you like to book a site visit to Skyline Heights?"},
        {"role": "user", "content": "Yeah, sure"},
    ]
    assert caller_agreed_to_site_visit(hist) is True


def test_bare_yes_without_a_visit_offer_is_not_agreement():
    hist = [
        {"role": "assistant", "content": "We have 3 and 4 BHK options starting at 2.45 crore."},
        {"role": "user", "content": "ok"},
    ]
    assert caller_agreed_to_site_visit(hist) is False


def test_enquiry_only_is_not_agreement():
    hist = [
        {"role": "user", "content": "what's the price and the RERA number?"},
        {"role": "assistant", "content": "It's 2.45 crore, RERA registered."},
    ]
    assert caller_agreed_to_site_visit(hist) is False


def test_detects_datetime_acceptance_after_visit_offer():
    """The caller accepts "when would you like to come?" by naming a slot, not
    by saying "yes" — this must still file a site visit (the reported bug)."""
    hist = [
        {"role": "assistant", "content": "Sure! When would you like to come and see the project?"},
        {"role": "user", "content": "Saturday around 4 works"},
    ]
    assert caller_agreed_to_site_visit(hist) is True


def test_datetime_acceptance_tolerates_a_clarifying_exchange():
    hist = [
        {"role": "assistant", "content": "Would you like to book a site visit?"},
        {"role": "user", "content": "what's the price first?"},
        {"role": "assistant", "content": "It starts at 2.45 crore."},
        {"role": "user", "content": "okay tomorrow evening"},
    ]
    assert caller_agreed_to_site_visit(hist) is True


def test_noncommittal_reply_after_offer_is_not_agreement():
    """A hesitant caller who mentions a timeframe but is "just exploring" stays
    an enquiry lead, not a booked visit."""
    hist = [
        {"role": "assistant", "content": "Would you like to schedule a site visit?"},
        {"role": "user", "content": "Maybe next month, I'm just exploring for now"},
    ]
    assert caller_agreed_to_site_visit(hist) is False


def test_date_without_a_visit_offer_is_not_agreement():
    """A date mentioned during a pure enquiry (no visit was offered) must not
    trigger a site visit — preserves the two-intents split."""
    hist = [
        {"role": "assistant", "content": "When are you looking to buy?"},
        {"role": "user", "content": "maybe in the next couple of months"},
    ]
    assert caller_agreed_to_site_visit(hist) is False


# ── minimal inbound site-visit record ───────────────────────────────────────


class _FakeDB:
    def __init__(self):
        self.added: list[Any] = []
        self.committed = False

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None


def test_create_inbound_site_visit_is_ani_plus_name_no_fields(monkeypatch):
    merged: dict[str, Any] = {}

    async def fake_merge(tenant_res, call_id, patch, **kw):
        merged.update(patch)
        return patch

    monkeypatch.setattr(
        "app.services.agent_session_store.AgentSessionStore.merge_state", fake_merge
    )

    db = _FakeDB()
    org_id = uuid.uuid4()
    res = _run(NokvoOneVoicePipeline._create_inbound_site_visit(
        db, org_id, SimpleNamespace(provider_status={}), "call-1",
        state={"caller_phone": "+919999999999"},
        memory={"name": "Nihar"},
    ))

    assert res is not None and res["result"]["ok"] is True
    assert db.committed is True
    rec = db.added[0]
    assert rec.record_type == "ticket"
    assert rec.contact_phone == "+919999999999"            # the ANI
    assert rec.data["phone"] == "+919999999999"
    assert rec.data["name"] == "Nihar"
    assert rec.data["issue_type"] == "site_visit"
    assert rec.data["agent_mode_final"] == "site_visit"
    assert rec.status                                       # a real ticket status
    # NO structured booking fields — that context rides in the call note.
    for k in ("visit_date", "visit_time", "project_name", "project_id"):
        assert k not in rec.data
    # State so the condenser attaches the note AND no duplicate lead is created.
    assert merged["auto_site_visit_id"] == str(rec.id)
    assert merged["auto_site_visit_created"] is True
    assert merged["auto_lead_created"] is True
    assert merged["agent_mode_final"] == "site_visit"


def test_create_inbound_site_visit_omits_name_when_not_captured(monkeypatch):
    async def fake_merge(*a, **k):
        return {}

    monkeypatch.setattr(
        "app.services.agent_session_store.AgentSessionStore.merge_state", fake_merge
    )
    db = _FakeDB()
    res = _run(NokvoOneVoicePipeline._create_inbound_site_visit(
        db, uuid.uuid4(), SimpleNamespace(provider_status={}), "c",
        state={"caller_phone": "918031321315"}, memory={},
    ))
    assert res is not None
    rec = db.added[0]
    assert "name" not in rec.data                           # only set when captured
    assert rec.data["phone"] == "918031321315"


# ── end-of-call routing (inbound) ───────────────────────────────────────────


def test_inbound_agreement_creates_site_visit_not_lead(monkeypatch):
    """When the caller agreed to a visit, the teardown hook files a SITE VISIT
    ticket (ANI + name) and never reaches the leads_create path."""
    org_id = uuid.uuid4()
    tool_calls: list[str] = []
    merged: list[dict[str, Any]] = []

    async def fake_context(db, tenant_res):
        return SimpleNamespace(industry="real_estate"), {}, []

    async def fake_state(*a, **k):
        return {"call_surface": "voice_inbound", "caller_phone": "+919999999999"}

    async def fake_history(*a, **k):
        return [
            {"role": "assistant", "content": "Would you like to book a site visit this week?"},
            {"role": "user", "content": "Yes, I'll come on Saturday around 4 pm"},
        ]

    async def fake_execute(db, organization_id, user_id, tool, arguments, **kwargs):
        tool_calls.append(tool.key)
        return {"ok": True, "id": str(uuid.uuid4())}

    async def fake_merge(_tenant_res, _call_id, patch, **kwargs):
        merged.append(patch)
        return patch

    monkeypatch.setattr(NokvoOneVoicePipeline, "_voice_business_context", staticmethod(fake_context))
    monkeypatch.setattr("app.services.agent_session_store.AgentSessionStore.get_state", fake_state)
    monkeypatch.setattr("app.services.agent_session_store.AgentSessionStore.get_history", fake_history)
    monkeypatch.setattr("app.services.agent_session_store.AgentSessionStore.merge_state", fake_merge)
    monkeypatch.setattr("app.services.nokvo_one_voice_pipeline.PredefinedToolsService.execute", fake_execute)

    db = _FakeDB()
    tenant_res = SimpleNamespace(tenant_id="t", organization_id=org_id, provider_status={})
    result = _run(NokvoOneVoicePipeline.maybe_create_real_estate_lead_from_call(
        tenant_res, db, "call-visit",
    ))

    assert result is not None and result["tool"] == "site_visit_create_minimal"
    assert "leads_create" not in tool_calls                 # NOT filed as a lead
    ticket = next(r for r in db.added if getattr(r, "record_type", None) == "ticket")
    assert ticket.contact_phone == "+919999999999"
    assert ticket.data["issue_type"] == "site_visit"
    assert any(p.get("auto_site_visit_id") for p in merged)
