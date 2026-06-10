"""Tests for Nokvo One macros + custom tabs.

Covers:
  - Macro gating per business_type.
  - Macro tools are present in the resolved catalog only for their business_type.
  - Custom tabs produce 8 CRUD tools with the right record_type.
  - Custom tab status vocabulary drives the set_status / close enums.
  - Macro handlers write the expected NokvoOneToolRecord rows.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from app.services.dynamic_tool_resolver import (
    catalog_as_groups,
    resolve_catalog,
    resolve_index,
)
from app.services.nokvo_one_business_templates import (
    custom_tab_record_type,
    custom_tabs_from_overrides,
    normalize_custom_tab_slug,
)
from app.services.predefined_tools_service import (
    MACRO_CATALOG,
    PredefinedToolsService,
    macros_for_business_type,
)


# ─────────── Macro gating ───────────


def test_macros_gated_to_specific_business_types():
    assert {t.key for t in macros_for_business_type("clinics")} == {
        "book_appointment_with_lead_capture"
    }
    assert {t.key for t in macros_for_business_type("real_estate")} == {
        "qualify_lead_and_schedule_visit",
        "project_search",
        "request_brochure",
    }
    assert {t.key for t in macros_for_business_type("ecommerce")} == {
        "open_return_ticket_with_order_lookup"
    }
    assert {t.key for t in macros_for_business_type("hospitality")} == {
        "create_reservation_lead_and_hold"
    }
    # `other` and unknown types get no macros.
    assert macros_for_business_type("other") == ()
    assert macros_for_business_type(None) == ()
    assert macros_for_business_type("nope") == ()


def test_macros_appear_in_resolver_under_macros_group():
    groups = {g["label"]: g for g in catalog_as_groups("clinics")}
    assert "Macros" in groups
    keys = {t["key"] for t in groups["Macros"]["tools"]}
    assert keys == {"book_appointment_with_lead_capture"}


def test_macros_do_not_leak_to_other_business_types():
    # ecommerce should not contain the clinics macro.
    ecom_index = resolve_index("ecommerce")
    assert "book_appointment_with_lead_capture" not in ecom_index
    # clinics index should not contain the ecommerce macro.
    clinic_index = resolve_index("clinics")
    assert "open_return_ticket_with_order_lookup" not in clinic_index


def test_macro_catalog_input_schemas_are_well_formed():
    for tools in MACRO_CATALOG.values():
        for tool in tools:
            schema = tool.input_schema
            assert schema["type"] == "object"
            # Required fields exist as properties.
            for req in schema.get("required", []):
                assert req in schema["properties"], f"{tool.key}: required '{req}' missing from properties"
            assert tool.handler_name.startswith("macro_")


# ─────────── Custom tabs ───────────


def test_custom_tab_slug_normalization():
    assert normalize_custom_tab_slug("Deliveries") == "deliveries"
    assert normalize_custom_tab_slug("Express-Deliveries") == "express_deliveries"
    with pytest.raises(ValueError):
        normalize_custom_tab_slug("leads")  # reserved
    with pytest.raises(ValueError):
        normalize_custom_tab_slug("123abc")  # must start with letter
    with pytest.raises(ValueError):
        normalize_custom_tab_slug("")


def test_custom_tab_record_type_is_prefixed():
    assert custom_tab_record_type("deliveries") == "custom_deliveries"
    assert custom_tab_record_type("Properties") == "custom_properties"


def test_custom_tabs_from_overrides_skips_invalid_entries():
    provider_status = {
        "business_template_custom_tabs": [
            {"slug": "deliveries", "label": "Deliveries"},  # valid
            {"slug": "leads", "label": "Bad"},  # reserved
            {"slug": "deliveries", "label": "Dup"},  # duplicate after first wins
            {"slug": "", "label": "Empty"},  # invalid slug
            "not-a-dict",  # ignored
        ]
    }
    result = custom_tabs_from_overrides(provider_status)
    assert [t["slug"] for t in result] == ["deliveries"]
    # Defaults applied when status_vocabulary missing.
    assert result[0]["status_vocabulary"]["initial"] == "open"


def test_custom_tab_produces_eight_crud_tools():
    custom_tabs = [
        {
            "slug": "deliveries",
            "label": "Deliveries",
            "fields": [
                {"key": "tracking_id", "label": "Tracking", "type": "text", "required": True},
                {"key": "phone", "label": "Phone", "type": "phone", "required": True},
            ],
            "status_vocabulary": {
                "initial": "pending",
                "all": ["pending", "in_transit", "delivered"],
                "forward": ["pending", "in_transit", "delivered"],
            },
            "search_keys": ["tracking_id"],
        }
    ]
    index = resolve_index("clinics", None, custom_tabs)
    expected_keys = {
        "deliveries_create",
        "deliveries_update",
        "deliveries_get",
        "deliveries_list",
        "deliveries_search",
        "deliveries_add_note",
        "deliveries_set_status",
        "deliveries_close",
    }
    assert expected_keys.issubset(set(index.keys()))


def test_custom_tab_set_status_enum_matches_vocabulary():
    custom_tabs = [
        {
            "slug": "deliveries",
            "label": "Deliveries",
            "fields": [],
            "status_vocabulary": {
                "initial": "pending",
                "all": ["pending", "in_transit", "delivered"],
                "forward": ["pending", "in_transit", "delivered"],
            },
        }
    ]
    tool = resolve_index("clinics", None, custom_tabs)["deliveries_set_status"]
    enum = tool.input_schema["properties"]["status"]["enum"]
    assert enum == ["pending", "in_transit", "delivered"]


def test_custom_tab_record_type_in_resolved_tools():
    custom_tabs = [{"slug": "deliveries", "label": "Deliveries", "fields": []}]
    index = resolve_index("other", None, custom_tabs)
    assert index["deliveries_create"].record_type == "custom_deliveries"
    assert index["deliveries_set_status"].record_type == "custom_deliveries"


def test_custom_tab_emits_own_group():
    custom_tabs = [{"slug": "deliveries", "label": "Deliveries", "fields": []}]
    groups = catalog_as_groups("clinics", None, custom_tabs)
    labels = [g["label"] for g in groups]
    assert "Deliveries" in labels
    # Ordering: built-in tab groups first, then custom groups, then Macros, then General.
    deliveries_idx = labels.index("Deliveries")
    assert deliveries_idx > labels.index("Appointments")  # after the built-in clinic tab
    assert deliveries_idx < labels.index("Macros")
    assert deliveries_idx < labels.index("General")


# ─────────── Macro handler dispatch (DB-mocked) ───────────


class _FakeDB:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    async def flush(self):
        pass

    async def commit(self):
        pass

    async def rollback(self):
        pass

    async def execute(self, _stmt):
        class _Result:
            def scalars(self):
                return self

            def first(self):
                return None

            def all(self):
                return []

        return _Result()


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_clinics_macro_writes_lead_and_appointment():
    db = _FakeDB()
    tool = MACRO_CATALOG["clinics"][0]
    args = {
        "patient_name": "Jane Doe",
        "phone": "+918888888888",
        "reason": "follow-up",
        "appointment_time": "2026-05-20T10:00:00Z",
        "preferred_doctor": "Dr. Smith",
        "department": "General",
        "email": "jane@example.com",
    }
    result = _run(
        PredefinedToolsService.execute(
            db, uuid.uuid4(), uuid.uuid4(), tool, args
        )
    )
    assert result["ok"] is True
    assert "lead_id" in result and "appointment_id" in result
    record_types = sorted(
        getattr(o, "record_type", None) for o in db.added if getattr(o, "record_type", None)
    )
    assert "lead" in record_types and "appointment" in record_types


def test_real_estate_macro_writes_site_visit_ticket_and_callback():
    """Booking a site visit writes a TICKET (Site Visits tab) keyed by the
    Site Visit Fields, plus a scheduling callback — not a lead."""
    db = _FakeDB()
    tool = MACRO_CATALOG["real_estate"][0]
    args = {
        "name": "John Buyer",
        "phone": "+919999999999",
        "visit_at": "2026-05-25T14:00:00Z",
        "project_name": "Skyline Heights",
        "record_data": {
            "name": "John Buyer",
            "phone": "+919999999999",
            "project_name": "Skyline Heights",
            "visit_date": "2026-05-25",
            "visit_time": "2:00 PM",
        },
    }
    result = _run(
        PredefinedToolsService.execute(
            db, uuid.uuid4(), uuid.uuid4(), tool, args
        )
    )
    assert result["ok"] is True
    assert result["record_type"] == "ticket"
    assert result["ticket_id"]
    record_types = {getattr(o, "record_type", None) for o in db.added}
    assert {"ticket", "callback"}.issubset(record_types)
    assert "lead" not in record_types
    ticket = next(o for o in db.added if getattr(o, "record_type", None) == "ticket")
    # Data is keyed by the configured Site Visit Fields.
    assert ticket.data["name"] == "John Buyer"
    assert ticket.data["project_name"] == "Skyline Heights"
    assert ticket.data["visit_date"] == "2026-05-25"
    assert ticket.data["issue_type"] == "site_visit"


def test_ecommerce_macro_writes_ticket_and_call_log():
    db = _FakeDB()
    tool = MACRO_CATALOG["ecommerce"][0]
    args = {
        "customer_name": "Buyer X",
        "issue_summary": "Wrong size delivered",
        "order_id": "ORD-1234",
        "phone": "+917777777777",
    }
    result = _run(
        PredefinedToolsService.execute(
            db, uuid.uuid4(), uuid.uuid4(), tool, args
        )
    )
    assert result["ok"] is True
    assert result["ticket_status"] == "open"
    record_types = {getattr(o, "record_type", None) for o in db.added}
    assert {"ticket", "call_log"}.issubset(record_types)


def test_hospitality_macro_writes_lead_and_hold_callback():
    db = _FakeDB()
    tool = MACRO_CATALOG["hospitality"][0]
    args = {
        "guest_name": "Booking Guest",
        "phone": "+916666666666",
        "stay_dates": "2026-06-01 to 2026-06-05",
        "confirm_at": "2026-05-19T12:00:00Z",
        "party_size": 2,
    }
    result = _run(
        PredefinedToolsService.execute(
            db, uuid.uuid4(), uuid.uuid4(), tool, args
        )
    )
    assert result["ok"] is True
    assert "lead_id" in result and "callback_id" in result
    record_types = {getattr(o, "record_type", None) for o in db.added}
    assert {"lead", "callback"}.issubset(record_types)
