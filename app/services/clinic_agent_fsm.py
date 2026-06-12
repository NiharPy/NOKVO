"""Clinic inbound agent modes (Phase 1 — clinics only).

Derived, persona/guardrail blocks layered on top of the deterministic
appointment slot-fill FSM (``voice_turn_policy`` / the ``appointment_flow``
path). Like ``real_estate_agent_fsm``, this module does NOT drive slot-filling
— it tells the LLM "what situation are we in and how to behave" so the prompt
stays safe and on-task. The slot engine still owns the booking mechanics.

Modes:
  * ``query``               – default. Answer clinic/service/doctor questions
                              from the Services catalog + Business Facts.
  * ``appointment_booking`` – a booking is in progress; drive the next slot,
                              don't drift into chat.
  * ``service_inquiry``     – caller is asking about a service / price / which
                              doctor; describe from the catalog, then offer to
                              book.
  * ``triage_escalation``   – URGENT/EMERGENCY symptoms detected. Safety mode:
                              no diagnosis, no advice, no normal booking —
                              direct to emergency care / clinic staff.

Interested-but-not-booking callers need no mode: every clinic caller is
captured in the Customer base automatically (ANI + post-call notes), so the
agent just answers and offers to book — it never interrogates for details.

Only the FINAL mode is persisted (``agent_mode_final``) for telemetry; mid-call
modes are computed on demand via :func:`current_mode`.
"""
from __future__ import annotations

import re
from typing import Any


AGENT_MODE_QUERY = "query"
AGENT_MODE_APPOINTMENT_BOOKING = "appointment_booking"
AGENT_MODE_SERVICE_INQUIRY = "service_inquiry"
AGENT_MODE_TRIAGE_ESCALATION = "triage_escalation"

ALL_MODES = (
    AGENT_MODE_QUERY,
    AGENT_MODE_APPOINTMENT_BOOKING,
    AGENT_MODE_SERVICE_INQUIRY,
    AGENT_MODE_TRIAGE_ESCALATION,
)


def enabled_for_business_type(business_type: str | None) -> bool:
    """Phase 1 gate. Clinics only."""
    return str(business_type or "").strip().lower() == "clinics"


# Red-flag symptoms that must short-circuit to triage. Conservative on purpose:
# false positives (escalating a non-emergency) are far safer than missing a
# real emergency. Mirrors the spirit of assignment's _is_clinic_emergency.
_URGENT_RE = re.compile(
    r"\b(chest\s*pain|can'?t\s*breathe|cannot\s*breathe|difficulty\s*breathing|"
    r"short(?:ness)?\s*of\s*breath|breathless|"
    r"unconscious|fainted|faint(?:ing)?|seizure|convuls|"
    r"stroke|slurred\s*speech|face\s*droop|numb(?:ness)?\s*(?:on\s*)?one\s*side|"
    r"heart\s*attack|severe\s*bleeding|bleeding\s*heavily|won'?t\s*stop\s*bleeding|"
    r"suicid|overdose|poison|"
    r"severe\s*(?:pain|allerg)|anaphyla|"
    r"not\s*breathing|turning\s*blue|blue\s*lips|"
    r"emergency|ambulance|"
    r"high\s*fever\s*(?:and|with)\s*(?:fits|seizure)|"
    r"pregnan\w*\s*bleeding)\b",
    re.IGNORECASE,
)


def detect_urgent_symptoms(text: str | None) -> bool:
    return bool(text) and bool(_URGENT_RE.search(text))


def current_mode(
    state: dict[str, Any] | None,
    *,
    latest_user_text: str | None = None,
) -> str:
    """Compute the clinic agent mode for this turn.

    Triage takes absolute precedence (patient safety). Otherwise: an active
    appointment flow → booking; an explicit service question → service_inquiry;
    else query.
    """
    if detect_urgent_symptoms(latest_user_text):
        return AGENT_MODE_TRIAGE_ESCALATION
    appointment = ((state or {}).get("appointment") or {}) if isinstance(state, dict) else {}
    if appointment.get("active") and not appointment.get("completed"):
        return AGENT_MODE_APPOINTMENT_BOOKING
    return AGENT_MODE_QUERY


_DONT_DIAGNOSE = (
    "# MEDICAL SAFETY — non-negotiable\n"
    "- You are front-desk, NOT a clinician. NEVER diagnose, interpret symptoms or "
    "reports, suggest treatment, recommend or dose medication, or say whether "
    "something is serious. If asked, say the doctor will advise and offer to book.\n"
    "- Confirm patient name and phone by reading them back (digit by digit for the "
    "number) — a wrong number costs the clinic the appointment."
)


def mode_block_for_prompt(
    mode: str,
    *,
    pending_slot_label: str | None = None,
    pending_slot_question: str | None = None,
) -> str:
    """Render the LLM-facing block for the current clinic mode."""
    if mode == AGENT_MODE_TRIAGE_ESCALATION:
        return (
            "# AGENT MODE — TRIAGE / URGENT (override everything)\n"
            "The caller may be describing a medical EMERGENCY. Patient safety first:\n"
            "- Do NOT book a routine appointment, do NOT collect slots, do NOT chit-chat.\n"
            "- In ONE calm, clear breath: tell them if this is an emergency to call "
            "emergency services / go to the nearest emergency room immediately (in "
            "India, 112 or 108 for an ambulance).\n"
            "- Offer to connect them to clinic staff right now. Do NOT diagnose, "
            "reassure that it's 'probably fine', or advise any treatment.\n"
            "- Stay on this until they're safe or ask for something else."
        )
    if mode == AGENT_MODE_APPOINTMENT_BOOKING:
        next_step = ""
        if pending_slot_label:
            next_step = f"\nNext detail to capture: **{pending_slot_label}**."
            if pending_slot_question:
                next_step += f' Ask it naturally (reference: "{pending_slot_question}"). One ask per turn.'
        return (
            "# AGENT MODE — APPOINTMENT_BOOKING\n"
            "You're booking an appointment. Identify the SERVICE the patient needs, then "
            "collect the remaining details ONE at a time — don't drift into general chat. "
            "The system books the slot and assigns a doctor who provides that service; you "
            "don't pick the doctor or promise a specific one unless the caller asked for "
            "one who offers the service. Read back name + phone before confirming."
            + next_step
            + "\n\n" + _DONT_DIAGNOSE
        )
    if mode == AGENT_MODE_SERVICE_INQUIRY:
        return (
            "# AGENT MODE — SERVICE_INQUIRY\n"
            "The caller is asking about a service, its price, or which doctor provides it. "
            "Answer from the SERVICES list only (never invent a service, price, or doctor). "
            "After answering, warmly offer to book it.\n\n" + _DONT_DIAGNOSE
        )
    return (
        "# AGENT MODE — QUERY\n"
        "Answer the caller's clinic / service / doctor / timing question from the SERVICES "
        "list and BUSINESS FACTS. After answering, ask one warm question that moves toward "
        "booking an appointment.\n\n" + _DONT_DIAGNOSE
    )


__all__ = (
    "AGENT_MODE_QUERY",
    "AGENT_MODE_APPOINTMENT_BOOKING",
    "AGENT_MODE_SERVICE_INQUIRY",
    "AGENT_MODE_TRIAGE_ESCALATION",
    "ALL_MODES",
    "enabled_for_business_type",
    "detect_urgent_symptoms",
    "current_mode",
    "mode_block_for_prompt",
)
