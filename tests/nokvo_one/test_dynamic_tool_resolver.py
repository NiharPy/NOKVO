"""Unit tests for the Nokvo One dynamic tool resolver.

These are pure tests — no DB, no async — and cover:
  - Catalog shape per business_type.
  - Schema-override propagation into generated input schemas.
  - Status vocab → enum injection.
  - Group-by-tab UI shaping.
  - Legacy key aliases + cross-cutting validation.
"""
from __future__ import annotations

import pytest

from app.services.dynamic_tool_resolver import (
    catalog_as_groups,
    default_tool_keys,
    resolve_catalog,
    resolve_index,
    validate_resolved_tool_keys,
)
from app.services.predefined_tools_service import (
    CROSS_CUTTING_CATALOG,
    LEGACY_KEY_ALIASES,
    resolve_legacy_key,
    validate_cross_cutting_keys,
)


# ─────────── Catalog shape ───────────


def test_real_estate_catalog_contains_leads_and_tickets_only():
    keys = {t.key for t in resolve_catalog("real_estate")}
    # leads and tickets present
    assert "leads_create" in keys
    assert "tickets_create" in keys
    # appointments are NOT enabled for real_estate
    assert "appointments_create" not in keys


def test_clinics_catalog_contains_appointments_and_tickets_no_leads():
    # Clinics are Tickets + Appointments. There is NO leads tab — every
    # caller is captured in the Customer base instead, so no leads tools.
    keys = {t.key for t in resolve_catalog("clinics")}
    for tab in ("tickets", "appointments"):
        for verb in ("create", "update", "get", "list", "search", "add_note", "set_status", "close"):
            assert f"{tab}_{verb}" in keys, f"missing {tab}_{verb}"
    assert not any(k.startswith("leads_") for k in keys), "clinics should not emit leads tools"


def test_cross_cutting_tools_present_in_every_catalog():
    cross_keys = {t.key for t in CROSS_CUTTING_CATALOG}
    for bt in ("real_estate", "clinics", "ecommerce", "hospitality", "other"):
        keys = {t.key for t in resolve_catalog(bt)}
        assert cross_keys.issubset(keys), f"{bt} missing cross-cutting tools"


def test_unknown_business_type_returns_cross_cutting_only():
    catalog = resolve_catalog("not_a_real_type")
    assert {t.key for t in catalog} == {t.key for t in CROSS_CUTTING_CATALOG}


# ─────────── Schema generation ───────────


def test_leads_create_schema_has_required_phone_for_real_estate():
    index = resolve_index("real_estate")
    schema = index["leads_create"].input_schema
    # real_estate.leads marks `name` and `phone` required, `status` is system-managed.
    assert "phone" in schema["properties"]
    assert "phone" in schema["required"]
    assert "status" not in schema["properties"], "status must not be writable on create"


def test_schema_override_extends_field_schema():
    # Use ecommerce as the fixture vertical — clinics no longer have a leads
    # tab, so the override mechanics are exercised on a vertical that does.
    overrides = {
        "leads": [
            {"key": "name", "label": "Customer Name", "type": "text", "required": True},
            {"key": "phone", "label": "Phone", "type": "phone", "required": True},
            {"key": "loyalty_tier", "label": "Loyalty Tier", "type": "text", "required": True},
            {"key": "status", "label": "Status", "type": "select", "required": True},
        ]
    }
    index = resolve_index("ecommerce", overrides)
    create_schema = index["leads_create"].input_schema
    assert "loyalty_tier" in create_schema["properties"]
    assert "loyalty_tier" in create_schema["required"]


def test_set_status_uses_business_specific_enum():
    appt = resolve_index("clinics")["appointments_set_status"]
    enum = appt.input_schema["properties"]["status"]["enum"]
    # clinics.appointments vocab covers these
    for required_value in ("requested", "confirmed", "completed", "cancelled", "no_show"):
        assert required_value in enum, f"appointment vocab missing {required_value}"


def test_set_status_not_emitted_when_no_vocab():
    # The `other` business_type has leads/tickets — both have vocab. So set_status exists.
    keys = {t.key for t in resolve_catalog("other")}
    assert "leads_set_status" in keys
    assert "tickets_set_status" in keys


# ─────────── UI grouping ───────────


def test_catalog_as_groups_orders_tabs_then_general():
    groups = catalog_as_groups("clinics")
    labels = [g["label"] for g in groups]
    # Tab groups appear before General, in business-template declared order.
    # Clinics: Tickets + Appointments (no Leads — Customer base instead).
    assert set(labels[:2]) == {"Tickets", "Appointments"}
    assert labels[-1] == "General"


def test_default_tool_keys_covers_full_catalog():
    defaults = default_tool_keys("ecommerce")
    keys = {t.key for t in resolve_catalog("ecommerce")}
    assert set(defaults) == keys


# ─────────── Legacy aliases ───────────


def test_legacy_lead_tracker_keys_resolve_to_new_names():
    assert resolve_legacy_key("lead_tracker_create_lead") == "leads_create"
    assert resolve_legacy_key("lead_tracker_update_status") == "leads_set_status"
    assert resolve_legacy_key("lead_tracker_add_note") == "leads_add_note"
    assert resolve_legacy_key("create_ticket") == "tickets_create"
    assert resolve_legacy_key("call_logger_get_history") == "call_log_search"


def test_legacy_alias_table_does_not_drift():
    # Every alias must point at a key that exists for SOME business_type or in cross-cutting.
    cross_keys = {t.key for t in CROSS_CUTTING_CATALOG}
    union_keys = set(cross_keys)
    for bt in ("real_estate", "clinics", "ecommerce", "hospitality", "other"):
        union_keys |= {t.key for t in resolve_catalog(bt)}
    for legacy, new in LEGACY_KEY_ALIASES.items():
        assert new in union_keys, f"alias {legacy} → {new} points at non-existent tool"


# ─────────── Validation ───────────


def test_validate_rejects_unknown_keys():
    with pytest.raises(ValueError):
        validate_resolved_tool_keys(["leads_create", "nope_create"], "real_estate")


def test_validate_accepts_legacy_resolved_keys():
    # Should accept tools that are present in the per-business catalog.
    out = validate_resolved_tool_keys(["leads_create", "tickets_create"], "real_estate")
    assert "leads_create" in out


def test_validate_cross_cutting_keys_resolves_legacy():
    out = validate_cross_cutting_keys(["call_logger_get_history", "schedule_callback"])
    assert "call_log_search" in out
    assert "schedule_callback" in out


def test_validate_cross_cutting_keys_rejects_unknown():
    with pytest.raises(ValueError):
        validate_cross_cutting_keys(["nope_tool"])


# ─────────── Tool metadata stamping ───────────


def test_tab_tools_carry_business_type_metadata():
    index = resolve_index("clinics")
    tool = index["appointments_set_status"]
    assert tool.metadata.get("business_type") == "clinics"
    assert tool.metadata.get("tab") == "appointments"


def test_cross_cutting_tools_have_no_business_metadata():
    index = resolve_index("clinics")
    # find_contact and friends are cross-cutting — no business_type stamping.
    find_contact = index["find_contact"]
    assert find_contact.metadata == {} or "business_type" not in find_contact.metadata
