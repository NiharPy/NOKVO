from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.outgoing_lead import LeadCallStatus, LeadConsentStatus, LeadSourceProvider, OutgoingLead
from app.models.outbound_campaign import OutboundCampaign
from app.services.agent_outbound_context import (
    DEFAULT_OBJECTIVES,
    OutboundCampaignContext,
    build_agent_config,
    compose_outbound_system_section,
    infer_covered_objectives,
)
from app.services.outbound_campaign_service import OutboundCampaignService


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


class _Upload:
    filename = "script.txt"

    async def read(self):
        return b"Call warm clinic leads and help them book an appointment."


class _FakeDb:
    def __init__(self):
        self.added = []
        self.commits = 0

    def add(self, item):
        self.added.append(item)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _item):
        return None


def _lead(**overrides):
    base = {
        "id": uuid.uuid4(),
        "tenant_id": "tenant-proactive",
        "source_provider": LeadSourceProvider.meta_ads,
        "name": "Asha Rao",
        "email": "asha@example.com",
        "phone_e164": "+919999999999",
        "consent_status": LeadConsentStatus.granted,
        "consented_at": datetime.now(timezone.utc),
        "call_status": LeadCallStatus.new,
        "provider_lead_id": "lead-1",
    }
    base.update(overrides)
    return OutgoingLead(**base)


def test_build_agent_config_defaults_make_campaign_proactive():
    config = build_agent_config()

    assert config["agent_prompt"]
    assert config["objectives"] == DEFAULT_OBJECTIVES
    assert config["exit_conditions"]
    assert config["silence_timeout_seconds"] == 5.0


def test_outbound_system_section_includes_pending_objectives_only():
    context = OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="Dry eye follow-up",
        goal="Book interested dry-eye patients",
        agent_prompt="Call as Rohit Eye Clinic.",
        objectives=["Confirm time to talk", "Book appointment"],
        exit_conditions=["Not interested"],
        tone="warm",
        doc_text=None,
        silence_timeout_seconds=4,
    )

    section = compose_outbound_system_section(
        context,
        covered_objectives=["Confirm time to talk"],
    )

    assert "PROACTIVE MODE" in section
    assert "Call as Rohit Eye Clinic." in section
    assert "Book appointment" in section
    assert "Still pending this call" in section


def test_infer_covered_objectives_tracks_discussed_items_without_llm():
    context = OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="Clinic campaign",
        goal="Book appointments",
        agent_prompt="",
        objectives=[
            "Confirm this is a good time to talk.",
            "Capture the next step: appointment or callback.",
        ],
        exit_conditions=[],
        tone=None,
        doc_text=None,
    )

    covered = infer_covered_objectives(
        context,
        caller_text="Yes, this is a good time. I want an appointment tomorrow.",
        agent_answer="I can help book that appointment.",
        already_covered=[],
    )

    assert context.objectives[0] in covered
    assert context.objectives[1] in covered


def test_create_campaign_from_leads_persists_proactive_config(monkeypatch):
    lead = _lead()

    async def _validate(_tenant, _db, lead_ids):
        assert lead_ids == [lead.id]
        return [lead]

    async def _index(*_args, **_kwargs):
        return 3

    monkeypatch.setattr(
        "app.services.outgoing_lead_service.OutgoingLeadService.validate_callable_leads",
        staticmethod(_validate),
    )
    monkeypatch.setattr(OutboundCampaignService, "_index_campaign_script", staticmethod(_index))

    tenant = SimpleNamespace(
        tenant_id="tenant-proactive",
        organization_id=uuid.uuid4(),
        provider_status={},
        twilio_phone_number="+918888888888",
    )
    db = _FakeDb()

    campaign = _run(
        OutboundCampaignService.create_campaign_from_leads(
            tenant,
            db,
            name="Clinic follow-up",
            lead_ids=[lead.id],
            doc_file=_Upload(),
            agent_config={
                "agent_prompt": "You are calling warm clinic leads.",
                "objectives": ["Confirm interest", "Book appointment"],
                "exit_conditions": ["Opt out"],
                "tone": "calm",
                "silence_timeout_seconds": 3,
            },
        )
    )

    assert isinstance(campaign, OutboundCampaign)
    assert campaign.agent_config["agent_prompt"] == "You are calling warm clinic leads."
    assert campaign.agent_config["objectives"] == ["Confirm interest", "Book appointment"]
    assert campaign.agent_config["exit_conditions"] == ["Opt out"]
    assert campaign.agent_config["tone"] == "calm"
    assert campaign.agent_config["silence_timeout_seconds"] == 3.0
    assert campaign.contacts[0]["script_indexed_points"] == 3
    assert lead.call_status == LeadCallStatus.queued
