"""P4 — generic outbound objective → tool-flow gate."""
from __future__ import annotations

from app.services.outbound_objectives import allowed_flow_keys_for_objectives


def test_no_or_unknown_objectives_allow_all():
    assert allowed_flow_keys_for_objectives(None) is None
    assert allowed_flow_keys_for_objectives([]) is None
    assert allowed_flow_keys_for_objectives("") is None
    assert allowed_flow_keys_for_objectives(["nonsense"]) is None


def test_services_objectives_map_to_flows():
    assert allowed_flow_keys_for_objectives(["consultation"]) == ["clinic_appointment"]
    assert allowed_flow_keys_for_objectives(["lead"]) == ["leads_create"]
    assert allowed_flow_keys_for_objectives(["consultation", "lead"]) == [
        "clinic_appointment",
        "leads_create",
    ]


def test_aliases_normalization_and_dedupe():
    assert allowed_flow_keys_for_objectives("Site Visit") == ["real_estate_site_visit"]
    assert allowed_flow_keys_for_objectives(["appointment", "quote"]) == [
        "clinic_appointment",
        "leads_create",
    ]
    # booking + consultation both resolve to clinic_appointment → deduped
    assert allowed_flow_keys_for_objectives(["booking", "consultation"]) == ["clinic_appointment"]


def test_unknown_mixed_with_known():
    assert allowed_flow_keys_for_objectives(["nonsense", "lead"]) == ["leads_create"]
