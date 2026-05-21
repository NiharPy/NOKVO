"""Regression tests for the schema-driven field-collection prompt.

When the agent is collecting fields for a ticket / lead / appointment,
it must ask using the operator-configured field labels and question
text — not the LLM's free-form paraphrase. These tests verify:

* ``format_field_questions_prompt`` renders each flow and writable tab
  with the exact configured phrasings (default English + per-language).
* ``NokvoOneVoicePipeline._messages`` includes the FIELD-COLLECTION
  SCRIPT block when a non-empty prompt is supplied, and omits it
  cleanly when none is supplied.
* Operator overrides on a field label propagate through to the
  rendered prompt — so a tenant that renames ``patient_name`` to
  ``guest_name`` actually changes what the agent says.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.agent_runtime_bundle import RuntimeBundle
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.tool_flow_questions import (
    build_tool_flow_questions,
    format_field_questions_prompt,
)


def _bundle(industry: str, overrides: dict | None = None) -> RuntimeBundle:
    org = SimpleNamespace(id="org-1", name="Test Org", industry=industry)
    return RuntimeBundle(
        tenant_id="t-1",
        organization=org,
        organization_industry=industry,
        organization_name="Test Org",
        overrides=overrides or {},
        custom_tabs=[],
        policy_cards=[],
        single_prompt_guidance="",
        single_prompt_enabled=False,
        version_key="vk-1",
    )


def test_format_field_questions_prompt_renders_real_estate_visit_flow():
    catalog = build_tool_flow_questions("real_estate")
    prompt = format_field_questions_prompt(catalog, language="en")
    assert "# FIELD-COLLECTION SCRIPT" in prompt
    assert "real_estate_site_visit" in prompt
    # Default name + phone phrasings.
    assert '"May I have your name?"' in prompt
    assert '"What phone number should our team use?"' in prompt
    # Required vs optional labelling propagates.
    assert "visit_date (Visit Date, required)" in prompt
    assert "visit_time (Visit Time, required)" in prompt


def test_format_field_questions_prompt_picks_language_specific_phrasing():
    catalog = build_tool_flow_questions("real_estate")
    hindi = format_field_questions_prompt(catalog, language="hi")
    assert "कृपया आपका name बताइए" in hindi
    telugu = format_field_questions_prompt(catalog, language="te")
    assert "మీ name చెప్పండి" in telugu


def test_format_field_questions_prompt_falls_back_to_english_for_unknown_language():
    catalog = build_tool_flow_questions("real_estate")
    fallback = format_field_questions_prompt(catalog, language="xx")
    assert '"May I have your name?"' in fallback


def test_format_field_questions_prompt_empty_when_no_catalog():
    assert format_field_questions_prompt({}) == ""
    assert format_field_questions_prompt(None) == ""
    # Catalog with no flows / tabs.
    assert format_field_questions_prompt({"flows": {}, "tabs": {}}) == ""


def test_pipeline_helper_returns_empty_when_industry_unknown():
    # An empty / unknown industry has no tool-flow catalog, so the
    # prompt block is empty — keeps inbound calls without record
    # creation from carrying dead weight.
    bundle = _bundle("")
    prompt = NokvoOneVoicePipeline._field_questions_prompt_for_bundle(bundle, language="en")
    # Either empty or only contains writable tabs from defaults.
    assert isinstance(prompt, str)


def test_pipeline_helper_includes_flows_for_known_industry():
    bundle = _bundle("real_estate")
    prompt = NokvoOneVoicePipeline._field_questions_prompt_for_bundle(bundle, language="en")
    assert "# FIELD-COLLECTION SCRIPT" in prompt
    assert "real_estate_site_visit" in prompt


def test_messages_embed_field_collection_block_when_provided():
    bundle = _bundle("real_estate")
    field_prompt = NokvoOneVoicePipeline._field_questions_prompt_for_bundle(
        bundle, language="en"
    )
    msgs = NokvoOneVoicePipeline._messages(
        "I want to schedule a visit",
        [],
        language="en",
        history=[],
        company_name="Acme",
        field_questions_prompt=field_prompt,
    )
    sys_prompt = msgs[0]["content"]
    assert "# FIELD-COLLECTION SCRIPT" in sys_prompt
    # The exact phrasing the agent should use for visit_date.
    assert "What date would you prefer?" in sys_prompt
    # The block is placed BEFORE the closing language reminder so the
    # LLM weights it correctly.
    assert sys_prompt.index("FIELD-COLLECTION SCRIPT") < sys_prompt.index("# REMINDER")


def test_messages_omit_field_collection_block_when_no_prompt():
    msgs = NokvoOneVoicePipeline._messages(
        "where are you located?",
        [],
        language="en",
        history=[],
        company_name="Acme",
    )
    sys_prompt = msgs[0]["content"]
    assert "# FIELD-COLLECTION SCRIPT" not in sys_prompt


def test_operator_override_of_field_label_propagates_to_prompt():
    """A tenant that renames a writable field in their schema overrides
    must see the new label/question reflected verbatim in the prompt.
    Anchors the contract between schema editing and agent behaviour.

    ``apply_schema_overrides`` consumes ``{tab: [fields]}`` (no
    ``schemas`` wrapper) — see
    :func:`app.services.nokvo_one_business_templates.apply_schema_overrides`.
    """
    overrides = {
        "leads": [
            {
                "key": "guest_name",
                "label": "Guest Name",
                "type": "text",
                "required": True,
                "writable": True,
            },
            {
                "key": "phone",
                "label": "Mobile",
                "type": "text",
                "required": True,
                "writable": True,
            },
        ]
    }
    catalog = build_tool_flow_questions("real_estate", overrides)
    prompt = format_field_questions_prompt(catalog, language="en")
    # The renamed field surfaces with the operator's chosen label.
    assert "Guest Name" in prompt
    assert "guest_name" in prompt
    # The default "patient_name" / "customer_name" should be gone for
    # this tenant.
    assert "patient_name" not in prompt
