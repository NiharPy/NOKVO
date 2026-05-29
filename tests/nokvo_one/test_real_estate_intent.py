"""Intent-capture + slot-inference coverage for the real-estate agent.

Locks the expanded visit/enquiry intent phrasings and the case-insensitive
location inference (STT emits lowercase), so paraphrases like "drop by" or
"in kokapet" stop slipping through.
"""
from __future__ import annotations

import pytest

from app.services.tool_flow_policy import (
    _LOCATION_INFER_RE,
    _infer_domain_slots,
    _start_flow_key,
)


@pytest.mark.parametrize(
    "text",
    [
        "I'd like to book a site visit",
        "Can I drop by on Saturday?",
        "I want to come and see the flat",
        "can you show me around the project",
        "I'd love to take a tour this weekend",
        "want to see the property in person",
    ],
)
def test_visit_paraphrases_start_site_visit(text):
    assert _start_flow_key(text, "real_estate", []) == "real_estate_site_visit"


@pytest.mark.parametrize(
    "text",
    [
        "just send me the brochure",
        "can you share more details",
        "I want a quote",
        "please call me back",
        "tell me about your projects",
    ],
)
def test_enquiry_paraphrases_start_lead(text):
    assert _start_flow_key(text, "real_estate", []) == "leads_create"


def test_visit_intent_wins_over_lead_when_both_present():
    # "interested" (lead) + "site visit" (visit) → visit is the stronger,
    # more specific outcome and must win.
    assert _start_flow_key(
        "I'm interested, can I book a site visit?", "real_estate", []
    ) == "real_estate_site_visit"


def test_location_regex_matches_lowercase_stt():
    m = _LOCATION_INFER_RE.search("looking for something in kokapet")
    assert m is not None
    assert m.group(1).lower().startswith("kokapet")


def _bundle_with_location_slot():
    return {"flows": {"leads_create": {"slots": [{"key": "location", "kind": "location"}]}}}


def test_infer_domain_slots_fills_lowercase_location():
    collected: dict = {}
    _infer_domain_slots("looking in kokapet", "real_estate", _bundle_with_location_slot(), "leads_create", collected)
    assert collected.get("location") == "kokapet"


def test_infer_domain_slots_skips_filler_location():
    collected: dict = {}
    _infer_domain_slots("looking in the area", "real_estate", _bundle_with_location_slot(), "leads_create", collected)
    assert "location" not in collected
