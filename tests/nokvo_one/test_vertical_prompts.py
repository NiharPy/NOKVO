"""Tests for per-vertical prompts + the bundle's prompt composition."""
from __future__ import annotations

from app.services.vertical_prompts import (
    VERTICAL_SYSTEM_PROMPTS,
    system_prompt_for_vertical,
)
from app.services.agent_runtime_bundle import _single_prompt_guidance


def test_all_five_verticals_present_and_distinct():
    assert set(VERTICAL_SYSTEM_PROMPTS) == {
        "real_estate", "clinics", "ecommerce", "hospitality", "other",
    }
    # Each carries its domain marker.
    assert "site visit" in system_prompt_for_vertical("real_estate").lower()
    _clinic = system_prompt_for_vertical("clinics").lower()
    assert "diagnose" in _clinic and "emergency" in _clinic  # safety guardrails present
    assert "order" in system_prompt_for_vertical("ecommerce").lower()
    assert "reservation" in system_prompt_for_vertical("hospitality").lower()


def test_resolver_normalizes_and_falls_back():
    assert system_prompt_for_vertical("Real Estate") == VERTICAL_SYSTEM_PROMPTS["real_estate"]
    assert system_prompt_for_vertical(None) == VERTICAL_SYSTEM_PROMPTS["other"]
    assert system_prompt_for_vertical("nonexistent") == VERTICAL_SYSTEM_PROMPTS["other"]


def test_bundle_composes_vertical_only_when_no_facts():
    g = _single_prompt_guidance({}, "clinics")
    assert g == VERTICAL_SYSTEM_PROMPTS["clinics"]
    assert "# BUSINESS FACTS" not in g


def test_bundle_appends_business_facts():
    g = _single_prompt_guidance({"business_facts": "Open 9-6. Dr. Rao Tue/Thu."}, "clinics")
    assert g.startswith(VERTICAL_SYSTEM_PROMPTS["clinics"])
    assert "# BUSINESS FACTS" in g
    assert "Dr. Rao" in g


def test_bundle_backfills_legacy_single_prompt_as_facts():
    ps = {"single_prompt_voice_agent": {"enabled": True, "prompt": "We are ACME Realty, Hyderabad."}}
    g = _single_prompt_guidance(ps, "real_estate")
    assert "ACME Realty" in g
    assert "site visit" in g.lower()  # vertical persona still present


def test_explicit_business_facts_wins_over_legacy():
    ps = {
        "business_facts": "New facts win.",
        "single_prompt_voice_agent": {"enabled": True, "prompt": "Old legacy text."},
    }
    g = _single_prompt_guidance(ps, "other")
    assert "New facts win." in g
    assert "Old legacy text." not in g
