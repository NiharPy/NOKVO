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
    generate_outbound_opener_text,
    infer_covered_objectives,
    outbound_memory_as_facts,
    render_outbound_memory,
    update_outbound_memory,
)
from app.services.conversational_memory import (
    FACT_BHK,
    FACT_BUDGET,
    ConversationalMemory,
    seed_facts,
)
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.outbound_campaign_service import OutboundCampaignService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


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
    assert config["silence_timeout_seconds"] == 10.0  # nudge→cut silence window


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


# ── Gap 2: word-number budget in outbound memory ─────────────────────────────


def test_outbound_system_section_declares_factual_scope():
    """The outbound system prompt must explicitly forbid drawing on the inbound
    knowledge base / property inventory / admin single-prompt — campaign brief
    is the entire factual scope. Regression for: outbound agent improvising
    product facts from inbound resources."""
    context = OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="RE outreach",
        goal="Re-engage enquiry leads",
        agent_prompt="",
        objectives=["Book site visit"],
        exit_conditions=[],
        tone="warm",
        doc_text=None,
        caller_name="Riya",
        company_name="Raghava",
        pitch_summary="our new project",
    )
    section = compose_outbound_system_section(context)
    assert "OUTBOUND FACTUAL SCOPE" in section
    assert "knowledge base" in section.lower()
    assert "property inventory" in section.lower() or "inventory" in section.lower()
    assert "general world knowledge" in section.lower() or "training data" in section.lower()


def test_outbound_memory_budget_handles_word_numbers():
    """The outbound extractor now reuses the inbound budget parser, so spelled-out
    amounts a human understands are captured — not just digit+unit."""
    m = update_outbound_memory({}, caller_text="my budget is quite limited, maybe half a crore")
    assert m["budget"] == "0.5 crore"
    m2 = update_outbound_memory({}, caller_text="we can stretch to one and a half crore")
    assert m2["budget"] == "1.5 crore"


# ── Gap 3: next-step typing + objection parity + coverage tightening ─────────


def test_outbound_memory_distinguishes_callback_from_site_visit():
    cb = update_outbound_memory({}, caller_text="just call me back next week")
    assert cb["next_step"] == "callback"
    sv = update_outbound_memory({}, caller_text="sure, I'll come visit the site on Saturday")
    assert sv["next_step"] == "site_visit"


def test_outbound_memory_captures_competitor_objection():
    """Outbound now recognises the full inbound objection vocabulary, not just
    not-interested / price."""
    m = update_outbound_memory({}, caller_text="I already booked with another builder")
    assert m["objection"] == "competitor"


def test_infer_covered_objectives_requires_real_next_step_action():
    """A next-step objective is NOT covered by incidental token overlap — only
    when an actual next-step action is discussed."""
    context = OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="RE campaign",
        goal="Book site visits",
        agent_prompt="",
        objectives=["Capture the next step: appointment, callback, or site visit."],
        exit_conditions=[],
        tone=None,
        doc_text=None,
    )
    # No next step discussed — only generic chit-chat that happens to share a
    # word ("step") would have falsely covered it before.
    not_covered = infer_covered_objectives(
        context,
        caller_text="watch your step there, it's slippery",
        agent_answer="noted",
        already_covered=[],
    )
    assert context.objectives[0] not in not_covered
    # A real next step — covered.
    covered = infer_covered_objectives(
        context,
        caller_text="okay, let's set up a site visit",
        agent_answer="great, I'll schedule the visit",
        already_covered=[],
    )
    assert context.objectives[0] in covered


# ── Gap 1: outbound memory feeds the structured ConversationalMemory ─────────


def test_outbound_memory_as_facts_maps_to_canonical_keys():
    facts = outbound_memory_as_facts({"bhk": "3 BHK", "budget": "0.5 crore", "visit_preference": "weekend"})
    assert facts[FACT_BHK] == "3 BHK"
    assert facts[FACT_BUDGET] == "0.5 crore"
    # Free-form keys without a canonical fact are not mapped.
    assert "weekend" not in facts.values()


