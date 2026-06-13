"""Lead-vs-Site-Visit routing for the end-of-call real-estate safety net.

Regression for: a call where the caller clearly booked a site visit
(gave name, phone, project, a firm date AND time) created a LEAD instead of
a Site Visit, because the deterministic flow didn't fire and the safety net
only ever made leads.

These exercise ``NokvoOneVoicePipeline._site_visit_args_from_call_state`` — the
pure decision the safety net now uses: a FIRM booking (name + phone + parseable
date + time) yields site-visit args; an enquiry / vague call yields ``None`` so
it stays a lead.
"""
from __future__ import annotations

import asyncio
import uuid

from app.services.agent_session_store import AgentSessionStore
from app.services.conversational_memory import (
    FACT_BUDGET,
    FACT_NAME,
    FACT_PHONE,
    FACT_PROPERTY,
    FACT_VISIT_DATE,
    FACT_VISIT_TIME,
    ConversationalMemory,
    MemoryFact,
)
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.predefined_tools_service import PredefinedToolsService


class _Org:
    industry = "real_estate"


def _fact(key: str, value: str, turn: int = 1) -> MemoryFact:
    return MemoryFact(key=key, value=value, confidence=0.9, source_turn=turn, timestamp=0.0)


def _state(**facts: str) -> dict:
    cm = ConversationalMemory()
    for key, value in facts.items():
        cm.facts[key] = _fact(key, value)
    return {"memory": cm.to_state_blob(), "tool_flow": {}}


def _call(state: dict, *, memory: dict | None = None, campaign_context: dict | None = None):
    return NokvoOneVoicePipeline._site_visit_args_from_call_state(
        state=state,
        organization=_Org(),
        overrides={},
        custom_tabs=[],
        memory=memory or {},
        campaign_context=campaign_context,
    )


def test_firm_booking_builds_site_visit_args() -> None:
    state = _state(
        **{
            FACT_NAME: "Nihar",
            FACT_PHONE: "7569672503",
            FACT_VISIT_DATE: "tomorrow",
            FACT_VISIT_TIME: "11 AM",
            FACT_PROPERTY: "Skyline Heights",
        }
    )
    args = _call(state)
    assert args is not None
    assert args["name"] == "Nihar"
    assert args["phone"] == "7569672503"
    assert args["project_name"] == "Skyline Heights"
    assert "T" in args["visit_at"]  # combined ISO datetime
    assert isinstance(args["record_data"], dict) and args["record_data"]


def test_named_time_of_day_with_date_is_a_booking() -> None:
    """'morning' is a real stated slot (the scheduler resolves it to 9 AM), the
    same as the deterministic flow — so date + morning + name + phone books."""
    state = _state(
        **{
            FACT_NAME: "Nihar",
            FACT_PHONE: "7569672503",
            FACT_VISIT_DATE: "tomorrow",
            FACT_VISIT_TIME: "morning",
        }
    )
    assert _call(state) is not None


def test_enquiry_without_date_time_is_not_a_site_visit() -> None:
    state = _state(
        **{
            FACT_NAME: "Nihar",
            FACT_PHONE: "7569672503",
            FACT_BUDGET: "80 lakhs",
            FACT_PROPERTY: "Skyline Heights",
        }
    )
    assert _call(state) is None


def test_date_without_time_is_not_a_firm_booking() -> None:
    state = _state(
        **{
            FACT_NAME: "Nihar",
            FACT_PHONE: "7569672503",
            FACT_VISIT_DATE: "tomorrow",
        }
    )
    assert _call(state) is None


def test_unparseable_time_is_not_a_firm_booking() -> None:
    state = _state(
        **{
            FACT_NAME: "Nihar",
            FACT_PHONE: "7569672503",
            FACT_VISIT_DATE: "tomorrow",
            FACT_VISIT_TIME: "sometime",
        }
    )
    assert _call(state) is None


def test_missing_phone_is_not_a_firm_booking() -> None:
    state = _state(
        **{
            FACT_NAME: "Nihar",
            FACT_VISIT_DATE: "tomorrow",
            FACT_VISIT_TIME: "11 AM",
        }
    )
    # No FACT_PHONE and no campaign-context phone to fall back on.
    assert _call(state) is None


