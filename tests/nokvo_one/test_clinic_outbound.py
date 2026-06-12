"""Clinic outbound (admin-commanded follow-up) — synthetic context, prompt
composition, triage safety, and the business-type branch of the outbound
FSM mode block.
"""
from __future__ import annotations

import asyncio
import uuid

from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    compose_outbound_system_section,
)
from app.services.clinic_outbound_agent_fsm import (
    AGENT_MODE_APPOINTMENT_BOOKING,
    AGENT_MODE_FOLLOWUP,
    AGENT_MODE_TRIAGE_ESCALATION,
    current_mode,
    enabled_for_business_type,
    mode_block_for_prompt,
)
from app.services.clinic_outbound_context import build_clinic_followup_context


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── FSM gating + mode derivation ────────────────────────────────────────────


def test_clinic_outbound_fsm_gates_to_clinics():
    assert enabled_for_business_type("clinics") is True
    assert enabled_for_business_type("real_estate") is False
    assert enabled_for_business_type(None) is False


def test_triage_overrides_everything_on_outbound():
    mode = current_mode(
        {"appointment": {"active": True}},
        latest_user_text="I have chest pain right now",
    )
    assert mode == AGENT_MODE_TRIAGE_ESCALATION
    block = mode_block_for_prompt(mode)
    assert "TRIAGE" in block
    assert "112" in block or "108" in block
    # The follow-up agenda must be dropped, never the patient.
    assert "STOP the follow-up agenda" in block


def test_appointment_flow_drives_booking_mode():
    mode = current_mode({"appointment": {"active": True, "completed": False}})
    assert mode == AGENT_MODE_APPOINTMENT_BOOKING
    block = mode_block_for_prompt(mode, pending_slot_label="Phone")
    assert "APPOINTMENT_BOOKING" in block
    assert "Phone" in block
    assert "NEVER diagnose" in block


def test_default_mode_is_followup_with_no_diagnosis_guardrail():
    mode = current_mode({}, latest_user_text="hello, who is this?")
    assert mode == AGENT_MODE_FOLLOWUP
    block = mode_block_for_prompt(mode)
    assert "FOLLOWUP" in block
    assert "REASON FOR THIS CALL" in block
    assert "NEVER diagnose" in block


# ── Synthetic context ───────────────────────────────────────────────────────


def test_clinic_followup_context_carries_note_and_catalog_fallback():
    ctx = _run(
        build_clinic_followup_context(
            None,  # no db → services catalog falls back to the guard text
            organization_id=uuid.uuid4(),
            admin_note="Remind them about tomorrow's 10 AM cleaning",
            company_name="Smile Dental",
        )
    )
    assert ctx.is_proactive
    assert ctx.objectives == ["Remind them about tomorrow's 10 AM cleaning"]
    assert ctx.goal == "Remind them about tomorrow's 10 AM cleaning"
    assert ctx.company_name == "Smile Dental"
    assert "Do not invent services or prices" in (ctx.doc_text or "")
    # Medical safety travels with the persona.
    assert "NEVER diagnose" in ctx.agent_prompt


def test_clinic_followup_context_without_note_still_proactive():
    ctx = _run(
        build_clinic_followup_context(
            None, organization_id=uuid.uuid4(), admin_note=None
        )
    )
    assert ctx.is_proactive
    assert ctx.objectives  # falls back to a generic check-in objective


# ── Prompt composition: business-type branch + admin note ───────────────────


def _ctx(**overrides):
    base = dict(
        campaign_id=str(uuid.uuid4()),
        name="Clinic follow-up",
        goal="Remind about appointment",
        agent_prompt="You are the clinic front desk calling back.",
        objectives=["Remind about appointment"],
        exit_conditions=["Customer asks not to be called again."],
        tone="warm",
        doc_text=None,
        silence_timeout_seconds=4,
    )
    base.update(overrides)
    return OutboundCampaignContext(**base)


def test_clinic_business_type_renders_clinic_mode_block_not_real_estate():
    section = compose_outbound_system_section(_ctx(), business_type="clinics")
    assert "AGENT MODE — FOLLOWUP" in section
    # No real-estate FSM guidance on a clinic call (the generic turn rules
    # block is shared, so we assert on the mode blocks specifically).
    assert "AGENT MODE — SALE" not in section
    assert "AGENT MODE — SITE_VISIT" not in section
    assert "AGENT MODE — OUTBOUND_LEAD" not in section
    assert "DO NOT INVENT LEAD PREFERENCES" not in section


def test_default_business_type_keeps_real_estate_block():
    # Legacy callers don't pass business_type — current behaviour (the RE
    # mode block) must be preserved exactly.
    section_default = compose_outbound_system_section(_ctx())
    section_re = compose_outbound_system_section(_ctx(), business_type="real_estate")
    assert "AGENT MODE — SALE" in section_default
    assert "AGENT MODE — SALE" in section_re


def test_clinic_outbound_triage_reachable_from_latest_user_text():
    section = compose_outbound_system_section(
        _ctx(),
        business_type="clinics",
        latest_user_text="my father can't breathe",
    )
    assert "TRIAGE" in section


def test_admin_note_renders_as_reason_for_this_call():
    section = compose_outbound_system_section(
        _ctx(),
        business_type="clinics",
        outbound_memory={
            "followup": {
                "is_followup": True,
                "attempt_n": 1,
                "handoff_note": "Asked about dental cleaning prices last week.",
                "admin_note": "Offer the June cleaning discount",
            }
        },
    )
    assert "REASON FOR THIS CALL — admin instruction" in section
    assert "Offer the June cleaning discount" in section
    # Prior-call notes render too (follow-up preamble).
    assert "Asked about dental cleaning prices last week." in section
    # The admin note (the agenda) must appear before the prior-call notes.
    assert section.index("REASON FOR THIS CALL — admin instruction") < section.index(
        "FOLLOW-UP CALL"
    )


def test_no_admin_note_renders_no_reason_block():
    section = compose_outbound_system_section(
        _ctx(),
        business_type="clinics",
        outbound_memory={
            "followup": {"is_followup": True, "attempt_n": 1, "handoff_note": ""}
        },
    )
    assert "REASON FOR THIS CALL — admin instruction" not in section
