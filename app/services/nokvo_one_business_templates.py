from __future__ import annotations

from copy import deepcopy
from typing import Any


ALLOWED_BUSINESS_TYPES = {"real_estate", "clinics", "ecommerce", "hospitality", "other"}


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
            "leads": [
                {"key": "name", "label": "Customer Name", "type": "text", "required": True},
                {"key": "phone", "label": "Phone", "type": "phone", "required": True},
                {"key": "property_type", "label": "Looking For", "type": "select", "required": False},
                {"key": "budget", "label": "Budget", "type": "currency", "required": False},
                {"key": "location", "label": "Area", "type": "text", "required": False},
                {"key": "visit_date", "label": "Visit Date", "type": "date", "required": False},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
            "tickets": [
                {"key": "customer", "label": "Customer Name", "type": "text", "required": True},
                {"key": "property_id", "label": "Property", "type": "text", "required": False},
                {"key": "issue_type", "label": "Request Type", "type": "select", "required": True},
                {"key": "priority", "label": "Urgency", "type": "select", "required": True},
                {"key": "assigned_to", "label": "Owner", "type": "text", "required": False},
            ],
        },
        "prompt": (
            "Business Type: Real Estate. Prioritize property inquiries, buyer/renter qualification, "
            "budget and location capture, site-visit scheduling, follow-ups, and ticket handling for "
            "property or documentation issues."
        ),
    },
    "clinics": {
        "value": "clinics",
        "label": "Clinics",
        "member_label": "Doctors / Staff",
        "tabs": ["tickets", "leads", "appointments"],
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
            "leads": [
                {"key": "patient_name", "label": "Patient Name", "type": "text", "required": True},
                {"key": "phone", "label": "Phone", "type": "phone", "required": True},
                {"key": "care_need", "label": "Reason", "type": "text", "required": True},
                {"key": "preferred_doctor", "label": "Doctor", "type": "text", "required": False},
                {"key": "source", "label": "Source", "type": "select", "required": False},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
            "tickets": [
                {"key": "patient_name", "label": "Patient Name", "type": "text", "required": True},
                {"key": "ticket_type", "label": "Request Type", "type": "select", "required": True},
                {"key": "priority", "label": "Urgency", "type": "select", "required": True},
                {"key": "department", "label": "Department", "type": "text", "required": False},
                {"key": "status", "label": "Status", "type": "select", "required": True},
            ],
            "appointments": [
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
            "Business Type: Clinics. Support appointments, appointment intake, clinic lead capture, patient support "
            "tickets, doctor or department routing, and reminders. Do not provide diagnosis, treatment, "
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
    if value is None:
        return None
    normalized = value.strip().lower().replace("-", "_").replace(" ", "_")
    if normalized == "real_estate":
        return normalized
    if normalized in ALLOWED_BUSINESS_TYPES:
        return normalized
    return None


def validate_business_type(value: str) -> str:
    normalized = normalize_business_type(value)
    if normalized is None:
        allowed = ", ".join(sorted(ALLOWED_BUSINESS_TYPES))
        raise ValueError(f"Business type must be one of: {allowed}")
    return normalized


def business_type_options() -> list[dict[str, Any]]:
    return [deepcopy(BUSINESS_TYPE_CONFIGS[key]) for key in ["real_estate", "clinics", "ecommerce", "hospitality", "other"]]


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


def business_template_prompt(value: str | None) -> str:
    config = business_type_config(value)
    if config is None:
        return (
            "Business Type: Not selected. Use general customer-operations behavior only and avoid "
            "assuming domain-specific workflows until the organization selects a Business Type."
        )
    return str(config["prompt"])
