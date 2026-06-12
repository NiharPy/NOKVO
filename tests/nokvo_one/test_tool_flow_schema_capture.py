"""Tests for schema-driven capture fixes.

Covers:
  A1 — a combined "Date and Time" (datetime) field is mapped to a single
       ``datetime`` slot (no phantom ``visit_time``), kind detection orders
       datetime BEFORE date, and ``_extract_value`` only fills the slot when
       both a date and a time are present.
  A2 — an active ``leads_create`` flow derives the ``lead_capture`` mode (so
       the agent drives the configured Lead Fields instead of falling through
       to the QUERY "answer questions" block), while ``site_visit`` is
       unchanged.
"""
from __future__ import annotations

from app.services.tool_flow_questions import (
    _kind_for_field,
    _question_for_kind,
    _real_estate_visit_slots,
    build_tool_flow_questions,
)
from app.services.nokvo_one_business_templates import field_catalog_for
from app.services.tool_flow_policy import _extract_value, evaluate_tool_flow_policy


def test_caller_phone_auto_fills_phone_slot_from_ani():
    # The caller's number (from call signaling) pre-fills the phone slot so the
    # agent never asks the caller to recite digits (telephony STT mangles them).
    res = evaluate_tool_flow_policy(
        "I want to schedule a site visit",
        business_type="real_estate",
        state={"caller_phone": "+917569672503"},
    )
    assert res is not None
    tf = (res.get("state_patch") or {}).get("tool_flow") or {}
    assert tf.get("collected", {}).get("phone") == "+917569672503"
    assert tf.get("phone_from_ani") is True
    # The next slot it asks for is NOT the phone (it's already filled).
    assert res.get("state_slot") != "phone"


def test_name_confirmation_loop_is_capped():
    # Telephony STT garbles names and re-confirming each fresh guess used to loop
    # forever ("Nihar"→"Nikhil"→"Nh"→"Lord"). After 2 prompts it must accept the
    # best-effort name and advance instead of confirming a 3rd time.
    bt = "real_estate"
    st = {"caller_phone": "+917569672503"}
    r = evaluate_tool_flow_policy("I want to schedule a site visit", business_type=bt, state=st)
    st["tool_flow"] = (r.get("state_patch") or {}).get("tool_flow") or {}
    # Answer the name slot → confirmation begins.
    r = evaluate_tool_flow_policy("Nihar", business_type=bt, state=st)
    st["tool_flow"] = (r.get("state_patch") or {}).get("tool_flow") or {}
    assert str(r.get("state_slot") or "").endswith("_confirm")
    # 1st non-confirmation (caller repeats) → one more confirm (still capped).
    r = evaluate_tool_flow_policy("Nikhil", business_type=bt, state=st)
    st["tool_flow"] = (r.get("state_patch") or {}).get("tool_flow") or {}
    assert str(r.get("state_slot") or "").endswith("_confirm")
    # 2nd non-confirmation → cap reached → accept best-effort + advance (NOT a confirm).
    r = evaluate_tool_flow_policy("Nihar", business_type=bt, state=st)
    tf = (r.get("state_patch") or {}).get("tool_flow") or {}
    assert not str(r.get("state_slot") or "").endswith("_confirm"), f"name confirm looped: {r.get('state_slot')}"
    assert tf.get("collected", {}).get("name")  # a best-effort name was kept
    assert not tf.get("awaiting_name_confirmation")


def test_name_confirmation_accepts_clean_yes():
    bt = "real_estate"
    st = {"caller_phone": "+917569672503"}
    r = evaluate_tool_flow_policy("I want to schedule a site visit", business_type=bt, state=st)
    st["tool_flow"] = (r.get("state_patch") or {}).get("tool_flow") or {}
    r = evaluate_tool_flow_policy("Nihar", business_type=bt, state=st)
    st["tool_flow"] = (r.get("state_patch") or {}).get("tool_flow") or {}
    r = evaluate_tool_flow_policy("yes", business_type=bt, state=st)
    tf = (r.get("state_patch") or {}).get("tool_flow") or {}
    assert not tf.get("awaiting_name_confirmation")
    assert tf.get("collected", {}).get("name")


def test_no_caller_phone_still_asks_for_phone():
    # Mic tester / no ANI → phone slot stays empty and is collected normally.
    res = evaluate_tool_flow_policy(
        "I want to schedule a site visit",
        business_type="real_estate",
        state={},
    )
    assert res is not None
    tf = (res.get("state_patch") or {}).get("tool_flow") or {}
    assert not tf.get("collected", {}).get("phone")
    assert not tf.get("phone_from_ani")
from app.services.real_estate_agent_fsm import (
    AGENT_MODE_LEAD_CAPTURE,
    AGENT_MODE_QUERY,
    AGENT_MODE_SITE_VISIT,
    current_mode,
    mode_block_for_prompt,
)


# ── A1: field-kind ordering ─────────────────────────────────────────────────


def test_combined_datetime_field_maps_to_datetime_kind():
    # type=datetime OR a label naming both date and time → "datetime", and the
    # datetime branch must win over the date-only branch.
    assert _kind_for_field({"key": "custom_4", "label": "Date and Time", "type": "datetime"}) == "datetime"
    assert _kind_for_field({"key": "x", "label": "Preferred Date and Time", "type": "text"}) == "datetime"
    # Plain date / time fields still resolve to their own kinds.
    assert _kind_for_field({"key": "d", "label": "Visit Date", "type": "text"}) == "date"
    assert _kind_for_field({"key": "t", "label": "Preferred Time", "type": "text"}) == "time"