def test_phone_falls_back_to_campaign_context() -> None:
    """Inbound calls carry the caller's number even if it was never spoken."""
    state = _state(
        **{
            FACT_NAME: "Nihar",
            FACT_VISIT_DATE: "tomorrow",
            FACT_VISIT_TIME: "11 AM",
        }
    )
    args = _call(state, campaign_context={"from_phone": "7569672503"})
    assert args is not None
    assert args["phone"] == "7569672503"


# ─────────── End-to-end safety net (maybe_create_real_estate_lead_from_call) ───


class _Org2:
    def __init__(self) -> None:
        self.industry = "real_estate"
        self.id = uuid.uuid4()


class _Tenant:
    def __init__(self) -> None:
        self.organization_id = uuid.uuid4()
        self.tenant_id = "t1"
        self.provider_status = {}


class _FakeDB2:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass

    async def rollback(self) -> None:
        pass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch_session(monkeypatch, *, state: dict, history: list[dict], merged: dict) -> list[str]:
    async def _get_state(_tr, _cid):
        return state

    async def _get_history(_tr, _cid):
        return history

    async def _merge_state(_tr, _cid, patch):
        merged.update(patch)

    async def _ctx(_db, _tr):
        return (_Org2(), {}, [])

    calls: list[str] = []

    async def _execute(_db, _org_id, _user_id, tool, _args, session_id=None):
        calls.append(tool.key)
        return {
            "ok": True,
            "id": str(uuid.uuid4()),
            "ticket_id": str(uuid.uuid4()),
            "record_type": "ticket",
        }

    monkeypatch.setattr(AgentSessionStore, "get_state", staticmethod(_get_state))
    monkeypatch.setattr(AgentSessionStore, "get_history", staticmethod(_get_history))
    monkeypatch.setattr(AgentSessionStore, "merge_state", staticmethod(_merge_state))
    monkeypatch.setattr(NokvoOneVoicePipeline, "_voice_business_context", staticmethod(_ctx))
    monkeypatch.setattr(PredefinedToolsService, "execute", staticmethod(_execute))
    return calls


def test_inbound_visit_agreement_creates_minimal_site_visit_not_lead(monkeypatch) -> None:
    """Inbound overhaul: when the caller agrees to a visit, the safety net files a
    MINIMAL Site Visit ticket (ANI + name; the date/time live in the call note),
    NOT a lead and NOT the old structured qualify_lead_and_schedule_visit ticket.
    Even though a firm date/time was captured in memory, it is NOT saved as fields."""
    cm = ConversationalMemory()
    cm.facts[FACT_NAME] = _fact(FACT_NAME, "Nihar")
    cm.facts[FACT_PHONE] = _fact(FACT_PHONE, "7569672503")
    cm.facts[FACT_VISIT_DATE] = _fact(FACT_VISIT_DATE, "tomorrow")
    cm.facts[FACT_VISIT_TIME] = _fact(FACT_VISIT_TIME, "11 AM")
    cm.facts[FACT_PROPERTY] = _fact(FACT_PROPERTY, "Skyline Heights")
    state = {
        "memory": cm.to_state_blob(),
        "tool_flow": {},
        "call_surface": "voice_inbound",
        "caller_phone": "+917569672503",
    }
    history = [{"role": "user", "content": "Could you arrange a site visit tomorrow at 11 AM?"}]
    merged: dict = {}
    calls = _patch_session(monkeypatch, state=state, history=history, merged=merged)

    db = _FakeDB2()
    result = _run(
        NokvoOneVoicePipeline.maybe_create_real_estate_lead_from_call(
            _Tenant(), db, "call-firm"
        )
    )
    assert result is not None
    assert result["tool"] == "site_visit_create_minimal"
    assert "qualify_lead_and_schedule_visit" not in calls   # no structured booking
    assert "leads_create" not in calls                       # and not a lead
    assert merged.get("auto_site_visit_created") is True
    assert merged.get("auto_site_visit_id")
    # Minimal record: ANI + captured name, NO structured date/time/project fields.
    ticket = next(r for r in db.added if getattr(r, "record_type", None) == "ticket")
    assert ticket.contact_phone == "+917569672503"
    assert ticket.data.get("name") == "Nihar"
    for k in ("visit_date", "visit_time", "project_name"):
        assert k not in ticket.data


