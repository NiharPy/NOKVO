"""
Outcome-driven starter packs for Nokvo One onboarding v2.

An owner picks 1–5 plain-English outcomes during signup ("Book appointments",
"Answer questions from our docs"). Each outcome maps to:

  - a slice of the dynamic tool catalog (tool_keys),
  - a snippet of system prompt copy,
  - optional starter knowledge-base seed text,
  - whether the outcome implies a calling-enabled / outbound feature
    (so we know whether the agent it produces is fully usable on day one).

The materialized agent reuses the existing NokvoOneAgent table. No new storage.

This module is the single source of truth for what outcomes exist per
business_type and how they translate into agent configuration. Adding a new
outcome is a data change here, not a code change anywhere else.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any


# ─────────── Outcome catalog ───────────


# Each entry: (slug, label, description, tool_keys, prompt_fragment, defaults_on)
# Tool keys are *resolver-produced* names (see dynamic_tool_resolver). When the
# resolver doesn't emit a key for a given business_type (e.g. appointments_*
# for ecommerce), the key is silently dropped at materialization time.
OUTCOME_PACKS: dict[str, list[dict[str, Any]]] = {
    "clinics": [
        {
            "slug": "book_appointments",
            "label": "Book and confirm appointments",
            "description": "Captures patient details, books the slot, and confirms back.",
            "tool_keys": [
                "appointments_create",
                "appointments_set_status",
                "appointments_search",
                "book_appointment_with_lead_capture",
            ],
            "prompt_fragment": (
                "Help patients book and confirm clinic appointments. Capture patient name, phone, "
                "reason, and preferred time. Use the appointment tools to create the booking. "
                "Never quote medical advice — escalate uncertain symptoms to clinic staff."
            ),
            "default_on": True,
        },
        {
            "slug": "answer_from_docs",
            "label": "Answer questions from our docs",
            "description": "Reads your uploaded clinic info to answer common questions about timings, doctors, services.",
            "tool_keys": [],  # Pure RAG; uses no tools, just the knowledge base.
            "prompt_fragment": (
                "Answer questions about clinic hours, services, doctors, and pricing using only the approved "
                "knowledge base content. If a fact is not in the knowledge base, say you'll connect them with staff."
            ),
            "default_on": True,
        },
        {
            "slug": "take_messages",
            "label": "Take messages and follow up",
            "description": "Logs callbacks and escalations for the team to follow up.",
            "tool_keys": [
                "schedule_callback",
                "escalate_to_human",
                "call_log_create",
            ],
            "prompt_fragment": (
                "When the patient's request can't be resolved on the call, schedule a callback or escalate "
                "to clinic staff so they can follow up. Always log the call summary."
            ),
            "default_on": True,
        },
        {
            "slug": "send_reminders",
            "label": "Send appointment reminders",
            "description": "Drafts reminder emails for upcoming appointments (human reviews and sends).",
            "tool_keys": ["send_email_draft", "appointments_list"],
            "prompt_fragment": (
                "For upcoming appointments, draft a reminder email. Drafts are queued for staff review — "
                "never claim a reminder has been sent."
            ),
            "default_on": False,
        },
    ],
    "real_estate": [
        {
            "slug": "qualify_inquiries",
            "label": "Qualify property inquiries",
            "description": "Captures budget, location, and need, then routes to an agent.",
            "tool_keys": [
                "leads_create",
                "leads_set_status",
                "leads_add_note",
                "qualify_lead_and_schedule_visit",
                "escalate_to_human",
            ],
            "prompt_fragment": (
                "Qualify property inquiries by capturing budget, preferred location, property type, and "
                "timeline. Create a lead and mark qualified ones for agent follow-up."
            ),
            "default_on": True,
        },
        {
            "slug": "schedule_site_visits",
            "label": "Schedule site visits",
            "description": "Books site visits and confirms back with the buyer.",
            "tool_keys": [
                "schedule_callback",
                "leads_update",
                "qualify_lead_and_schedule_visit",
                "call_log_create",
            ],
            "prompt_fragment": (
                "When a qualified buyer wants a site visit, schedule a callback to confirm the time and "
                "location. Update the lead with visit details."
            ),
            "default_on": True,
        },
        {
            "slug": "answer_property_questions",
            "label": "Answer property questions",
            "description": "Reads your uploaded brochures and listings to answer questions about properties.",
            "tool_keys": [],
            "prompt_fragment": (
                "Answer questions about listed properties, amenities, pricing, and locality using only the "
                "approved brochure / listing content."
            ),
            "default_on": True,
        },
        {
            "slug": "log_issues",
            "label": "Log documentation or property issues",
            "description": "Creates a ticket when a buyer raises a problem.",
            "tool_keys": ["tickets_create", "tickets_add_note", "escalate_to_human", "call_log_create"],
            "prompt_fragment": (
                "Create a support ticket when the buyer raises a documentation, payment, or property issue. "
                "Escalate urgent items to a human agent."
            ),
            "default_on": False,
        },
    ],
    "ecommerce": [
        {
            "slug": "order_status",
            "label": "Answer order-status questions",
            "description": "Looks up order context and explains status to the customer.",
            "tool_keys": [
                "find_contact",
                "call_log_create",
            ],
            "prompt_fragment": (
                "When a customer asks about an order, use find_contact to retrieve prior interactions and "
                "answer based only on the knowledge base. If the order is not in the system, escalate to a human."
            ),
            "default_on": True,
        },
        {
            "slug": "returns_and_refunds",
            "label": "Open returns and refund tickets",
            "description": "Files return-request tickets with order context. Refunds always require human approval.",
            "tool_keys": [
                "tickets_create",
                "tickets_add_note",
                "open_return_ticket_with_order_lookup",
                "escalate_to_human",
            ],
            "prompt_fragment": (
                "Open a return-request ticket with the order_id and the issue summary. Never promise a refund — "
                "policy decisions require human review."
            ),
            "default_on": True,
        },
        {
            "slug": "product_questions",
            "label": "Answer product questions",
            "description": "Reads your uploaded product info to answer questions before purchase.",
            "tool_keys": [],
            "prompt_fragment": (
                "Answer questions about product specs, availability, and pricing using only the approved "
                "product knowledge base."
            ),
            "default_on": True,
        },
        {
            "slug": "capture_leads",
            "label": "Capture leads from chat",
            "description": "Saves contact details when a visitor shows interest.",
            "tool_keys": ["leads_create", "leads_set_status", "leads_add_note"],
            "prompt_fragment": (
                "When a visitor expresses interest but isn't ready to buy, capture their name and contact "
                "details as a lead with the products they asked about."
            ),
            "default_on": False,
        },
    ],
    "hospitality": [
        {
            "slug": "booking_inquiries",
            "label": "Handle booking inquiries",
            "description": "Captures guest name, dates, room preferences, and creates a lead.",
            "tool_keys": [
                "leads_create",
                "leads_add_note",
                "create_reservation_lead_and_hold",
                "schedule_callback",
            ],
            "prompt_fragment": (
                "Capture booking inquiries with guest name, phone, stay dates, room type, and party size. "
                "Schedule a callback to confirm the hold."
            ),
            "default_on": True,
        },
        {
            "slug": "answer_property_info",
            "label": "Answer property and amenity questions",
            "description": "Reads your uploaded brochure to answer common guest questions.",
            "tool_keys": [],
            "prompt_fragment": (
                "Answer questions about amenities, check-in/out, dining, and policies using only the approved "
                "property knowledge base."
            ),
            "default_on": True,
        },
        {
            "slug": "log_complaints",
            "label": "Log guest complaints",
            "description": "Creates service-recovery tickets when a guest reports an issue.",
            "tool_keys": ["tickets_create", "tickets_add_note", "escalate_to_human", "call_log_create"],
            "prompt_fragment": (
                "When a guest reports a problem, create a service-recovery ticket and escalate urgent items to staff."
            ),
            "default_on": True,
        },
    ],
    "other": [
        {
            "slug": "capture_leads",
            "label": "Capture leads",
            "description": "Saves contact details and the customer's need.",
            "tool_keys": ["leads_create", "leads_set_status", "leads_add_note"],
            "prompt_fragment": (
                "Capture leads with name, contact details, and what the customer needs. Mark qualified leads "
                "for human follow-up."
            ),
            "default_on": True,
        },
        {
            "slug": "answer_from_docs",
            "label": "Answer questions from our docs",
            "description": "Reads your uploaded materials to answer common questions.",
            "tool_keys": [],
            "prompt_fragment": (
                "Answer questions using only the approved knowledge base. If the answer isn't in the materials, "
                "say so and offer to connect with a human."
            ),
            "default_on": True,
        },
        {
            "slug": "log_support",
            "label": "Log support requests",
            "description": "Creates tickets and schedules callbacks for follow-up.",
            "tool_keys": ["tickets_create", "tickets_add_note", "schedule_callback", "call_log_create"],
            "prompt_fragment": (
                "Create a support ticket or schedule a callback when the customer needs follow-up."
            ),
            "default_on": True,
        },
    ],
}


_AGENT_NAME_DEFAULTS: dict[str, str] = {
    "clinics": "Clinic Assistant",
    "real_estate": "Property Assistant",
    "ecommerce": "Store Assistant",
    "hospitality": "Front-Desk Assistant",
    "other": "Customer Assistant",
}


def outcomes_for(business_type: str | None) -> list[dict[str, Any]]:
    """Return the public-facing outcomes list for a business_type.

    Strips internal fields (tool_keys, prompt_fragment) — those are server-side
    only. The owner sees label + description + default_on.
    """
    raw = OUTCOME_PACKS.get(business_type or "", []) or OUTCOME_PACKS["other"]
    return [
        {
            "slug": pack["slug"],
            "label": pack["label"],
            "description": pack["description"],
            "default_on": bool(pack.get("default_on", False)),
        }
        for pack in raw
    ]


def default_agent_name(business_type: str | None) -> str:
    return _AGENT_NAME_DEFAULTS.get(business_type or "", "Customer Assistant")


def materialize_starter_pack(
    business_type: str | None,
    selected_outcome_slugs: list[str],
    *,
    available_tool_keys: set[str],
) -> dict[str, Any]:
    """Translate (business_type, selected outcomes) into an agent config.

    `available_tool_keys` is the set of tool keys that the resolver produces
    for this org (so we drop outcome-suggested tools that don't exist — e.g.
    appointments_* for ecommerce). Returns:
        { name, description, system_prompt, tool_keys }
    """
    raw_outcomes = OUTCOME_PACKS.get(business_type or "", []) or OUTCOME_PACKS["other"]
    by_slug = {pack["slug"]: pack for pack in raw_outcomes}
    chosen = [by_slug[slug] for slug in selected_outcome_slugs if slug in by_slug]
    if not chosen:
        # Empty selection — fall back to defaults_on outcomes so the agent isn't a no-op.
        chosen = [pack for pack in raw_outcomes if pack.get("default_on")]

    tool_keys: list[str] = []
    seen: set[str] = set()
    for pack in chosen:
        for key in pack.get("tool_keys", []) or []:
            if key in seen:
                continue
            if key in available_tool_keys:
                tool_keys.append(key)
                seen.add(key)

    prompt_lines = ["This agent is purpose-built around the following outcomes:"]
    for pack in chosen:
        prompt_lines.append(f"- {pack['label']}: {pack['prompt_fragment']}")

    return {
        "name": default_agent_name(business_type),
        "description": "Auto-configured from your selected outcomes. Edit any time in Settings.",
        "system_prompt": "\n".join(prompt_lines),
        "tool_keys": tool_keys,
        "selected_outcomes": [pack["slug"] for pack in chosen],
    }
