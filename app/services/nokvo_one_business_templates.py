from __future__ import annotations

from copy import deepcopy
from typing import Any


# Real-estate is the ONLY supported vertical. Other verticals' configs/code
# remain in this module for now but are never selectable — onboarding,
# validation, and the options list all resolve to real_estate only.
ALLOWED_BUSINESS_TYPES = {"real_estate"}


# ─────────── Per-tab status vocabularies ───────────
#
# Used to (a) generate enum values for the `status` field in resource tool input
# schemas, (b) validate set_status transitions, and (c) flag regressive moves
# (e.g. closed→open) as requires_confirmation.
#
# Each entry: tab -> {"initial": str, "all": [str, ...], "forward": [str, ...]}
# `forward` is the canonical happy-path order; transitions that go backwards
# in this list are policy-gated to require human confirmation.
STATUS_VOCABULARIES: dict[str, dict[str, dict[str, Any]]] = {
    "real_estate": {
        "leads": {
            "initial": "new",
            "all": ["new", "contacted", "qualified", "visit_scheduled", "converted", "lost"],
            "forward": ["new", "contacted", "qualified", "visit_scheduled", "converted"],
        },
        "tickets": {
            "initial": "open",
            "all": ["open", "in_progress", "waiting_on_customer", "resolved", "closed"],
            "forward": ["open", "in_progress", "resolved", "closed"],
        },
    },
    "clinics": {
        # NOTE: clinics have NO leads tab — every inbound caller lands in the
        # Customer base (customer_base table) instead, and follow-ups are
        # admin-commanded from there.
        "tickets": {
            "initial": "open",
            "all": ["open", "in_progress", "waiting_on_patient", "resolved", "closed", "escalated"],
            "forward": ["open", "in_progress", "resolved", "closed"],
        },
        "appointments": {
            "initial": "requested",
            "all": ["requested", "assigned", "confirmed", "checked_in", "completed", "cancelled", "no_show", "rescheduled"],
            "forward": ["requested", "assigned", "confirmed", "checked_in", "completed"],
        },
    },
    "ecommerce": {
        "leads": {
            "initial": "new",
            "all": ["new", "contacted", "qualified", "converted", "lost"],
            "forward": ["new", "contacted", "qualified", "converted"],
        },
        "tickets": {
            "initial": "open",
            "all": ["open", "in_progress", "waiting_on_customer", "resolved", "closed", "escalated"],
            "forward": ["open", "in_progress", "resolved", "closed"],
        },
    },
    "hospitality": {
        "leads": {
            "initial": "new",
            "all": ["new", "contacted", "qualified", "booked", "lost"],
            "forward": ["new", "contacted", "qualified", "booked"],
        },
        "tickets": {
            "initial": "open",
            "all": ["open", "in_progress", "waiting_on_guest", "resolved", "closed", "escalated"],
            "forward": ["open", "in_progress", "resolved", "closed"],
        },
    },
    "other": {
        "leads": {
            "initial": "new",
            "all": ["new", "contacted", "qualified", "converted", "lost"],
            "forward": ["new", "contacted", "qualified", "converted"],
        },
        "tickets": {
            "initial": "open",
            "all": ["open", "in_progress", "resolved", "closed"],
            "forward": ["open", "in_progress", "resolved", "closed"],
        },
    },
}


# Maps each tab name to the `record_type` it lives under inside NokvoOneToolRecord.
# Keeping tabs and record_types decoupled lets us reuse the same storage row for
# tabs that share semantics (e.g. real_estate.leads and clinics.leads both write
# `record_type='lead'`).
TAB_TO_RECORD_TYPE: dict[str, str] = {
    "leads": "lead",
    "tickets": "ticket",
    "appointments": "appointment",
}


