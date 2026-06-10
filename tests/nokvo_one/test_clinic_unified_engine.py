"""S1–S3 of the booking-engine unification: clinics on the generic engine.

S1 — the clinic_appointment flow builds in the catalog.
S2 — a clinic appointment drives to completion via evaluate_tool_flow_policy
     with correct book_appointment_with_lead_capture args.
S3 — the per-tenant flag + shadow-compare (default off, never mutates state).
"""
from __future__ import annotations

import copy

from app.services.tool_flow_questions import build_tool_flow_questions
from app.services.tool_flow_policy import evaluate_tool_flow_policy, _start_flow_key, _flow_action
from app.services.pipeline.booking_shadow import unified_booking_engine_enabled, run_shadow_compare


# ── S1 ──────────────────────────────────────────────────────────────────────


def test_clinic_appointment_flow_builds():
    cat = build_tool_flow_questions("clinics", None, [])
    flow = (cat.get("flows") or {}).get("clinic_appointment")
    assert flow is not None
    assert flow["tool_key"] == "book_appointment_with_lead_capture"
    assert flow["tab"] == "appointments"
    keys = [s["key"] for s in flow["slots"]]
    # Strictly field-driven: slots == the admin's configured Appointment Fields
    # (in schema order), minus categorical/assignment fields (doctor, department,
    # status — the doctor is chosen by the assignment engine). No forced scaffolding.
    assert keys == ["patient_name", "phone", "appointment_time", "reason"]
    assert "doctor" not in keys and "department" not in keys and "status" not in keys
    # real-estate flow is untouched.
    assert "real_estate_site_visit" in (build_tool_flow_questions("real_estate", None, [])["flows"])


# ── S2 ──────────────────────────────────────────────────────────────────────


def test_clinic_appointment_intent_starts_flow():
    assert _start_flow_key("I want to book an appointment", "clinics", []) == "clinic_appointment"
    assert _start_flow_key("I need to see a doctor", "clinics", []) == "clinic_appointment"
    # A clinic price question is an enquiry, not an appointment.
    assert _start_flow_key("what is the consultation fee", "clinics", []) != "clinic_appointment"


def test_clinic_booking_completes_via_unified_engine():
    state: dict = {}
    history: list = []
    turns = ["I want to book an appointment", "Nihar Reddy", "yes", "9876543210", "yes",
             "tomorrow at 11 am", "toothache"]
    for u in turns:
        res = evaluate_tool_flow_policy(
            u, business_type="clinics", schema_overrides=None, custom_tabs=None,
            provider_status={}, history=history, state=state, language="en",
        )
        if res is not None:
            for k, v in (res.get("state_patch") or {}).items():
                state[k] = v
        history += [{"role": "user", "content": u}, {"role": "assistant", "content": (res or {}).get("answer") or "..."}]

    tf = state.get("tool_flow", {})
    assert tf.get("completed") is True
    action = _flow_action(tf)
    assert action["tool_key"] == "book_appointment_with_lead_capture"
    args = action["arguments"]
    assert args["patient_name"] == "Nihar Reddy"
    assert args["phone"] == "9876543210"
    assert args["reason"] == "toothache"
    # appointment_time was combined + converted to an ISO timestamp.
    # "tomorrow" is date-relative — compute it instead of hardcoding (the IST
    # 11am slot lands on tomorrow's date in UTC since 05:30Z is still that day).
    import datetime as _dt
    _tz = _dt.timezone(_dt.timedelta(hours=5, minutes=30))
    tomorrow = (_dt.datetime.now(_tz).date() + _dt.timedelta(days=1)).isoformat()
    assert args["appointment_time"].startswith(f"{tomorrow}T") and args["appointment_time"].endswith("+00:00")


# ── S3 ──────────────────────────────────────────────────────────────────────


def test_unified_flag_default_off():
    assert unified_booking_engine_enabled({}) is False
    assert unified_booking_engine_enabled({"unified_booking_engine": True}) is True


def test_shadow_compare_never_mutates_state_or_raises():
    state = {"tool_flow": {"active": False}}
    snapshot = copy.deepcopy(state)
    # Should not raise and must not mutate the live state.
    run_shadow_compare(
        user_text="I want to book an appointment",
        history=[],
        state=state,
        language="en",
        business_type="clinics",
        schema_overrides=None,
        custom_tabs=None,
        provider_status={},
        bespoke_turn_policy=None,
        call_id="test-call",
    )
    assert state == snapshot