def test_seed_facts_fills_gaps_without_overriding_in_call_extraction():
    mem = ConversationalMemory()
    # In-call extraction captures a high-confidence BHK.
    mem.merge_text("I want a 4 BHK", turn_index=1, business_type="real_estate")
    # Seeding from outbound memory must NOT clobber it, but SHOULD fill a gap.
    seed_facts(mem, {FACT_BHK: "2 BHK", FACT_BUDGET: "0.5 crore"})
    assert mem.get(FACT_BHK) == "4 BHK"  # in-call value wins
    assert mem.get(FACT_BUDGET) == "0.5 crore"  # gap filled


# ── Gap 5: personalised opener ───────────────────────────────────────────────


def _opener_context() -> OutboundCampaignContext:
    return OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="RE outreach",
        goal="Re-engage enquiry leads",
        agent_prompt="",
        objectives=["Book site visit"],
        exit_conditions=[],
        tone="warm",
        doc_text=None,
        caller_name="Riya",
        company_name="Raghava",
        pitch_summary="our new project",
    )


def test_opener_personalises_from_lead_enquiry():
    text = generate_outbound_opener_text(
        _opener_context(),
        language="en",
        known_facts={"name": "Nihar", "bhk": "3 BHK", "location": "Gachibowli"},
    )
    assert "Nihar" in text
    assert "3 BHK" in text
    assert "Gachibowli" in text
    assert "enquired about" in text


def test_opener_returning_lead_defers_to_call_notes():
    # A returning (follow-up) lead gets a warm, notes-deferring greeting — it
    # names the lead and references the prior conversation, but does NOT try to
    # reconstruct enquiry specifics (bhk/location). The LLM states those from
    # the call notes on turn 2.
    text = generate_outbound_opener_text(
        _opener_context(),
        language="en",
        known_facts={"name": "Asha", "bhk": "2 BHK", "returning": True},
    )
    assert "Asha" in text
    assert "follow up on our last conversation" in text.lower()
    # The bhk specifics must NOT appear in the deterministic opener anymore.
    assert "2 BHK" not in text


def test_opener_falls_back_to_pitch_when_nothing_known():
    text = generate_outbound_opener_text(_opener_context(), language="en", known_facts={})
    assert "our new project" in text
    assert "this is Riya from Raghava" in text


def test_opener_personalises_in_hindi_and_telugu():
    """Multilingual parity — personalisation isn't English-only."""
    hi = generate_outbound_opener_text(
        _opener_context(), language="hi", known_facts={"name": "Nihar", "bhk": "3 BHK"}
    )
    assert "Nihar" in hi and "3 BHK" in hi
    te = generate_outbound_opener_text(
        _opener_context(), language="te", known_facts={"name": "Asha", "location": "Gachibowli"}
    )
    assert "Asha" in te and "Gachibowli" in te


# ── Follow-up preamble: call notes are the single source of prior-call context ──


def _followup_context() -> OutboundCampaignContext:
    return OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="RE follow-up",
        goal="Re-engage and book a site visit",
        agent_prompt="",
        objectives=["Book site visit"],
        exit_conditions=[],
        tone="warm",
        doc_text=None,
        caller_name="Riya",
        company_name="Raghava",
        pitch_summary="our new project",
    )


def test_followup_preamble_quotes_call_notes_when_present():
    note = "Asha wanted a 2 BHK near Gachibowli. She hesitated on the price. We promised to share a payment plan."
    section = compose_outbound_system_section(
        _followup_context(),
        outbound_memory={"followup": {"is_followup": True, "attempt_n": 2, "handoff_note": note}},
    )
    assert "FOLLOW-UP CALL" in section
    assert note in section  # the prose note is quoted verbatim
    assert "referencing these notes" in section


def test_followup_preamble_minimal_when_no_note_no_structured_dump():
    # When the condenser produced no note, the preamble must NOT reconstruct a
    # structured objections/commitments/preferences dump — just a minimal warm
    # acknowledgement (plus the cheap prior-promise field when present).
    section = compose_outbound_system_section(
        _followup_context(),
        outbound_memory={
            "followup": {
                "is_followup": True,
                "attempt_n": 1,
                "handoff_note": "",
                "prior_promise": "call me tomorrow at 5pm",
            }
        },
    )
    assert "FOLLOW-UP CALL" in section
    assert "spoke with this person recently" in section
    assert "call me tomorrow at 5pm" in section  # prior promise surfaced
    # The retired structured-dump lines must be gone.
    assert "Objections:" not in section
    assert "Commitments:" not in section
    assert "Preferences:" not in section