# Searchable JSONB keys per tab — used by {tab}_search and find_contact for
# indexed lookup over the data column.
TAB_SEARCH_KEYS: dict[str, tuple[str, ...]] = {
    "leads": ("full_name", "name", "customer_name", "patient_name", "guest_name", "phone", "email"),
    "tickets": ("subject", "customer", "customer_name", "patient_name", "guest_name", "order_id", "reservation_id", "property_id"),
    "appointments": ("patient_name", "doctor", "department", "reason"),
}


# Reserved slugs that custom tabs cannot use. The built-in tab slugs and a few
# system record_type prefixes are off-limits.
_RESERVED_CUSTOM_TAB_SLUGS: frozenset[str] = frozenset(
    {"leads", "tickets", "appointments", "callback", "call_log", "email_draft", "escalation", "lead", "ticket", "appointment"}
)


def _slugify(value: str) -> str:
    cleaned = (value or "").strip().lower().replace("-", "_").replace(" ", "_")
    cleaned = "".join(ch for ch in cleaned if ch.isalnum() or ch == "_")
    return cleaned


def normalize_custom_tab_slug(value: str) -> str:
    slug = _slugify(value)
    if not slug:
        raise ValueError("Custom tab slug is required")
    if slug in _RESERVED_CUSTOM_TAB_SLUGS:
        raise ValueError(f"Custom tab slug '{slug}' is reserved")
    if not slug[0].isalpha():
        raise ValueError("Custom tab slug must start with a letter")
    if len(slug) > 32:
        raise ValueError("Custom tab slug must be 32 characters or fewer")
    return slug


def custom_tab_record_type(slug: str) -> str:
    return f"custom_{normalize_custom_tab_slug(slug)}"


def custom_tab_tool_prefix(slug: str) -> str:
    """Tool key prefix for a custom tab — never collides with built-in tabs."""
    return normalize_custom_tab_slug(slug)


_DEFAULT_CUSTOM_TAB_STATUS_VOCAB: dict[str, Any] = {
    "initial": "open",
    "all": ["open", "in_progress", "done", "archived"],
    "forward": ["open", "in_progress", "done"],
}


