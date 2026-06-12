"""Clinic OUTBOUND agent FSM (admin-commanded follow-up calls).

Mirrors :mod:`real_estate_outbound_agent_fsm` in shape but for the clinic
manual-followup path: every outbound clinic call exists because an admin
clicked "Call now" / "Schedule" on a Customer base row and typed a purpose
note. There are no campaigns, no objectives gating, no automated retries.

Modes:

  * ``followup``            — default. Deliver the admin's note (the reason
                              for the call), backed by the customer's prior
                              call summary and the services catalog.
  * ``appointment_booking`` — the customer agreed to book; the appointment
                              slot engine is active — drive the next slot.
  * ``triage_escalation``   — the customer described urgent symptoms. Stop
                              the call's agenda; direct to emergency care.

Like the inbound clinic FSM, this module does not own slot mechanics — it
renders persona/guardrail blocks for the prompt. Triage detection is shared
with :func:`clinic_agent_fsm.detect_urgent_symptoms`.
"""
from __future__ import annotations

from typing import Any

from app.services.clinic_agent_fsm import _DONT_DIAGNOSE, detect_urgent_symptoms


AGENT_MODE_FOLLOWUP = "followup"
AGENT_MODE_APPOINTMENT_BOOKING = "appointment_booking"
AGENT_MODE_TRIAGE_ESCALATION = "triage_escalation"

ALL_MODES = (
    AGENT_MODE_FOLLOWUP,
    AGENT_MODE_APPOINTMENT_BOOKING,
    AGENT_MODE_TRIAGE_ESCALATION,
)


def enabled_for_business_type(business_type: str | None) -> bool:
    """Clinic outbound FSM gates to the clinics vertical."""
    return str(business_type or "").strip().lower() == "clinics"


def current_mode(
    state: dict[str, Any] | None,
    *,
    latest_user_text: str | None = None,
) -> str:
    """Compute the clinic outbound mode for this turn.

    Triage takes absolute precedence (patient safety), then an active
    appointment flow, else the default follow-up posture.
    """
    if detect_urgent_symptoms(latest_user_text):
        return AGENT_MODE_TRIAGE_ESCALATION
    appointment = ((state or {}).get("appointment") or {}) if isinstance(state, dict) else {}
    if appointment.get("active") and not appointment.get("completed"):
        return AGENT_MODE_APPOINTMENT_BOOKING
    return AGENT_MODE_FOLLOWUP


def mode_block_for_prompt(
    mode: str,
    *,
    pending_slot_label: str | None = None,
    pending_slot_question: str | None = None,
) -> str:
    """Render the LLM-facing block for the current clinic outbound mode."""
    if mode == AGENT_MODE_TRIAGE_ESCALATION:
        return (
            "# AGENT MODE — TRIAGE / URGENT (override everything)\n"
            "The person you called may be describing a medical EMERGENCY. Patient "
            "safety first — the reason you called no longer matters:\n"
            "- STOP the follow-up agenda. Do NOT deliver the admin's message, do NOT "
            "book anything, do NOT collect details.\n"
            "- In ONE calm, clear breath: tell them if this is an emergency to call "
            "emergency services / go to the nearest emergency room immediately (in "
            "India, 112 or 108 for an ambulance).\n"
            "- Offer to have clinic staff call them right away. Do NOT diagnose, "
            "reassure that it's 'probably fine', or advise any treatment.\n"
            "- Stay on this until they're safe, then end the call."
        )
    if mode == AGENT_MODE_APPOINTMENT_BOOKING:
        next_step = ""
        if pending_slot_label:
            next_step = f"\nNext detail to capture: **{pending_slot_label}**."
            if pending_slot_question:
                next_step += f' Ask it naturally (reference: "{pending_slot_question}"). One ask per turn.'
        return (
            "# AGENT MODE — APPOINTMENT_BOOKING (outbound)\n"
            "The customer agreed to book. Identify the SERVICE they need, then collect "
            "the remaining details ONE at a time — don't drift back into the follow-up "
            "chat. The system books the slot and assigns a doctor who provides that "
            "service. Read back name + phone before confirming."
            + next_step
            + "\n\n" + _DONT_DIAGNOSE
        )
    return (
        "# AGENT MODE — FOLLOWUP (default)\n"
        "You're the clinic's front desk calling this customer back on the clinic "
        "admin's instruction. The REASON FOR THIS CALL block is your agenda — deliver "
        "it warmly in your first substantive turn, referencing the prior-call notes "
        "when present.\n"
        "- One purpose, one call: cover the admin's note, answer the customer's "
        "questions from the SERVICES list, and offer to book an appointment when it "
        "fits naturally.\n"
        "- If they're busy or ask not to be called, apologise briefly and close — the "
        "admin can reschedule.\n"
        "- Never invent a service, price, or doctor not in the SERVICES list.\n\n"
        + _DONT_DIAGNOSE
    )


__all__ = (
    "AGENT_MODE_FOLLOWUP",
    "AGENT_MODE_APPOINTMENT_BOOKING",
    "AGENT_MODE_TRIAGE_ESCALATION",
    "ALL_MODES",
    "enabled_for_business_type",
    "current_mode",
    "mode_block_for_prompt",
)
