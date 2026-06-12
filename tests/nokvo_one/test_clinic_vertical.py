"""Tests for the Clinics vertical: tabs, service matching, clinic agent modes."""
from __future__ import annotations

from types import SimpleNamespace

from app.services.nokvo_one_business_templates import enabled_tabs_for, business_type_config
from app.services.clinic_service_service import find_service_match, services_prompt_section
from app.services.clinic_agent_fsm import (
    current_mode,
    detect_urgent_symptoms,
    mode_block_for_prompt,
    AGENT_MODE_APPOINTMENT_BOOKING,
    AGENT_MODE_QUERY,
    AGENT_MODE_TRIAGE_ESCALATION,
)


# ── Tabs: Appointments + Tickets, no Leads ──────────────────────────────────


def test_clinic_tabs_have_appointments_but_no_leads():
    # Clinics have no leads tab: every caller lands in the Customer base
    # instead, and follow-ups are admin-commanded from there.
    tabs = enabled_tabs_for("clinics")
    assert "appointments" in tabs
    assert "leads" not in tabs


def test_clinic_appointments_service_field_is_selectable():
    # "service" is intentionally NOT in the default appointments schema (the
    # flow resolves it from the free-text reason); it lives in the admin's
    # selectable field palette instead.
    from app.services.nokvo_one_business_templates import field_catalog_for

    palette_keys = [f["key"] for f in field_catalog_for("clinics", "appointments")]
    assert "service" in palette_keys


# ── Service matching (drives service-first routing) ─────────────────────────


def _svcs():
    return [
        SimpleNamespace(id="a", name="Dental Cleaning", department="Dentistry",
                        duration_minutes=30, price=None, price_display="₹800", description="Scaling"),
        SimpleNamespace(id="b", name="Cardiology Consultation", department="Cardiology",
                        duration_minutes=20, price=None, price_display=None, description=None),
    ]


def test_service_match_handles_filler_and_exact():
    svcs = _svcs()
    assert find_service_match(svcs, name="i want a cleaning").name == "Dental Cleaning"
    assert find_service_match(svcs, name="Dental Cleaning").name == "Dental Cleaning"
    assert find_service_match(svcs, name="cardiology").name == "Cardiology Consultation"
    assert find_service_match(svcs, name="just calling to say hi") is None
    assert find_service_match(svcs, service_id="b").name == "Cardiology Consultation"


def test_services_prompt_section_is_authoritative_and_lists_doctors():
    block = services_prompt_section([(_svcs()[0], ["Dr. Rao"]), (_svcs()[1], [])])
    assert "AUTHORITATIVE" in block
    assert "Dr. Rao" in block and "any available" in block


# ── Clinic agent modes (triage safety) ──────────────────────────────────────


def test_triage_takes_precedence_over_booking():
    # Even mid-booking, an urgent symptom flips to triage.
    st = {"appointment": {"active": True}}
    assert current_mode(st, latest_user_text="I have severe chest pain") == AGENT_MODE_TRIAGE_ESCALATION
    assert current_mode({}, latest_user_text="he's unconscious") == AGENT_MODE_TRIAGE_ESCALATION


def test_non_urgent_routes_to_booking_or_query():
    assert current_mode({"appointment": {"active": True}}, latest_user_text="book me in") == AGENT_MODE_APPOINTMENT_BOOKING
    assert current_mode({}, latest_user_text="what are your timings") == AGENT_MODE_QUERY
    assert not detect_urgent_symptoms("I have a mild cough and a runny nose")


def test_triage_block_forbids_booking_and_advises_emergency():
    block = mode_block_for_prompt(AGENT_MODE_TRIAGE_ESCALATION)
    low = block.lower()
    assert "emergency" in low
    assert "do not book" in low or "not book" in low
    # Booking block carries the no-diagnosis guardrail.
    assert "diagnose" in mode_block_for_prompt(AGENT_MODE_APPOINTMENT_BOOKING).lower()