def test_safety_net_creates_lead_for_enquiry(monkeypatch) -> None:
    """A vague enquiry (no firm date/time) still becomes a lead."""
    cm = ConversationalMemory()
    cm.facts[FACT_NAME] = _fact(FACT_NAME, "Nihar")
    cm.facts[FACT_PHONE] = _fact(FACT_PHONE, "7569672503")
    cm.facts[FACT_PROPERTY] = _fact(FACT_PROPERTY, "Skyline Heights")
    state = {"memory": cm.to_state_blob(), "tool_flow": {}, "call_surface": "voice_inbound"}
    history = [{"role": "user", "content": "Can you send me pricing details for the property?"}]
    merged: dict = {}
    calls = _patch_session(monkeypatch, state=state, history=history, merged=merged)

    result = _run(
        NokvoOneVoicePipeline.maybe_create_real_estate_lead_from_call(
            _Tenant(),
            _FakeDB2(),
            "call-enquiry",
            campaign_context={"from_phone": "7569672503"},
        )
    )
    assert result is not None
    assert result["tool"] == "leads_create"
    assert "leads_create" in calls
    assert "qualify_lead_and_schedule_visit" not in calls


def test_lead_args_are_minimal_name_and_phone_only() -> None:
    """A lead is name + phone only. Even when the call captured BHK / budget /
    area, those must NOT leak into the lead args — that context lives in the
    post-call call notes (data.handoff_note), not as structured lead fields."""
    args = NokvoOneVoicePipeline._lead_args_from_call_memory(
        memory={
            "name": "Nihar",
            "phone": "+917569672503",
            "bhk": "2BHK",
            "budget": "50 lakhs",
            "location_preference": "Gachibowli",
        },
        campaign_context={"from_phone": "+917569672503"},
        outbound_context=None,
    )
    assert set(args.keys()) <= {"name", "phone"}
    assert args.get("name") == "Nihar"
    assert "property_type" not in args
    assert "budget" not in args
    assert "location" not in args


def test_inbound_with_no_interest_keywords_still_creates_lead(monkeypatch) -> None:
    """Lead overhaul: ANY inbound real-estate call that didn't book a site visit
    becomes a lead (ANI + summary), even with no buying-interest keywords — we no
    longer require a positive interest signal."""
    cm = ConversationalMemory()
    cm.facts[FACT_PHONE] = _fact(FACT_PHONE, "7569672503")
    state = {"memory": cm.to_state_blob(), "tool_flow": {}, "call_surface": "voice_inbound"}
    # Plain chatter — no property/pricing/BHK keywords at all.
    history = [
        {"role": "user", "content": "Hi, is this the builder office?"},
        {"role": "assistant", "content": "Yes it is — how can I help?"},
        {"role": "user", "content": "Okay, I was just checking. Thanks."},
    ]
    merged: dict = {}
    calls = _patch_session(monkeypatch, state=state, history=history, merged=merged)

    result = _run(
        NokvoOneVoicePipeline.maybe_create_real_estate_lead_from_call(
            _Tenant(), _FakeDB2(), "call-chatter",
            campaign_context={"from_phone": "7569672503"},
        )
    )
    assert result is not None
    assert "leads_create" in calls
    assert "qualify_lead_and_schedule_visit" not in calls


def test_inbound_explicit_opt_out_does_not_create_lead(monkeypatch) -> None:
    """Compliance guard: an explicit 'wrong number / don't call' caller must NOT
    become a follow-up-eligible lead, even under the always-create rule."""
    cm = ConversationalMemory()
    cm.facts[FACT_PHONE] = _fact(FACT_PHONE, "7569672503")
    state = {"memory": cm.to_state_blob(), "tool_flow": {}, "call_surface": "voice_inbound"}
    history = [
        {"role": "user", "content": "Who is this? This is the wrong number, don't call me again."},
    ]
    merged: dict = {}
    calls = _patch_session(monkeypatch, state=state, history=history, merged=merged)

    result = _run(
        NokvoOneVoicePipeline.maybe_create_real_estate_lead_from_call(
            _Tenant(), _FakeDB2(), "call-optout",
            campaign_context={"from_phone": "7569672503"},
        )
    )
    assert result is None
    assert "leads_create" not in calls
