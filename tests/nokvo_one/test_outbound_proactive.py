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
    render_outbound_memory,
    update_outbound_memory,
)
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
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
    assert config["silence_timeout_seconds"] == 12.0


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

    # Outbound system fragment now leads with one of two headings —
    # SALES / OUTREACH PERSONA (templated) or CUSTOM PERSONA (operator
    # supplied an explicit agent_prompt). Either way the "OUTBOUND
    # CAMPAIGN" anchor must appear.
    assert "OUTBOUND CAMPAIGN" in section
    assert "Call as Rohit Eye Clinic." in section
    assert "Book appointment" in section
    assert "Still pending this call" in section
    assert "Listen to the latest caller message" in section
    assert "1 to 2 short sentences" in section
    assert "Ask at most one question per turn" in section
    assert "Do not push past their answer" in section


def test_outbound_system_section_listens_before_advancing_objectives():
    context = OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="Site visit follow-up",
        goal="Book interested leads for a site visit",
        agent_prompt="",
        objectives=[
            "Confirm this is a good time to talk",
            "Understand BHK preference",
            "Book site visit",
        ],
        exit_conditions=["Not interested"],
        tone="warm",
        doc_text=None,
        silence_timeout_seconds=4,
        caller_name="Riya",
        company_name="Raghava Skyline",
        pitch_summary="premium apartments in Hyderabad",
    )

    section = compose_outbound_system_section(context)

    assert "LISTEN FIRST" in section
    assert "latest caller utterance wins" in section
    assert "If they asked a question, answer it briefly before moving on" in section
    assert "The objective list is a guide, not permission to monologue" in section


def test_outbound_permission_after_opener_asks_discovery_not_pitch():
    context = OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="Site visit follow-up",
        goal="Confirm if the prospect can come for site visit",
        agent_prompt="",
        objectives=[
            "Introduce Raghava Skyline briefly.",
            "Identify whether the customer is buying for self-use or investment.",
            "Book site visit.",
        ],
        exit_conditions=["Not interested"],
        tone="warm",
        doc_text=None,
        caller_name="Riya",
        company_name="Raghava Constructions",
        pitch_summary="Raghava Skyline in Kokapet",
    )

    answer = NokvoOneVoicePipeline._outbound_post_opener_permission_reply(
        "Yeah",
        language="en",
        history=[
            {
                "role": "assistant",
                "content": "Hi, this is Riya. Is now a good time to talk for a minute?",
            }
        ],
        outbound_context=context,
        covered_objectives=[],
    )

    assert answer == "Great, is this for self-use or investment?"
    assert "Raghava" not in answer
    assert "Kokapet" not in answer


def test_outbound_memory_extracts_and_renders_customer_details():
    memory = update_outbound_memory(
        {},
        caller_text="My name is Nihar, looking for 4 BHK around 1.2 crore for self-use.",
    )
    memory = update_outbound_memory(memory, caller_text="Weekend morning is better, send pricing on WhatsApp.")

    assert memory["name"] == "Nihar"
    assert memory["bhk"] == "4 BHK"
    assert memory["budget"] == "1.2 crore"
    assert memory["purpose"] == "self-use"
    assert "weekend" in memory["visit_preference"]
    assert "pricing" in memory["requested_info"]

    rendered = render_outbound_memory(memory)
    assert "CONVERSATION MEMORY" in rendered
    assert "Do not ask for them again" in rendered
    assert "BHK preference: 4 BHK" in rendered


def test_outbound_system_section_includes_memory_to_prevent_reasking():
    context = OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="Site visit follow-up",
        goal="Book interested leads for a site visit",
        agent_prompt="",
        objectives=["Understand BHK preference", "Book site visit"],
        exit_conditions=["Not interested"],
        tone="warm",
        doc_text=None,
    )

    section = compose_outbound_system_section(
        context,
        outbound_memory={"name": "Nihar", "bhk": "4 BHK", "budget": "1.2 crore"},
    )

    assert "CONVERSATION MEMORY" in section
    assert "Name: Nihar" in section
    assert "BHK preference: 4 BHK" in section


def test_outbound_messages_put_latest_reply_before_campaign_brief():
    context = OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="Site visit follow-up",
        goal="Book interested leads for a site visit",
        agent_prompt="",
        objectives=["Understand BHK preference", "Book site visit"],
        exit_conditions=["Not interested"],
        tone="warm",
        doc_text=None,
    )

    messages = NokvoOneVoicePipeline._messages(
        "I want 4 BHK, budget is 1.2 crore.",
        [{"text": "Raghava Skyline has 2, 3, and 4 BHK homes in Kokapet."}],
        language="en",
        history=[],
        outbound_context=context,
        outbound_memory={"bhk": "4 BHK", "budget": "1.2 crore"},
    )

    assert "CONVERSATION MEMORY" in messages[0]["content"]
    assert "BHK preference: 4 BHK" in messages[0]["content"]
    assert messages[-1]["content"].startswith("Latest prospect reply")
    assert messages[-1]["content"].find("I want 4 BHK") < messages[-1]["content"].find("Campaign brief context")


def test_outbound_route_turn_short_circuits_to_rag():
    """Outbound turns must skip Tier-2 LLM classifier + Qdrant prefetch +
    out-of-scope re-retrieval (~500-800ms saved per turn). The route layer
    short-circuits to ``route=rag`` so the LLM call fires immediately."""
    import asyncio
    from unittest.mock import patch, MagicMock
    from types import SimpleNamespace
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline

    context = OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="Realty outbound",
        goal="Book a visit",
        agent_prompt="",
        objectives=["Confirm BHK preference"],
        exit_conditions=[],
        tone=None,
        doc_text="Raghava has 2/3/4 BHK in Kokapet.",
        silence_timeout_seconds=8.0,
        caller_name="Riya",
        company_name="Raghava",
        pitch_summary="2BHK from 78L",
        objective="lead_qualification",
    )

    tenant_res = SimpleNamespace(tenant_id="t", provider_status={}, organization_id="org")

    async def _run():
        # If we touched the LLM classifier or Qdrant retrieve we'd have to
        # mock them; the short-circuit means neither is called. Spy to
        # confirm that.
        with patch(
            "app.services.nokvo_one_voice_pipeline.LLMIntentClassifier.classify",
            new=MagicMock(side_effect=AssertionError("classifier must not run for outbound")),
        ), patch.object(
            NokvoOneVoicePipeline,
            "retrieve",
            new=MagicMock(side_effect=AssertionError("Qdrant retrieval must not run for outbound")),
        ):
            return await NokvoOneVoicePipeline._route_turn(
                tenant_res,
                "Yes",
                language="en",
                company_name="Raghava",
                call_id="call-1",
                outbound_context=context,
            )

    route = asyncio.new_event_loop().run_until_complete(_run())
    assert route["route"] == "rag"
    assert route["answer"] is None
    assert route.get("prefetched_retrieval") is None


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