def custom_tabs_from_overrides(provider_status: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Return validated, normalized list of custom-tab definitions for a tenant."""
    raw = (provider_status or {}).get("business_template_custom_tabs") or []
    if not isinstance(raw, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        slug_raw = entry.get("slug") or entry.get("key")
        if not slug_raw:
            continue
        try:
            slug = normalize_custom_tab_slug(str(slug_raw))
        except ValueError:
            continue
        if slug in seen_slugs:
            continue
        seen_slugs.add(slug)
        label = str(entry.get("label") or slug.replace("_", " ").title())
        fields = entry.get("fields")
        if not isinstance(fields, list):
            fields = []
        vocab = entry.get("status_vocabulary")
        if not isinstance(vocab, dict) or not vocab.get("all"):
            vocab = deepcopy(_DEFAULT_CUSTOM_TAB_STATUS_VOCAB)
        search_keys = entry.get("search_keys")
        if not isinstance(search_keys, list):
            search_keys = []
        normalized.append(
            {
                "slug": slug,
                "label": label,
                "fields": deepcopy(fields),
                "status_vocabulary": deepcopy(vocab),
                "search_keys": [str(k) for k in search_keys if isinstance(k, str)],
            }
        )
    return normalized


def default_custom_tab_status_vocabulary() -> dict[str, Any]:
    return deepcopy(_DEFAULT_CUSTOM_TAB_STATUS_VOCAB)


BUSINESS_TYPE_LABELS = {
    "real_estate": "Real Estate",
    "clinics": "Clinics",
    "ecommerce": "E-commerce",
    "hospitality": "Hospitality",
    "other": "Other",
}


BUSINESS_TYPE_CONFIGS: dict[str, dict[str, Any]] = {
    "real_estate": {
        "value": "real_estate",
        "label": "Real Estate",
        "member_label": "Agents",
        "tabs": ["tickets", "leads"],
        "request_types": [
            {"value": "property_inquiry", "label": "Property Inquiry"},
            {"value": "site_visit", "label": "Site Visit"},
            {"value": "pricing_query", "label": "Pricing Query"},
            {"value": "callback", "label": "Callback"},
            {"value": "document_query", "label": "Document Query"},
            {"value": "general_query", "label": "General Query"},
        ],
        "schemas": {
            # Lead Fields — an ENQUIRY (no site visit) captures ONLY the caller's
            # name + phone; everything else about the conversation lives in the
            # post-call "call notes" (data.handoff_note) written by the condenser.
            "leads": [
                {"key": "name", "label": "Customer Name", "type": "text", "required": False},
                {"key": "phone", "label": "Phone", "type": "phone", "required": False},
            ],
            # Site Visit Fields — captured when BOOKING a site visit. The
            # booking tool stores values under these keys so the Site Visits
            # tab renders them.
            "tickets": [
                {"key": "name", "label": "Customer Name", "type": "text", "required": True},
                {"key": "phone", "label": "Phone", "type": "phone", "required": True},
                {"key": "project_name", "label": "Project", "type": "text", "required": True},
                {"key": "visit_date", "label": "Visit Date", "type": "date", "required": True},
                {"key": "visit_time", "label": "Visit Time", "type": "time", "required": True},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
        },
        "prompt": (
            "Business Type: Real Estate. Two outcomes only: (1) book a SITE VISIT (capture name, phone, "
            "project, and a date/time) which goes to the Site Visits tab; or (2) if the caller only "
            "enquires about details and does not book a visit, capture a LEAD (just their name and phone) "
            "which goes to the Leads tab. Do not interrogate enquiry callers for budget or area — answer "
            "their property questions from the project inventory and capture only name + phone."
        ),
    },
    "clinics": {
        "value": "clinics",
        "label": "Clinics",
        "member_label": "Doctors / Staff",
        # No "leads" tab: clinic callers are captured in the Customer base.
        "tabs": ["tickets", "appointments"],
        "request_types": [
            {"value": "appointment", "label": "Appointment"},
            {"value": "follow_up", "label": "Follow-up"},
            {"value": "report_review", "label": "Report Review"},
            {"value": "billing_query", "label": "Billing Query"},
            {"value": "general_query", "label": "General Query"},
            {"value": "emergency_escalation", "label": "Emergency Escalation"},
        ],
        "consultation_types": [
            {"value": "general_consultation", "label": "General Consultation"},
            {"value": "follow_up", "label": "Follow-up"},
            {"value": "report_review", "label": "Report Review"},
            {"value": "vaccination", "label": "Vaccination"},
            {"value": "procedure", "label": "Procedure"},
            {"value": "emergency_escalation", "label": "Emergency Escalation"},
        ],
        "schemas": {
            # No "leads" schema: interested-but-not-booking callers are already
            # in the Customer base (number + post-call notes) automatically.
            "tickets": [
                {"key": "patient_name", "label": "Patient Name", "type": "text", "required": True},
                {"key": "ticket_type", "label": "Request Type", "type": "select", "required": True},
                {"key": "priority", "label": "Urgency", "type": "select", "required": True},
                {"key": "department", "label": "Department", "type": "text", "required": False},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
            "appointments": [
                # NOTE: no "service" slot by default — the booking flow resolves
                # the service from the free-text reason (find_service_match), and
                # admins can add an explicit Service field from FIELD_CATALOG.
                {"key": "patient_name", "label": "Patient Name", "type": "text", "required": True},
                {"key": "phone", "label": "Phone", "type": "phone", "required": True},
                {"key": "doctor", "label": "Doctor", "type": "text", "required": False},
                {"key": "department", "label": "Department", "type": "text", "required": False},
                {"key": "appointment_time", "label": "Date & Time", "type": "datetime", "required": True},
                {"key": "reason", "label": "Visit Reason", "type": "text", "required": True},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
        },
        "prompt": (
            "Business Type: Clinics. Support appointments, appointment intake, patient support "
            "tickets, doctor or department routing, and reminders. Every caller is recorded in the "
            "clinic's customer base automatically — do not interrogate non-booking callers for "
            "details; answer their questions and offer to book. Do not provide diagnosis, treatment, "
            "or emergency medical advice; escalate urgent symptoms, medical uncertainty, billing disputes, "
            "or privacy-sensitive requests to clinic staff."
        ),
    },
    "ecommerce": {
        "value": "ecommerce",
        "label": "E-commerce",
        "member_label": "Team Members",
        "tabs": ["tickets", "leads"],
        "request_types": [
            {"value": "order_status", "label": "Order Status"},
            {"value": "return_request", "label": "Return Request"},
            {"value": "refund_request", "label": "Refund Request"},
            {"value": "product_query", "label": "Product Query"},
            {"value": "complaint", "label": "Complaint"},
            {"value": "general_query", "label": "General Query"},
        ],
        "schemas": {
            "leads": [
                {"key": "customer_name", "label": "Customer Name", "type": "text", "required": True},
                {"key": "phone", "label": "Phone", "type": "phone", "required": False},
                {"key": "email", "label": "Email", "type": "email", "required": True},
                {"key": "product_interest", "label": "Interested In", "type": "text", "required": True},
                {"key": "cart_value", "label": "Cart Value", "type": "currency", "required": False},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
            "tickets": [
                {"key": "subject", "label": "Subject", "type": "text", "required": False},
                {"key": "customer_name", "label": "Customer Name", "type": "text", "required": True},
                {"key": "order_id", "label": "Order ID", "type": "text", "required": False},
                {"key": "issue_type", "label": "Request Type", "type": "select", "required": True},
                {"key": "priority", "label": "Urgency", "type": "select", "required": True},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
        },
        "prompt": (
            "Business Type: E-commerce. Prioritize product inquiries, cart recovery, order status, "
            "returns, refunds, shipping questions, and support tickets. Never promise refunds, inventory, "
            "or delivery outcomes without tool or approved policy evidence."
        ),
    },
    "hospitality": {
        "value": "hospitality",
        "label": "Hospitality",
        "member_label": "Staff",
        "tabs": ["tickets", "leads"],
        "request_types": [
            {"value": "booking", "label": "Booking"},
            {"value": "cancellation", "label": "Cancellation"},
            {"value": "guest_support", "label": "Guest Support"},
            {"value": "pricing_query", "label": "Pricing Query"},
            {"value": "complaint", "label": "Complaint"},
            {"value": "general_query", "label": "General Query"},
        ],
        "schemas": {
            "leads": [
                {"key": "guest_name", "label": "Guest Name", "type": "text", "required": True},
                {"key": "phone", "label": "Phone", "type": "phone", "required": True},
                {"key": "stay_dates", "label": "Stay Dates", "type": "text", "required": False},
                {"key": "room_type", "label": "Room Type", "type": "select", "required": False},
                {"key": "party_size", "label": "Guests", "type": "number", "required": False},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
            "tickets": [
                {"key": "guest_name", "label": "Guest Name", "type": "text", "required": True},
                {"key": "reservation_id", "label": "Reservation ID", "type": "text", "required": False},
                {"key": "issue_type", "label": "Request Type", "type": "select", "required": True},
                {"key": "priority", "label": "Urgency", "type": "select", "required": True},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
        },
        "prompt": (
            "Business Type: Hospitality. Prioritize guest inquiries, booking leads, reservation support, "
            "amenity questions, check-in/check-out coordination, complaints, and service recovery tickets."
        ),
    },
    "other": {
        "value": "other",
        "label": "Other",
        "member_label": "Members",
        "tabs": ["tickets", "leads"],
        "request_types": [
            {"value": "lead", "label": "Lead"},
            {"value": "support", "label": "Support"},
            {"value": "callback", "label": "Callback"},
            {"value": "complaint", "label": "Complaint"},
            {"value": "general_query", "label": "General Query"},
        ],
        "schemas": {
            "leads": [
                {"key": "name", "label": "Name", "type": "text", "required": True},
                {"key": "phone", "label": "Phone", "type": "phone", "required": False},
                {"key": "email", "label": "Email", "type": "email", "required": False},
                {"key": "need", "label": "What They Need", "type": "text", "required": True},
                {"key": "source", "label": "Source", "type": "select", "required": False},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
            "tickets": [
                {"key": "customer", "label": "Customer Name", "type": "text", "required": True},
                {"key": "issue_type", "label": "Request Type", "type": "select", "required": True},
                {"key": "priority", "label": "Urgency", "type": "select", "required": True},
                {"key": "owner", "label": "Owner", "type": "text", "required": False},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
        },
        "prompt": (
            "Business Type: Other. Use a general customer-operations posture focused on lead capture, "
            "ticket triage, follow-up, and concise escalation when the business context is unclear."
        ),
    },
}


def normalize_business_type(value: str | None) -> str | None:
    # Permissive: recognizes any business type that still has a config (so
    # back-compat code + the dynamic tool resolver keep working). The
    # real-estate-only LOCK is enforced by ``validate_business_type`` (user
    # input) and ``business_type_options`` (onboarding), not here.
    if value is None:
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized in BUSINESS_TYPE_CONFIGS:
        return normalized
    return None


def enabled_business_types() -> set[str]:
    """Business types selectable at onboarding: the always-on base
    (``ALLOWED_BUSINESS_TYPES``) plus any added via
    ``settings.ENABLED_BUSINESS_TYPES`` (comma-separated), intersected with what
    actually has a config. Staged rollout (P3): default = real_estate only, so
    behaviour is unchanged until the env var is set.
    """
    from app.core.config import settings

    extra = {
        t.strip().lower().replace("-", "_").replace(" ", "_")
        for t in (settings.ENABLED_BUSINESS_TYPES or "").split(",")
        if t.strip()
    }
    return (set(ALLOWED_BUSINESS_TYPES) | extra) & set(BUSINESS_TYPE_CONFIGS.keys())


def validate_business_type(value: str) -> str:
    normalized = normalize_business_type(value)
    allowed = enabled_business_types()
    if normalized not in allowed:
        raise ValueError(f"Business type must be one of: {', '.join(sorted(allowed))}")
    return normalized


def business_type_options() -> list[dict[str, Any]]:
    # The enabled verticals, in config order (real_estate first). Staged via
    # settings.ENABLED_BUSINESS_TYPES; default = real_estate only.
    enabled = enabled_business_types()
    return [deepcopy(cfg) for bt, cfg in BUSINESS_TYPE_CONFIGS.items() if bt in enabled]


def business_type_config(value: str | None) -> dict[str, Any] | None:
    normalized = normalize_business_type(value)
    if normalized is None:
        return None
    return deepcopy(BUSINESS_TYPE_CONFIGS[normalized])


def allowed_request_types(value: str | None) -> set[str]:
    config = business_type_config(value)
    if config is None:
        return set()
    return {str(item["value"]) for item in config.get("request_types") or []}


def allowed_consultation_types(value: str | None) -> set[str]:
    config = business_type_config(value)
    if config is None:
        return set()
    return {str(item["value"]) for item in config.get("consultation_types") or []}


def member_label_for_business_type(value: str | None) -> str:
    config = business_type_config(value)
    if config is None:
        return "Members"
    return str(config.get("member_label") or "Members")


def apply_schema_overrides(config: dict[str, Any], overrides: dict[str, Any] | None) -> dict[str, Any]:
    merged = deepcopy(config)
    schemas = dict(merged.get("schemas") or {})
    for key, fields in dict(overrides or {}).items():
        if key in schemas and isinstance(fields, list):
            schemas[key] = deepcopy(fields)
    merged["schemas"] = schemas
    return merged


def tab_status_vocabulary(business_type: str | None, tab: str) -> dict[str, Any] | None:
    """Return the status vocab {initial, all, forward} for (business_type, tab), or None."""
    normalized = normalize_business_type(business_type)
    if normalized is None:
        return None
    vocab = STATUS_VOCABULARIES.get(normalized, {}).get(tab)
    return deepcopy(vocab) if vocab else None


def tab_record_type(tab: str) -> str | None:
    return TAB_TO_RECORD_TYPE.get(tab)


def tab_search_keys(tab: str) -> tuple[str, ...]:
    return TAB_SEARCH_KEYS.get(tab, ())


def enabled_tabs_for(business_type: str | None) -> list[str]:
    config = business_type_config(business_type)
    if config is None:
        return []
    return [t for t in (config.get("tabs") or []) if t in TAB_TO_RECORD_TYPE]


# ─────────── Selectable field catalog ───────────
# A curated palette of fields per business-type + record (tab) that the admin can
# SELECT from (onboarding / Leads / Site-Visit pages). The chosen subset becomes the
# tab's schema (saved via PATCH /business-template/schemas/{key}); the agent then
# asks EXACTLY those fields. Each entry is a normal field dict {key, label, type,
# required}; its `kind` (→ how the agent phrases the question, in tool_flow_questions)
# is derived from key/label/type. Free-form custom fields are still allowed — this is
# the suggested palette, not a hard whitelist.
FIELD_CATALOG: dict[str, dict[str, list[dict[str, Any]]]] = {
    "real_estate": {
        # NOTE: leads are intentionally NOT configurable — an enquiry lead is
        # fixed to name + phone + post-call notes (see BUSINESS_TYPE_CONFIGS).
        # Only the site-visit (tickets) schema is admin-selectable.
        "tickets": [
            {"key": "name", "label": "Customer Name", "type": "text", "required": True},
            {"key": "phone", "label": "Phone", "type": "phone", "required": True},
            {"key": "project_name", "label": "Project", "type": "text", "required": False},
            {"key": "visit_date", "label": "Visit Date", "type": "date", "required": False},
            {"key": "visit_time", "label": "Visit Time", "type": "time", "required": False},
            {"key": "budget", "label": "Budget", "type": "currency", "required": False},
            {"key": "property_type", "label": "Looking For", "type": "select", "required": False},
            {"key": "location", "label": "Area", "type": "text", "required": False},
            {"key": "notes", "label": "Notes", "type": "text", "required": False},
        ],
    },
    "clinics": {
        "appointments": [
            {"key": "service", "label": "Service", "type": "select", "required": True},
            {"key": "patient_name", "label": "Patient Name", "type": "text", "required": True},
            {"key": "phone", "label": "Phone", "type": "phone", "required": True},
            {"key": "appointment_time", "label": "Date & Time", "type": "datetime", "required": False},
            {"key": "reason", "label": "Visit Reason", "type": "text", "required": False},
            {"key": "notes", "label": "Notes", "type": "text", "required": False},
        ],
        # clinics have no leads — callers land in the Customer base instead.
    },
}


def field_catalog_for(business_type: str | None, schema_key: str) -> list[dict[str, Any]]:
    """The selectable field palette for a business-type + record (tab). Falls back
    to the default schema for that tab when no curated palette is defined, so every
    vertical/tab still offers a sensible list to pick from."""
    bt = normalize_business_type(business_type)
    palette = (FIELD_CATALOG.get(bt or "") or {}).get(schema_key)
    if palette:
        return [dict(entry) for entry in palette]
    config = business_type_config(bt)
    return [dict(f) for f in ((config or {}).get("schemas") or {}).get(schema_key) or []]


def business_template_prompt(value: str | None) -> str:
    config = business_type_config(value)
    if config is None:
        return (
            "Business Type: Not selected. Use general customer-operations behavior only and avoid "
            "assuming domain-specific workflows until the organization selects a Business Type."
        )
    return str(config["prompt"])
