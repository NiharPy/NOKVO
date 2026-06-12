"""Synthetic outbound context for clinic manual follow-up calls.

Clinic outbound calls are not campaign-driven: an admin clicks "Call now" /
"Schedule" on a Customer base row and types a purpose note. There is no
OutboundCampaign row to load, so this module builds the
:class:`OutboundCampaignContext` the voice pipeline expects from three
sources instead:

  * the admin's note (the call's single objective),
  * the customer's prior-call summary (rendered separately by the follow-up
    preamble via ``campaign_context.handoff_note``),
  * the clinic's active services catalog (the call's entire factual scope —
    the same hard boundary campaign ``doc_text`` provides for real-estate).
"""
from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.agent_outbound_context import OutboundCampaignContext
from app.services.clinic_agent_fsm import _DONT_DIAGNOSE

logger = logging.getLogger(__name__)

_DOC_TEXT_MAX_CHARS = 4000

_CLINIC_FOLLOWUP_PROMPT = (
    "You are the clinic's front-desk assistant making a follow-up call that the "
    "clinic admin personally requested. You called the customer — they didn't "
    "call you. The REASON FOR THIS CALL block is your agenda: deliver it warmly "
    "and early, reference the prior conversation notes when present, and answer "
    "questions only from the services catalog below. Offer to book an "
    "appointment when it fits naturally; never push. If they're busy or ask not "
    "to be called, apologise briefly and end politely.\n\n" + _DONT_DIAGNOSE
)

_CLINIC_EXIT_CONDITIONS = [
    "Customer asks not to be called again.",
    "Customer says they are not interested.",
    "Customer says this is the wrong number.",
    "Customer is busy and asks for a callback.",
    "The admin's purpose for the call has been covered.",
]


def _services_doc_text(services: list[Any]) -> str:
    """Render the active services catalog as the call's factual scope."""
    if not services:
        return "The clinic has not published a services catalog. Do not invent services or prices."
    lines = ["CLINIC SERVICES CATALOG (the only services you may discuss):"]
    for service in services[:60]:
        bits = [str(service.name or "").strip()]
        if getattr(service, "department", None):
            bits.append(f"({service.department})")
        if getattr(service, "duration_minutes", None):
            bits.append(f"~{service.duration_minutes} min")
        price = (getattr(service, "price_display", None) or "").strip()
        if not price and getattr(service, "price", None) is not None:
            price = str(service.price)
        if price:
            bits.append(f"price: {price}")
        lines.append("- " + " — ".join(b for b in bits if b))
        description = (getattr(service, "description", None) or "").strip()
        if description:
            lines.append(f"  notes: {description[:240]}")
    return "\n".join(lines)[:_DOC_TEXT_MAX_CHARS]


async def build_clinic_followup_context(
    db: AsyncSession | None,
    *,
    organization_id: uuid.UUID | str | None,
    admin_note: str | None,
    customer_name: str | None = None,
    company_name: str | None = None,
    caller_name: str | None = None,
) -> OutboundCampaignContext:
    """Build the synthetic proactive context for one clinic follow-up call."""
    note = str(admin_note or "").strip()[:500]
    objective_text = note or "Check in with the customer and see how the clinic can help."

    doc_text = "The clinic has not published a services catalog. Do not invent services or prices."
    try:
        from app.services.clinic_service_service import load_active_services

        services = await load_active_services(db, organization_id)
        doc_text = _services_doc_text(services)
    except Exception:
        logger.debug("CLINIC-OUTBOUND: services catalog load failed", exc_info=True)

    return OutboundCampaignContext(
        campaign_id=f"clinic-followup-{organization_id or 'unknown'}",
        name="Clinic follow-up",
        goal=objective_text,
        agent_prompt=_CLINIC_FOLLOWUP_PROMPT,
        objectives=[objective_text],
        exit_conditions=list(_CLINIC_EXIT_CONDITIONS),
        tone="warm, calm, and respectful",
        doc_text=doc_text,
        caller_name=str(caller_name or "").strip() or "Riya",
        company_name=str(company_name or "").strip() or "the clinic",
        pitch_summary="",
        objective="info_outreach",
    )


__all__ = ("build_clinic_followup_context",)
