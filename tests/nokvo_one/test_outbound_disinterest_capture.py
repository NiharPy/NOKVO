"""Outbound: salesperson persona + campaign mode, and the hard rule that a
prospect who shows disinterest in ANY language is NEVER captured as a lead.

Two layers are pinned here:
  * the deterministic capture backstop ``_real_estate_opt_out`` (the gate
    ``maybe_create_real_estate_lead_from_call`` consults before creating a lead),
    now multilingual — STT emits native te/hi script, so we match that directly;
  * the system-prompt scaffold renders the salesperson persona, the campaign-mode
    arc, and the disinterest guardrail LAST (max recency) so it outranks every
    "collect name/phone" nudge above it.
"""
from __future__ import annotations

import uuid

import pytest

from app.services.agent_outbound_context import (
    OutboundCampaignContext,
    compose_outbound_system_section,
)
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline


def _opt_out(text: str) -> bool:
    return NokvoOneVoicePipeline._real_estate_opt_out(
        memory={}, history=[{"role": "user", "content": text}]
    )


# ── the capture backstop: disinterest in ANY language blocks lead creation ────

@pytest.mark.parametrize(
    "text",
    [
        # English
        "I am not interested",
        "no I don't need it",
        "please remove me from your list",
        "not looking right now",
        "leave me alone",
        "wrong number",
        # Hindi — native script (what Sarvam STT emits) + romanized
        "नहीं चाहिए",
        "मुझे interest नहीं है",
        "नहीं भाई रहने दो",
        "mujhe interest nahi hai",
        "zaroorat nahi hai",
        # Telugu — native script + romanized
        "వద్దు అండి",
        "ఇంటరెస్ట్ లేదు",
        "అవసరం లేదు",
        "interest ledu",
        "akkarledu",
    ],
)
def test_disinterest_blocks_capture_any_language(text):
    assert _opt_out(text) is True, f"disinterest not caught: {text!r}"


@pytest.mark.parametrize(
    "text",
    [
        "yes tell me more about the 3 BHK",
        "what is the price",
        "I'm looking for self-use",
        "సెల్ఫ్ యూస్ కోసం",          # Telugu "for self-use" — interested, must NOT block
        "3 BHK or 4 BHK?",
        "okay sure go ahead",
    ],
)
def test_genuine_interest_is_not_falsely_blocked(text):
    assert _opt_out(text) is False, f"false-positive opt-out on: {text!r}"


def test_opt_out_reads_extracted_objection_field():
    # The LLM-extracted objection blob is also scanned (not just raw turns).
    assert NokvoOneVoicePipeline._real_estate_opt_out(
        memory={"objection": "caller said not interested"}, history=[]
    ) is True


# ── the prompt: salesperson persona, campaign mode, disinterest last ──────────

def _ctx() -> OutboundCampaignContext:
    return OutboundCampaignContext(
        campaign_id=str(uuid.uuid4()),
        name="Skyline launch",
        goal="Pitch the new launch and book a site visit",
        agent_prompt="Pitch the new 3 & 4 BHK launch in Kokapet.",
        objectives=["lead", "site_visit"],
        exit_conditions=[],
        tone="warm",
        doc_text=None,
        caller_name="Riya",
        company_name="My Home",
        pitch_summary="3 & 4 BHK launch",
    )


def test_prompt_renders_salesperson_and_campaign_mode():
    section = compose_outbound_system_section(_ctx(), language="en", turn_index=2)
    assert "SALESPERSON PERSONA" in section
    assert "CAMPAIGN MODE" in section
    assert "CAMPAIGN THE PRODUCT" in section          # sell value before qualifying
    assert "DISINTEREST — STOP SELLING" in section


def test_disinterest_rule_is_rendered_last_even_at_late_turns():
    # turn_index=6 renders the late-turn "TURN PROGRESS" push toward the close;
    # the disinterest guardrail must still come AFTER it so recency keeps it on top.
    section = compose_outbound_system_section(_ctx(), language="en", turn_index=6)
    assert "TURN PROGRESS" in section                 # the late-turn drive-to-close block is present
    assert section.rstrip().endswith(
        "when in doubt, back off."
    ), "disinterest rule must be the final (max-recency) block"
    assert "OVERRIDES" in section and "CAPTURE NOTHING" in section


def test_disinterest_rule_names_native_script_examples():
    # The agent must recognise te/hi disinterest, so the prompt seeds native-script
    # exemplars (the LLM generalises from these to any phrasing/language).
    section = compose_outbound_system_section(_ctx(), language="en")
    assert "vaddu" in section          # Telugu disinterest exemplar (romanized)
    assert "नहीं चाहिए" in section      # Hindi disinterest exemplar (native script)