def test_site_visit_slots_emit_one_datetime_slot_no_phantom_time():
    fields = [
        {"key": "custom_1", "label": "Name", "type": "text", "required": True},
        {"key": "custom_2", "label": "Phone", "type": "phone", "required": True},
        {"key": "custom_3", "label": "Project", "type": "text", "required": True},
        {"key": "custom_4", "label": "Date and Time", "type": "datetime", "required": True},
    ]
    slots = _real_estate_visit_slots(fields)
    keys = [s["key"] for s in slots]
    kinds = {s["key"]: s["kind"] for s in slots}
    # The combined field is one slot bound to its configured key...
    assert "custom_4" in keys
    assert kinds["custom_4"] == "datetime"
    # ...and NO phantom canonical visit_time/visit_date slot was appended.
    assert "visit_time" not in keys
    assert "visit_date" not in keys
    # Each slot stores back under the admin's configured key.
    assert all(s.get("source_field") == s["key"] for s in slots)


def test_field_catalog_offers_real_estate_palette():
    tickets = {f["key"] for f in field_catalog_for("real_estate", "tickets")}
    leads = {f["key"] for f in field_catalog_for("real_estate", "leads")}
    assert {"name", "phone", "project_name", "visit_date", "visit_time", "budget"} <= tickets
    # Leads are intentionally NOT configurable — fixed to name + phone (the rest of
    # the conversation lives in the post-call call notes). The catalog falls back
    # to the trimmed config schema, so the leads palette is exactly {name, phone}.
    assert leads == {"name", "phone"}
    # Unknown vertical/tab falls back to the default schema (never empty for a real tab).
    assert field_catalog_for("clinics", "appointments")


def test_new_real_estate_kinds_have_multilingual_questions():
    for key, expected_kind in (("possession_timeline", "possession_timeline"), ("purpose", "purpose"), ("financing", "financing")):
        assert _kind_for_field({"key": key, "label": key.replace("_", " ").title(), "type": "select"}) == expected_kind
        for lang in ("en", "hi", "te"):
            q = _question_for_kind(expected_kind, key.title(), lang)
            assert q and "{" not in q  # rendered, non-empty, no leftover format placeholder


def test_site_visit_slots_are_exactly_the_configured_fields():
    # Strictly field-driven: the agent asks ONLY what the admin configured. With
    # just {name, phone} there is NO auto-added date/time/project scaffolding.
    fields = [
        {"key": "name", "label": "Name", "type": "text", "required": True},
        {"key": "phone", "label": "Phone", "type": "phone", "required": True},
    ]
    keys = [s["key"] for s in _real_estate_visit_slots(fields)]
    assert keys == ["name", "phone"]
    assert "visit_date" not in keys and "visit_time" not in keys and "project_name" not in keys


def test_site_visit_slots_keep_custom_fields_drop_admin_only():
    # A custom {name, budget, phone} config → exactly those three, in order;
    # system/admin-only fields (status) are withheld for server-side defaults.
    fields = [
        {"key": "name", "label": "Name", "type": "text", "required": True},
        {"key": "budget", "label": "Budget", "type": "currency", "required": True},
        {"key": "phone", "label": "Phone", "type": "phone", "required": True},
        {"key": "status", "label": "Status", "type": "select", "required": True},
    ]
    slots = _real_estate_visit_slots(fields)
    assert [s["key"] for s in slots] == ["name", "budget", "phone"]
    assert {s["key"]: s["kind"] for s in slots} == {"name": "name", "budget": "budget", "phone": "phone"}


# ── A1: datetime extraction ─────────────────────────────────────────────────


def test_extract_value_datetime_requires_both_date_and_time():
    assert _extract_value("tomorrow at 11 am", "custom_4", "datetime")  # both → filled
    assert _extract_value("tomorrow", "custom_4", "datetime") is None    # date only → pending
    assert _extract_value("11 am", "custom_4", "datetime") is None       # time only → pending


# ── A2: lead_capture mode ───────────────────────────────────────────────────


def test_real_estate_enquiry_stays_query_never_lead_capture():
    # Real estate no longer interrogates for lead fields mid-call. Even a stale
    # leads_create flag resolves to QUERY (the lead is created end-of-call from
    # ANI + summary). The agent just answers questions and nudges to a visit.
    st = {"tool_flow": {"active": True, "flow_key": "leads_create"}}
    assert current_mode(st) == AGENT_MODE_QUERY


def test_site_visit_and_query_modes_unchanged():
    assert current_mode({"tool_flow": {"active": True, "flow_key": "real_estate_site_visit"}}) == AGENT_MODE_SITE_VISIT
    # Completed / inactive flows fall back to query.
    assert current_mode({"tool_flow": {"active": True, "flow_key": "leads_create", "completed": True}}) == AGENT_MODE_QUERY
    assert current_mode({"tool_flow": {"active": False}}) == AGENT_MODE_QUERY


def test_lead_capture_block_names_pending_slot():
    block = mode_block_for_prompt(
        AGENT_MODE_LEAD_CAPTURE,
        pending_slot_label="Phone",
        pending_slot_question="What phone number should our team use?",
    )
    assert "LEAD_CAPTURE" in block
    assert "Phone" in block
    # Required-asked / optional-offered discipline is stated.
    assert "ONE field" in block or "one field" in block.lower()


def test_leads_create_flow_present_in_catalog_for_real_estate():
    # With lead fields configured, the leads_create flow carries them as slots.
    overrides = {"leads": [
        {"key": "full_name", "label": "Full Name", "type": "text", "required": True},
        {"key": "budget", "label": "Budget", "type": "currency", "required": False},
    ]}
    cat = build_tool_flow_questions("real_estate", overrides, [])
    flow = (cat.get("flows") or {}).get("leads_create")
    assert flow is not None
    slot_keys = [s["key"] for s in flow["slots"]]
    assert "full_name" in slot_keys
    assert "budget" in slot_keys
