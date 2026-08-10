"""P3 — the `services` vertical (interior design's home).

Proves services is a first-class config: selectable when enabled, resolves the
full CRUD toolset for its tabs, builds both the lead-capture and the (generic)
appointment flow, and books an appointment end-to-end through the unified engine
with no clinic-specific code.
"""
from __future__ import annotations

from app.core.config import settings
from app.services.nokvo_one_business_templates import (
    business_type_config,
    validate_business_type,
)
from app.services.dynamic_tool_resolver import resolve_index
from app.services.tool_flow_questions import build_tool_flow_questions
from app.services.tool_flow_policy import (
    _flow_action,
    _start_flow_key,
    evaluate_tool_flow_policy,
)


def test_services_is_a_valid_configured_type():
    cfg = business_type_config("services")
    assert cfg is not None and cfg["value"] == "services"
    assert cfg["tabs"] == ["tickets", "appointments", "leads"]


def test_services_selectable_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "ENABLED_BUSINESS_TYPES", "services")
    assert validate_business_type("services") == "services"


def test_services_resolves_full_toolset():
    keys = set(resolve_index("services", None, None).keys())
    for tab in ("leads", "appointments", "tickets"):
        for verb in ("create", "list", "search"):
            assert f"{tab}_{verb}" in keys, f"{tab}_{verb}"
    assert "schedule_callback" in keys and "escalate_to_human" in keys


def test_services_builds_appointment_and_lead_flows():
    flows = (build_tool_flow_questions("services", None, []).get("flows")) or {}
    assert "clinic_appointment" in flows  # the generic appointment flow
    assert "leads_create" in flows
    assert flows["clinic_appointment"]["tool_key"] == "book_appointment_with_lead_capture"


def test_services_appointment_intent_starts_flow():
    assert _start_flow_key("I want to book a design consultation", "services", []) == "clinic_appointment"


def test_services_appointment_books_via_unified_engine():
    state: dict = {}
    history: list = []
    turns = ["I want to book a design consultation", "Nihar Reddy", "yes",
             "9876543210", "yes", "tomorrow at 4 pm"]
    for u in turns:
        res = evaluate_tool_flow_policy(
            u, business_type="services", schema_overrides=None, custom_tabs=None,
            provider_status={}, history=history, state=state, language="en",
        )
        if res is not None:
            for k, v in (res.get("state_patch") or {}).items():
                state[k] = v
        history += [{"role": "user", "content": u},
                    {"role": "assistant", "content": (res or {}).get("answer") or "..."}]

    tf = state.get("tool_flow", {})
    assert tf.get("completed") is True, tf
    action = _flow_action(tf)
    assert action["tool_key"] == "book_appointment_with_lead_capture"
    args = action["arguments"]
    assert args.get("patient_name") == "Nihar Reddy"  # the "name" slot maps to patient_name
    assert args.get("phone") == "9876543210"
    assert "appointment_time" in args
