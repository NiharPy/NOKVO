"""
Nokvo One predefined-tool catalog and dispatcher.

Two layers of tools live here:

1. **Cross-cutting tools** (CROSS_CUTTING_CATALOG) — business-type-agnostic
   primitives: log a call, schedule a callback, draft an email, escalate to a
   human, find a contact across a tab. Same for every org.

2. **Resource handlers** — generic CRUD handlers (`_handle_resource_*`)
   parameterized by `record_type`. The per-tab tools (`leads_create`,
   `tickets_update`, `appointments_set_status`, ...) are produced by
   `dynamic_tool_resolver.resolve_catalog()` from the org's business template
   and schema overrides; they route to these handlers via `handler_name`.

Hard rules:
  - No tool ever performs an external send. send_email_draft only writes a
    pending_confirmation draft row.
  - No tool hard-deletes data. {tab}_close transitions to a terminal status.
  - All side-effects go through NokvoOneToolRecord (org-scoped).
  - Every successful invocation is audited in AgentToolInvocation; identical
    repeated calls within IDEMPOTENCY_WINDOW_SECONDS are short-circuited.
"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable, Iterable

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_tool_invocation import AgentToolInvocation
from app.models.nokvo_one_tool_record import NokvoOneToolRecord
from app.models.organization import Organization
from app.services.nokvo_one_assignment_service import NokvoOneAssignmentService


JSONSchema = dict[str, Any]
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


IDEMPOTENCY_WINDOW_SECONDS = 60


@dataclass(frozen=True)
class PredefinedTool:
    key: str
    display_name: str
    description: str
    input_schema: JSONSchema
    record_type: str | None
    handler_name: str
    requires_confirmation: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)


# ─────────── Cross-cutting catalog ───────────

CROSS_CUTTING_CATALOG: tuple[PredefinedTool, ...] = (
    PredefinedTool(
        key="call_log_create",
        display_name="Log call",
        description="Record an interaction with a customer (call, chat, email).",
        record_type="call_log",
        handler_name="call_log_create",
        input_schema={
            "type": "object",
            "required": ["channel", "summary"],
            "properties": {
                "channel": {"type": "string", "enum": ["voice", "chat", "email", "other"]},
                "contact_name": {"type": "string", "maxLength": 200},
                "contact_phone": {"type": "string", "maxLength": 32},
                "contact_email": {"type": "string", "format": "email"},
                "summary": {"type": "string", "minLength": 1, "maxLength": 4000},
                "outcome": {"type": "string", "maxLength": 200},
                "duration_seconds": {"type": "integer", "minimum": 0, "maximum": 86400},
            },
            "additionalProperties": False,
        },
    ),
    PredefinedTool(
        key="call_log_search",
        display_name="Search call history",
        description="Retrieve recent call/interaction history, optionally filtered by contact.",
        record_type="call_log",
        handler_name="call_log_search",
        input_schema={
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
                "contact_email": {"type": "string", "format": "email"},
                "contact_phone": {"type": "string", "maxLength": 32},
            },
            "additionalProperties": False,
        },
    ),
    PredefinedTool(
        key="schedule_callback",
        display_name="Schedule callback",
        description="Schedule a callback for a customer.",
        record_type="callback",
        handler_name="schedule_callback",
        input_schema={
            "type": "object",
            "required": ["contact_phone", "callback_at"],
            "properties": {
                "contact_name": {"type": "string", "maxLength": 200},
                "contact_phone": {"type": "string", "minLength": 4, "maxLength": 32},
                "callback_at": {"type": "string", "format": "date-time"},
                "notes": {"type": "string", "maxLength": 2000},
            },
            "additionalProperties": False,
        },
    ),
    PredefinedTool(
        key="send_email_draft",
        display_name="Draft email (requires confirmation)",
        description=(
            "Create a draft email queued for human confirmation. The agent cannot send "
            "external email directly — a human must review and confirm the draft from the portal."
        ),
        record_type="email_draft",
        handler_name="send_email_draft",
        requires_confirmation=True,
        input_schema={
            "type": "object",
            "required": ["to_email", "subject", "body"],
            "properties": {
                "to_email": {"type": "string", "format": "email"},
                "subject": {"type": "string", "minLength": 1, "maxLength": 200},
                "body": {"type": "string", "minLength": 1, "maxLength": 8000},
                "cc": {"type": "string", "format": "email"},
            },
            "additionalProperties": False,
        },
    ),
    PredefinedTool(
        key="escalate_to_human",
        display_name="Escalate to human",
        description="Flag a conversation for human follow-up with a reason and priority.",
        record_type="escalation",
        handler_name="escalate_to_human",
        input_schema={
            "type": "object",
            "required": ["reason"],
            "properties": {
                "reason": {"type": "string", "minLength": 1, "maxLength": 1000},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
                "contact_name": {"type": "string", "maxLength": 200},
                "contact_phone": {"type": "string", "maxLength": 32},
                "contact_email": {"type": "string", "format": "email"},
                "related_record_id": {"type": "string", "format": "uuid"},
            },
            "additionalProperties": False,
        },
    ),
    PredefinedTool(
        key="find_contact",
        display_name="Find contact",
        description=(
            "Look up the most recent record matching a phone or email inside one tab "
            "(leads | tickets | appointments). Returns id + summary or null."
        ),
        record_type=None,
        handler_name="find_contact",
        input_schema={
            "type": "object",
            "required": ["tab"],
            "properties": {
                "tab": {"type": "string", "enum": ["leads", "tickets", "appointments"]},
                "phone": {"type": "string", "maxLength": 32},
                "email": {"type": "string", "format": "email"},
            },
            "additionalProperties": False,
        },
    ),
)


_CROSS_CUTTING_INDEX: dict[str, PredefinedTool] = {tool.key: tool for tool in CROSS_CUTTING_CATALOG}


# ─────────── Macro catalog (per business_type) ───────────
#
# Macros are compound tools that perform a multi-step workflow in a single
# invocation. They write multiple NokvoOneToolRecord rows (e.g. a lead AND
# an appointment) and return a structured result with all created ids.
#
# Macros are STRICTLY gated to a business_type. The resolver only emits the
# macros whose key appears in MACRO_CATALOG[business_type].

MACRO_CATALOG: dict[str, tuple[PredefinedTool, ...]] = {
    "clinics": (
        PredefinedTool(
            key="book_appointment_with_lead_capture",
            display_name="Book appointment + capture lead",
            description=(
                "Atomic workflow for clinics: creates a lead if the patient is new, "
                "then books an appointment for them. Returns both record ids."
            ),
            record_type=None,
            handler_name="macro_book_appointment_with_lead_capture",
            input_schema={
                "type": "object",
                "required": ["patient_name", "phone", "reason", "appointment_time"],
                "properties": {
                    "patient_name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "phone": {"type": "string", "minLength": 4, "maxLength": 32},
                    "email": {"type": "string", "format": "email"},
                    "reason": {"type": "string", "minLength": 1, "maxLength": 500},
                    "preferred_doctor": {"type": "string", "maxLength": 200},
                    "department": {"type": "string", "maxLength": 200},
                    "appointment_time": {"type": "string", "format": "date-time"},
                    "source": {"type": "string", "maxLength": 80},
                },
                "additionalProperties": False,
            },
        ),
    ),
    "real_estate": (
        PredefinedTool(
            key="qualify_lead_and_schedule_visit",
            display_name="Qualify lead + schedule site visit",
            description=(
                "Real-estate workflow: books a SITE VISIT — creates a site-visit "
                "record in the Site Visits tab (captured per the org's Site Visit "
                "Fields) and schedules a callback, assigning an available agent. "
                "Set project_name to the project the caller asked about (matches the "
                "projects list in the system prompt)."
            ),
            record_type=None,
            handler_name="macro_qualify_lead_and_schedule_visit",
            input_schema={
                "type": "object",
                "required": ["name", "phone", "visit_at"],
                "properties": {
                    "name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "phone": {"type": "string", "minLength": 4, "maxLength": 32},
                    "email": {"type": "string", "format": "email"},
                    "property_type": {"type": "string", "maxLength": 80},
                    "budget": {"type": "number"},
                    "location": {"type": "string", "maxLength": 200},
                    "visit_at": {"type": "string", "format": "date-time"},
                    "notes": {"type": "string", "maxLength": 2000},
                    "project_id": {"type": "string", "maxLength": 80},
                    "project_name": {"type": "string", "maxLength": 200},
                    # Field-keyed site-visit data, built by the caller from the
                    # org's Site Visit Fields so the Site Visits tab renders
                    # populated cells. Free-form because field keys are
                    # admin-configurable.
                    "record_data": {"type": "object"},
                },
                "additionalProperties": False,
            },
        ),
        PredefinedTool(
            key="project_search",
            display_name="Search projects",
            description=(
                "Real-estate inventory lookup: returns the org's ACTIVE projects "
                "matching optional filters — max/min budget in rupees, a "
                "configuration like '2 BHK' or 'villa', and/or an area. Use it to "
                "answer questions like 'what do you have under 1 crore' or 'any "
                "3 BHK in Kokapet' precisely instead of guessing from memory. "
                "Returns name, location, configurations, price and possession for "
                "each match."
            ),
            record_type=None,
            handler_name="macro_project_search",
            input_schema={
                "type": "object",
                "required": [],
                "properties": {
                    "budget_max": {
                        "type": "number",
                        "description": "Maximum budget in rupees (e.g. 10000000 for ₹1 crore).",
                    },
                    "budget_min": {
                        "type": "number",
                        "description": "Minimum budget in rupees.",
                    },
                    "configuration": {
                        "type": "string",
                        "maxLength": 40,
                        "description": "Desired configuration, e.g. '2 BHK' or 'villa'.",
                    },
                    "location": {
                        "type": "string",
                        "maxLength": 120,
                        "description": "Preferred area / locality.",
                    },
                    "limit": {"type": "integer", "minimum": 1, "maximum": 20, "default": 10},
                },
                "additionalProperties": False,
            },
        ),
        PredefinedTool(
            key="request_brochure",
            display_name="Log brochure request",
            description=(
                "Real-estate: log that the caller asked for a project's brochure "
                "or details to be sent to them. Creates a follow-up task for the "
                "team with the caller's phone and the matched project, and reports "
                "whether a brochure link is on file. Use when the caller says "
                "'send me the brochure/details'. This does NOT itself send an SMS "
                "— a teammate dispatches it from the task."
            ),
            record_type=None,
            handler_name="macro_request_brochure",
            input_schema={
                "type": "object",
                "required": ["phone"],
                "properties": {
                    "name": {"type": "string", "maxLength": 200},
                    "phone": {"type": "string", "minLength": 4, "maxLength": 32},
                    "project_id": {"type": "string", "maxLength": 80},
                    "project_name": {"type": "string", "maxLength": 200},
                    "channel": {
                        "type": "string",
                        "enum": ["sms", "whatsapp", "email"],
                        "default": "whatsapp",
                    },
                },
                "additionalProperties": False,
            },
        ),
    ),
    "ecommerce": (
        PredefinedTool(
            key="open_return_ticket_with_order_lookup",
            display_name="Open return ticket + log call",
            description=(
                "E-commerce workflow: opens a return_request ticket scoped to an order, "
                "and logs the customer interaction. Returns ticket_id + call_log_id."
            ),
            record_type=None,
            handler_name="macro_open_return_ticket_with_order_lookup",
            input_schema={
                "type": "object",
                "required": ["customer_name", "issue_summary"],
                "properties": {
                    "customer_name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "email": {"type": "string", "format": "email"},
                    "phone": {"type": "string", "maxLength": 32},
                    "order_id": {"type": "string", "maxLength": 80},
                    "issue_summary": {"type": "string", "minLength": 1, "maxLength": 2000},
                    "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
                    "channel": {"type": "string", "enum": ["voice", "chat", "email", "other"], "default": "voice"},
                },
                "additionalProperties": False,
            },
        ),
    ),
    "hospitality": (
        PredefinedTool(
            key="create_reservation_lead_and_hold",
            display_name="Create reservation lead + schedule hold-confirm",
            description=(
                "Hospitality workflow: captures a qualified booking lead and schedules a "
                "callback to confirm the hold. Returns lead_id + callback_id."
            ),
            record_type=None,
            handler_name="macro_create_reservation_lead_and_hold",
            input_schema={
                "type": "object",
                "required": ["guest_name", "phone", "stay_dates", "confirm_at"],
                "properties": {
                    "guest_name": {"type": "string", "minLength": 1, "maxLength": 200},
                    "phone": {"type": "string", "minLength": 4, "maxLength": 32},
                    "email": {"type": "string", "format": "email"},
                    "stay_dates": {"type": "string", "maxLength": 200},
                    "room_type": {"type": "string", "maxLength": 80},
                    "party_size": {"type": "integer", "minimum": 1, "maximum": 64},
                    "confirm_at": {"type": "string", "format": "date-time"},
                    "notes": {"type": "string", "maxLength": 2000},
                },
                "additionalProperties": False,
            },
        ),
    ),
}


def macros_for_business_type(business_type: str | None) -> tuple[PredefinedTool, ...]:
    if business_type is None:
        return ()
    return MACRO_CATALOG.get(business_type, ())


# ─────────── Backwards-compat aliases ───────────
# Older NokvoOneAgent rows reference these v0 tool keys. We map them to the new
# resolver-produced or cross-cutting keys so existing agents continue to work
# without a migration.
LEGACY_KEY_ALIASES: dict[str, str] = {
    "lead_tracker_create_lead": "leads_create",
    "lead_tracker_update_status": "leads_set_status",
    "lead_tracker_add_note": "leads_add_note",
    "call_logger_create_entry": "call_log_create",
    "call_logger_get_history": "call_log_search",
    "create_ticket": "tickets_create",
}


def resolve_legacy_key(key: str) -> str:
    return LEGACY_KEY_ALIASES.get(key, key)


# ─────────── Public catalog accessors (cross-cutting only) ───────────


def list_cross_cutting_tools() -> list[dict[str, Any]]:
    return [
        {
            "key": tool.key,
            "display_name": tool.display_name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "requires_confirmation": tool.requires_confirmation,
        }
        for tool in CROSS_CUTTING_CATALOG
    ]


def get_cross_cutting_tool(key: str) -> PredefinedTool | None:
    return _CROSS_CUTTING_INDEX.get(key)


def validate_cross_cutting_keys(keys: Iterable[str]) -> list[str]:
    resolved = [resolve_legacy_key(k) for k in keys]
    invalid = [k for k in resolved if k not in _CROSS_CUTTING_INDEX]
    if invalid:
        raise ValueError(f"Unknown cross-cutting tool keys: {invalid}")
    return resolved


# Legacy export names — kept so callers that haven't migrated still import cleanly.
def list_tools() -> list[dict[str, Any]]:
    return list_cross_cutting_tools()


def get_tool(key: str) -> PredefinedTool | None:
    return get_cross_cutting_tool(resolve_legacy_key(key))


# ─────────── Idempotency ───────────


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def compute_idempotency_key(session_id: str | None, tool_key: str, arguments: dict[str, Any]) -> str:
    payload = f"{session_id or ''}|{tool_key}|{_canonical_json(arguments)}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _validate_tool_arguments(tool: PredefinedTool, arguments: dict[str, Any]) -> None:
    schema = tool.input_schema or {}
    properties = schema.get("properties") or {}
    required = [str(item) for item in schema.get("required") or []]
    if not isinstance(arguments, dict):
        raise ValueError("Tool arguments must be a JSON object.")

    missing = [
        key
        for key in required
        if key not in arguments or arguments.get(key) is None or str(arguments.get(key)).strip() == ""
    ]
    if missing:
        raise ValueError(f"Missing required fields: {', '.join(missing)}")

    if schema.get("additionalProperties") is False:
        extra = [key for key in arguments if key not in properties]
        if extra:
            raise ValueError(f"Unsupported fields for {tool.key}: {', '.join(extra)}")

    for key, value in arguments.items():
        field = properties.get(key)
        if not isinstance(field, dict) or value is None:
            continue
        expected_type = field.get("type")
        if expected_type == "string":
            if not isinstance(value, str):
                raise ValueError(f"Field '{key}' must be a string.")
            if field.get("minLength") is not None and len(value.strip()) < int(field["minLength"]):
                raise ValueError(f"Field '{key}' is too short.")
            if field.get("maxLength") is not None and len(value) > int(field["maxLength"]):
                raise ValueError(f"Field '{key}' is too long.")
            if field.get("format") == "email" and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value.strip()):
                raise ValueError(f"Field '{key}' must be a valid email address.")
        elif expected_type == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                raise ValueError(f"Field '{key}' must be an integer.")
            if field.get("minimum") is not None and value < int(field["minimum"]):
                raise ValueError(f"Field '{key}' is below the minimum.")
            if field.get("maximum") is not None and value > int(field["maximum"]):
                raise ValueError(f"Field '{key}' is above the maximum.")
        elif expected_type == "number":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                raise ValueError(f"Field '{key}' must be a number.")
        enum_values = field.get("enum")
        if enum_values and value not in enum_values:
            raise ValueError(f"Field '{key}' must be one of: {', '.join(map(str, enum_values))}.")


# ─────────── Dispatcher ───────────


class PredefinedToolsService:
    """Async dispatcher for Nokvo One predefined tool calls.

    Callers must pass the resolved `tool` (from the cross-cutting index or
    dynamic_tool_resolver) so the dispatcher knows which handler to run and
    which record_type to scope to. This avoids re-resolving the catalog inside
    every execute() call.
    """

    @staticmethod
    async def execute(
        db: AsyncSession,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        tool: PredefinedTool | str,
        arguments: dict[str, Any],
        *,
        agent_id: uuid.UUID | None = None,
        session_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(tool, str):
            resolved_key = resolve_legacy_key(tool)
            cross = get_cross_cutting_tool(resolved_key)
            if cross is None:
                raise ValueError(f"Unknown tool '{tool}'")
            tool = cross
        _validate_tool_arguments(tool, arguments)
        idem_key = compute_idempotency_key(session_id, tool.key, arguments)
        cached = await _recent_invocation(db, organization_id, idem_key)
        if cached is not None:
            return {"idempotent": True, **(cached.result or {})}

        handler_name = f"_handle_{tool.handler_name}"
        handler = getattr(PredefinedToolsService, handler_name, None)
        if handler is None:
            raise ValueError(f"No handler '{handler_name}' for tool '{tool.key}'")

        try:
            result = await handler(db, organization_id, user_id, tool, arguments)
        except ValueError:
            # Validation errors propagate; do NOT write an audit row (no side-effect ran).
            raise

        await _record_invocation(
            db,
            organization_id=organization_id,
            agent_id=agent_id,
            session_id=session_id,
            tool_key=tool.key,
            idempotency_key=idem_key,
            arguments=arguments,
            result=result,
        )
        return result

    # ─────────── Generic resource handlers ───────────

    @staticmethod
    async def _handle_resource_create(db, org_id, user_id, tool, args):
        record_type = tool.record_type
        if record_type is None:
            raise ValueError(f"Tool '{tool.key}' has no record_type")

        vocab = _tool_status_vocabulary(tool)
        initial_status = (vocab or {}).get("initial") or "open"

        data = dict(args)
        contact_phone, contact_email = _extract_contact(data)
        rec = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type=record_type,
            status=initial_status,
            data=data,
            contact_phone=contact_phone,
            contact_email=contact_email,
        )
        db.add(rec)
        await db.flush()
        assignment = await _maybe_assign_created_resource(db, org_id, user_id, tool, rec, data)
        response = {
            "ok": True,
            "id": str(rec.id),
            "record_type": record_type,
            "status": rec.status,
        }
        if assignment:
            response["assignment"] = assignment
            if assignment.get("selected_member_name"):
                response["assigned_member_name"] = assignment["selected_member_name"]
            response["assignment_status"] = assignment.get("assignment_status")
            response["scheduled_time"] = assignment.get("scheduled_time")
            response["requested_time"] = assignment.get("requested_time")
            response["time_adjusted"] = bool(assignment.get("time_adjusted"))
        return response

    @staticmethod
    async def _handle_resource_update(db, org_id, user_id, tool, args):
        record_type = tool.record_type
        if record_type is None:
            raise ValueError(f"Tool '{tool.key}' has no record_type")

        rec = await _resolve_record(db, org_id, record_type, args)
        new_data = dict(rec.data or {})
        for key, value in args.items():
            if key in {"id", "phone", "email"} and key not in new_data:
                continue
            if value is None:
                continue
            new_data[key] = value
        rec.data = new_data
        contact_phone, contact_email = _extract_contact(new_data)
        if contact_phone:
            rec.contact_phone = contact_phone
        if contact_email:
            rec.contact_email = contact_email
        await db.flush()
        return {"ok": True, "id": str(rec.id), "status": rec.status}

    @staticmethod
    async def _handle_resource_get(db, org_id, user_id, tool, args):
        record_type = tool.record_type
        rec = await _fetch_by_id(db, org_id, args["id"], record_type)
        return {
            "ok": True,
            "id": str(rec.id),
            "record_type": rec.record_type,
            "status": rec.status,
            "data": rec.data or {},
            "created_at": rec.created_at.isoformat() if rec.created_at else None,
        }

    @staticmethod
    async def _handle_resource_list(db, org_id, user_id, tool, args):
        record_type = tool.record_type
        limit = int(args.get("limit") or 10)
        stmt = (
            select(NokvoOneToolRecord)
            .where(
                NokvoOneToolRecord.organization_id == org_id,
                NokvoOneToolRecord.record_type == record_type,
            )
            .order_by(NokvoOneToolRecord.created_at.desc())
            .limit(limit)
        )
        if args.get("status"):
            stmt = stmt.where(NokvoOneToolRecord.status == args["status"])
        result = await db.execute(stmt)
        rows = result.scalars().all()
        return {
            "ok": True,
            "entries": [_row_summary(r) for r in rows],
        }

    @staticmethod
    async def _handle_resource_search(db, org_id, user_id, tool, args):
        from app.services.nokvo_one_business_templates import tab_search_keys

        record_type = tool.record_type
        tab = _tab_from_tool_key(tool.key) or ""
        keys = tuple(tab_search_keys(tab)) or tuple(tool.metadata.get("custom_tab_search_keys") or ())
        query = str(args.get("query") or "").strip()
        limit = int(args.get("limit") or 10)
        if not query:
            return {"ok": True, "entries": []}

        stmt = (
            select(NokvoOneToolRecord)
            .where(
                NokvoOneToolRecord.organization_id == org_id,
                NokvoOneToolRecord.record_type == record_type,
            )
            .order_by(NokvoOneToolRecord.created_at.desc())
            .limit(200)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        lowered = query.lower()
        matches: list[NokvoOneToolRecord] = []
        for row in rows:
            data = row.data or {}
            haystack_parts = [str(data.get(k) or "") for k in keys] + [
                str(row.contact_phone or ""),
                str(row.contact_email or ""),
            ]
            haystack = " ".join(haystack_parts).lower()
            if lowered in haystack:
                matches.append(row)
                if len(matches) >= limit:
                    break
        return {"ok": True, "entries": [_row_summary(r) for r in matches]}

    @staticmethod
    async def _handle_resource_add_note(db, org_id, user_id, tool, args):
        record_type = tool.record_type
        rec = await _fetch_by_id(db, org_id, args["id"], record_type)
        data = dict(rec.data or {})
        history = list(data.get("notes_history") or [])
        history.append(
            {
                "note": args["note"],
                "at": datetime.now(timezone.utc).isoformat(),
                "by_user_id": str(user_id) if user_id else None,
            }
        )
        data["notes_history"] = history
        rec.data = data
        await db.flush()
        return {"ok": True, "id": str(rec.id), "note_count": len(history)}

    @staticmethod
    async def _handle_resource_set_status(db, org_id, user_id, tool, args):
        record_type = tool.record_type
        rec = await _fetch_by_id(db, org_id, args["id"], record_type)

        new_status = args["status"]
        vocab = _tool_status_vocabulary(tool)
        if vocab and new_status not in (vocab.get("all") or []):
            raise ValueError(f"Status '{new_status}' is not allowed for this resource")

        regressive = False
        if vocab:
            forward = list(vocab.get("forward") or [])
            try:
                cur_ix = forward.index(rec.status)
                new_ix = forward.index(new_status)
                regressive = new_ix < cur_ix
            except ValueError:
                regressive = False

        prior = rec.status
        rec.status = new_status
        await db.flush()
        return {
            "ok": True,
            "id": str(rec.id),
            "prior_status": prior,
            "status": rec.status,
            "regressive": regressive,
        }

    @staticmethod
    async def _handle_resource_close(db, org_id, user_id, tool, args):
        record_type = tool.record_type
        rec = await _fetch_by_id(db, org_id, args["id"], record_type)
        tab = _tab_from_tool_key(tool.key) or ""
        vocab = _tool_status_vocabulary(tool)
        if tab == "appointments":
            terminal = "cancelled"
        elif tool.metadata.get("custom_tab_terminal_status"):
            terminal = str(tool.metadata["custom_tab_terminal_status"])
        elif vocab and vocab.get("forward"):
            terminal = vocab["forward"][-1]
        else:
            terminal = "closed"
        rec.status = terminal
        data = dict(rec.data or {})
        if args.get("reason"):
            data["close_reason"] = args["reason"]
        rec.data = data
        await db.flush()
        return {"ok": True, "id": str(rec.id), "status": rec.status}

    # ─────────── Cross-cutting handlers ───────────

    @staticmethod
    async def _handle_call_log_create(db, org_id, user_id, tool, args):
        rec = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="call_log",
            status="logged",
            data={
                "channel": args["channel"],
                "contact_name": args.get("contact_name"),
                "contact_phone": args.get("contact_phone"),
                "contact_email": args.get("contact_email"),
                "summary": args["summary"],
                "outcome": args.get("outcome"),
                "duration_seconds": args.get("duration_seconds"),
            },
            contact_phone=args.get("contact_phone"),
            contact_email=args.get("contact_email"),
        )
        db.add(rec)
        await db.flush()
        return {"ok": True, "call_log_id": str(rec.id)}

    @staticmethod
    async def _handle_call_log_search(db, org_id, user_id, tool, args):
        from app.services.voice_data_audit_service import VoiceDataAuditService

        limit = int(args.get("limit") or 10)
        clauses = [
            NokvoOneToolRecord.organization_id == org_id,
            NokvoOneToolRecord.record_type == "call_log",
        ]
        if args.get("contact_email"):
            clauses.append(NokvoOneToolRecord.contact_email == args["contact_email"])
        if args.get("contact_phone"):
            clauses.append(NokvoOneToolRecord.contact_phone == args["contact_phone"])
        stmt = (
            select(NokvoOneToolRecord)
            .where(and_(*clauses))
            .order_by(NokvoOneToolRecord.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        await VoiceDataAuditService.log(
            db,
            organization_id=org_id,
            actor_type="organization_user" if user_id else "system",
            actor_id=str(user_id) if user_id else None,
            access_type="read",
            resource_type="call_log",
            reason="call_log_search_tool",
            metadata={"limit": limit, "results_count": len(rows)},
        )
        return {
            "ok": True,
            "entries": [
                {
                    "id": str(r.id),
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    **(r.data or {}),
                }
                for r in rows
            ],
        }

    @staticmethod
    async def _handle_schedule_callback(db, org_id, user_id, tool, args):
        rec = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="callback",
            status="scheduled",
            data={
                "contact_name": args.get("contact_name"),
                "contact_phone": args["contact_phone"],
                "callback_at": args["callback_at"],
                "notes": args.get("notes"),
            },
            contact_phone=args.get("contact_phone"),
        )
        db.add(rec)
        await db.flush()
        return {"ok": True, "callback_id": str(rec.id), "scheduled_for": args["callback_at"]}

    @staticmethod
    async def _handle_send_email_draft(db, org_id, user_id, tool, args):
        # CRITICAL: this never sends. It only stores a draft requiring confirmation.
        rec = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="email_draft",
            status="pending_confirmation",
            data={
                "to_email": args["to_email"],
                "cc": args.get("cc"),
                "subject": args["subject"],
                "body": args["body"],
                "drafted_at": datetime.now(timezone.utc).isoformat(),
            },
            contact_email=args["to_email"],
        )
        db.add(rec)
        await db.flush()
        return {
            "ok": True,
            "draft_id": str(rec.id),
            "status": "pending_confirmation",
            "message": (
                "Email draft created. A human must review and confirm it from the Nokvo One "
                "portal before it can be sent."
            ),
        }

    @staticmethod
    async def _handle_escalate_to_human(db, org_id, user_id, tool, args):
        rec = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="escalation",
            status="open",
            data={
                "reason": args["reason"],
                "priority": args.get("priority") or "normal",
                "contact_name": args.get("contact_name"),
                "contact_phone": args.get("contact_phone"),
                "contact_email": args.get("contact_email"),
                "related_record_id": args.get("related_record_id"),
                "flagged_at": datetime.now(timezone.utc).isoformat(),
            },
            contact_phone=args.get("contact_phone"),
            contact_email=args.get("contact_email"),
        )
        db.add(rec)
        await db.flush()
        return {"ok": True, "escalation_id": str(rec.id), "priority": rec.data["priority"]}

    @staticmethod
    async def _handle_find_contact(db, org_id, user_id, tool, args):
        from app.services.nokvo_one_business_templates import tab_record_type

        record_type = tab_record_type(args["tab"])
        if record_type is None:
            raise ValueError(f"Unknown tab '{args['tab']}'")
        phone = args.get("phone")
        email = args.get("email")
        if not phone and not email:
            raise ValueError("Provide phone or email")
        clauses: list[Any] = [
            NokvoOneToolRecord.organization_id == org_id,
            NokvoOneToolRecord.record_type == record_type,
        ]
        match_clauses: list[Any] = []
        if phone:
            match_clauses.append(NokvoOneToolRecord.contact_phone == phone)
        if email:
            match_clauses.append(NokvoOneToolRecord.contact_email == email)
        clauses.append(or_(*match_clauses))
        stmt = (
            select(NokvoOneToolRecord)
            .where(and_(*clauses))
            .order_by(NokvoOneToolRecord.created_at.desc())
            .limit(1)
        )
        result = await db.execute(stmt)
        rec = result.scalars().first()
        if rec is None:
            return {"ok": True, "match": None}
        return {"ok": True, "match": _row_summary(rec)}

    # ─────────── Macro handlers ───────────
    # Each macro creates multiple NokvoOneToolRecord rows in one invocation and
    # returns all created ids. The handlers reuse the same storage so the records
    # appear in the standard tab queries with no special-casing.

    @staticmethod
    async def _handle_macro_book_appointment_with_lead_capture(db, org_id, user_id, tool, args):
        lead = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="lead",
            status="scheduled",
            data={
                "patient_name": args["patient_name"],
                "phone": args["phone"],
                "email": args.get("email"),
                "care_need": args["reason"],
                "preferred_doctor": args.get("preferred_doctor"),
                "source": args.get("source"),
            },
            contact_phone=args["phone"],
            contact_email=args.get("email"),
        )
        appt = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="appointment",
            status="requested",
            data={
                "patient_name": args["patient_name"],
                "phone": args["phone"],
                "doctor": args.get("preferred_doctor"),
                "department": args.get("department"),
                "appointment_time": args["appointment_time"],
                "reason": args["reason"],
                "related_lead_id": str(lead.id),
            },
            contact_phone=args["phone"],
            contact_email=args.get("email"),
        )
        db.add(lead)
        db.add(appt)
        await db.flush()
        appointment_at = _parse_tool_datetime(args["appointment_time"], field_name="appointment_time")
        assignment = await _assign_existing_record(
            db,
            org_id,
            appt,
            request_type="appointment",
            requested_time=appointment_at,
            summary=args["reason"],
            metadata={
                "patient_name": args["patient_name"],
                "phone": args["phone"],
                "department": args.get("department"),
                "preferred_doctor": args.get("preferred_doctor"),
                "appointment_time": args["appointment_time"],
                "appointment_start": appointment_at.isoformat(),
                "reason": args["reason"],
                "related_lead_id": str(lead.id),
                "assignment_source": tool.key,
            },
        )
        scheduled_time = (assignment or {}).get("scheduled_time")
        return {
            "ok": True,
            "lead_id": str(lead.id),
            "appointment_id": str(appt.id),
            "appointment_status": appt.status,
            "assignment": assignment,
            "assigned_member_name": assignment.get("selected_member_name") if assignment else None,
            "assignment_status": assignment.get("assignment_status") if assignment else None,
            "scheduled_time": scheduled_time,
            "requested_time": args["appointment_time"],
            "time_adjusted": bool((assignment or {}).get("time_adjusted")),
        }

    @staticmethod
    async def _handle_macro_qualify_lead_and_schedule_visit(db, org_id, user_id, tool, args):
        from app.services.real_estate_project_service import (
            find_project_match,
            load_active_projects,
        )
        from app.services.nokvo_one_business_templates import STATUS_VOCABULARIES

        projects = await load_active_projects(db, org_id)
        matched = find_project_match(
            projects,
            project_id=args.get("project_id"),
            project_name=args.get("project_name"),
        )
        project_id_str = str(matched.id) if matched else (args.get("project_id") or None)
        project_name_str = (
            matched.name if matched else (args.get("project_name") or None)
        )

        # The site-visit record is stored as a TICKET (the Site Visits tab),
        # keyed by the org's configured Site Visit Fields. The caller builds
        # ``record_data`` from those fields; fall back to a minimal shape so
        # the macro still works if invoked without it.
        record_data = dict(args.get("record_data") or {})
        if not record_data:
            record_data = {
                "name": args["name"],
                "phone": args["phone"],
                "visit_date": args["visit_at"],
            }
            for key in ("email", "property_type", "budget", "location"):
                if args.get(key) not in (None, ""):
                    record_data[key] = args[key]
        record_data.setdefault("issue_type", "site_visit")
        record_data.setdefault("priority", "normal")
        if project_id_str:
            record_data["project_id"] = project_id_str
        if matched is not None:
            # A real project resolved — its canonical name wins over the raw
            # spoken/STT text the flow captured, so the Site Visits tab shows
            # the registered project name.
            record_data["project_name"] = matched.name
        elif project_name_str:
            record_data.setdefault("project_name", project_name_str)

        ticket_status = (
            (STATUS_VOCABULARIES.get("real_estate", {}).get("tickets") or {}).get("initial")
            or "open"
        )
        contact_phone, contact_email = _extract_contact(record_data)
        site_visit = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="ticket",
            status=ticket_status,
            data=record_data,
            contact_phone=contact_phone or args["phone"],
            contact_email=contact_email or args.get("email"),
        )
        callback_data = {
            "contact_name": args["name"],
            "contact_phone": args["phone"],
            "callback_at": args["visit_at"],
            "notes": args.get("notes") or "Site visit scheduled.",
            "related_ticket_id": str(site_visit.id),
        }
        if project_id_str:
            callback_data["project_id"] = project_id_str
        if project_name_str:
            callback_data["project_name"] = project_name_str

        callback = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="callback",
            status="scheduled",
            data=callback_data,
            contact_phone=args["phone"],
        )
        db.add(site_visit)
        db.add(callback)
        await db.flush()
        visit_at = _parse_tool_datetime(args["visit_at"], field_name="visit_at")
        assignment = await _assign_existing_record(
            db,
            org_id,
            callback,
            request_type="site_visit",
            requested_time=visit_at,
            summary=_assignment_summary_for(record_data, fallback="Real-estate site visit"),
            metadata={
                "contact_name": args["name"],
                "contact_phone": args["phone"],
                "visit_at": args["visit_at"],
                "callback_at": args["visit_at"],
                "related_ticket_id": str(site_visit.id),
                "assignment_source": tool.key,
                "project_id": project_id_str,
                "project_name": project_name_str,
            },
        )
        scheduled_visit = (assignment or {}).get("scheduled_time") or args["visit_at"]
        return {
            "ok": True,
            "id": str(site_visit.id),
            "ticket_id": str(site_visit.id),
            "ticket_status": site_visit.status,
            "record_type": "ticket",
            "callback_id": str(callback.id),
            "callback_status": callback.status,
            "scheduled_for": scheduled_visit,
            "requested_for": args["visit_at"],
            "time_adjusted": bool((assignment or {}).get("time_adjusted")),
            "assignment": assignment,
            "assigned_member_name": assignment.get("selected_member_name") if assignment else None,
            "assignment_status": assignment.get("assignment_status") if assignment else None,
            "project_id": project_id_str,
            "project_name": project_name_str,
        }

    @staticmethod
    async def _handle_macro_project_search(db, org_id, user_id, tool, args):
        from app.services.real_estate_project_service import (
            _format_price,
            filter_projects,
            load_active_projects,
        )

        projects = await load_active_projects(db, org_id)
        matches = filter_projects(
            projects,
            budget_max=args.get("budget_max"),
            budget_min=args.get("budget_min"),
            configuration=args.get("configuration"),
            location=args.get("location"),
        )
        limit = max(1, min(int(args.get("limit") or 10), 20))
        limited = matches[:limit]
        return {
            "ok": True,
            "count": len(matches),
            "returned": len(limited),
            "projects": [
                {
                    "id": str(p.id),
                    "name": p.name,
                    "location": p.location,
                    "property_type": p.property_type,
                    "configurations": list(p.configurations or []),
                    "price": _format_price(p),
                    "possession_date": p.possession_date,
                    "rera_number": p.rera_number,
                }
                for p in limited
            ],
        }

    @staticmethod
    async def _handle_macro_request_brochure(db, org_id, user_id, tool, args):
        from app.services.real_estate_project_service import (
            find_project_match,
            load_active_projects,
        )

        projects = await load_active_projects(db, org_id)
        matched = find_project_match(
            projects,
            project_id=args.get("project_id"),
            project_name=args.get("project_name"),
        )
        project_name_str = matched.name if matched else (args.get("project_name") or None)
        brochure_url = matched.brochure_url if matched else None
        channel = args.get("channel") or "whatsapp"

        data: dict[str, Any] = {
            "contact_name": args.get("name") or "Property inquiry",
            "contact_phone": args["phone"],
            "purpose": "brochure_dispatch",
            "channel": channel,
            "notes": (
                f"Send brochure/details for {project_name_str or 'the requested project'} "
                f"to the caller via {channel}."
            ),
        }
        if matched is not None:
            data["project_id"] = str(matched.id)
            data["project_name"] = matched.name
        elif project_name_str:
            data["project_name"] = project_name_str
        if brochure_url:
            data["brochure_url"] = brochure_url

        record = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="callback",
            status="scheduled",
            data=data,
            contact_phone=args["phone"],
        )
        db.add(record)
        await db.flush()
        return {
            "ok": True,
            "id": str(record.id),
            "record_type": "callback",
            "project_name": project_name_str,
            "brochure_on_file": bool(brochure_url),
            "channel": channel,
            "dispatch": "queued_for_team",
        }

    @staticmethod
    async def _handle_macro_open_return_ticket_with_order_lookup(db, org_id, user_id, tool, args):
        ticket = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="ticket",
            status="open",
            data={
                "customer_name": args["customer_name"],
                "email": args.get("email"),
                "phone": args.get("phone"),
                "order_id": args.get("order_id"),
                "issue_type": "return_request",
                "priority": args.get("priority") or "normal",
                "subject": f"Return request for order {args.get('order_id') or '(no order id)'}",
                "description": args["issue_summary"],
            },
            contact_phone=args.get("phone"),
            contact_email=args.get("email"),
        )
        call_log = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="call_log",
            status="logged",
            data={
                "channel": args.get("channel") or "voice",
                "contact_name": args["customer_name"],
                "contact_email": args.get("email"),
                "contact_phone": args.get("phone"),
                "summary": args["issue_summary"],
                "outcome": "Opened return-request ticket",
                "related_ticket_id": str(ticket.id),
            },
            contact_phone=args.get("phone"),
            contact_email=args.get("email"),
        )
        db.add(ticket)
        db.add(call_log)
        await db.flush()
        return {
            "ok": True,
            "ticket_id": str(ticket.id),
            "ticket_status": ticket.status,
            "call_log_id": str(call_log.id),
        }

    @staticmethod
    async def _handle_macro_create_reservation_lead_and_hold(db, org_id, user_id, tool, args):
        lead = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="lead",
            status="qualified",
            data={
                "guest_name": args["guest_name"],
                "phone": args["phone"],
                "email": args.get("email"),
                "stay_dates": args["stay_dates"],
                "room_type": args.get("room_type"),
                "party_size": args.get("party_size"),
            },
            contact_phone=args["phone"],
            contact_email=args.get("email"),
        )
        callback = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="callback",
            status="scheduled",
            data={
                "contact_name": args["guest_name"],
                "contact_phone": args["phone"],
                "callback_at": args["confirm_at"],
                "notes": args.get("notes") or "Confirm hospitality booking hold.",
                "related_lead_id": str(lead.id),
            },
            contact_phone=args["phone"],
        )
        db.add(lead)
        db.add(callback)
        await db.flush()
        return {
            "ok": True,
            "lead_id": str(lead.id),
            "callback_id": str(callback.id),
            "scheduled_for": args["confirm_at"],
        }


# ─────────── Internal helpers ───────────


def _row_summary(rec: NokvoOneToolRecord) -> dict[str, Any]:
    return {
        "id": str(rec.id),
        "record_type": rec.record_type,
        "status": rec.status,
        "data": rec.data or {},
        "created_at": rec.created_at.isoformat() if rec.created_at else None,
    }


def _extract_contact(data: dict[str, Any]) -> tuple[str | None, str | None]:
    phone = None
    email = None
    for key in ("phone", "contact_phone"):
        if data.get(key):
            phone = str(data[key])
            break
    for key in ("email", "contact_email"):
        if data.get(key):
            email = str(data[key])
            break
    return phone, email


def _tab_from_tool_key(key: str) -> str | None:
    if "_" not in key:
        return None
    head = key.split("_", 1)[0]
    if head in {"leads", "tickets", "appointments"}:
        return head
    return None


def _tool_status_vocabulary(tool: PredefinedTool) -> dict[str, Any] | None:
    """Resolve the status vocabulary for a resource tool.

    Built-in tabs route through the per-business_type STATUS_VOCABULARIES table.
    Custom tabs stamp their vocabulary directly into tool metadata.
    """
    from app.services.nokvo_one_business_templates import tab_status_vocabulary

    custom_vocab = tool.metadata.get("custom_tab_status_vocabulary")
    if custom_vocab:
        return dict(custom_vocab)
    tab = _tab_from_tool_key(tool.key)
    if tab is None:
        return None
    return tab_status_vocabulary(tool.metadata.get("business_type"), tab)


def _parse_tool_datetime(value: Any, *, field_name: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        raw = value.strip()
        if not raw:
            raise ValueError(f"Field '{field_name}' is required.")
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Field '{field_name}' must be an ISO date-time.") from exc
    else:
        raise ValueError(f"Field '{field_name}' must be an ISO date-time.")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def _load_organization(db: AsyncSession, org_id: uuid.UUID) -> Organization | None:
    res = await db.execute(select(Organization).where(Organization.id == org_id))
    return res.scalars().first()


def _assignment_summary_for(data: dict[str, Any], *, fallback: str = "") -> str:
    for key in ("reason", "care_need", "summary", "notes", "issue_summary", "property_type", "location"):
        value = data.get(key)
        if value:
            return str(value)
    return fallback


async def _assign_existing_record(
    db: AsyncSession,
    org_id: uuid.UUID,
    rec: NokvoOneToolRecord,
    *,
    request_type: str,
    requested_time: datetime,
    summary: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    organization = await _load_organization(db, org_id)
    if organization is None:
        return None
    assignment = await NokvoOneAssignmentService.assign_request(
        db,
        organization,
        request_type=request_type,
        request_id=rec.id,
        requested_time=requested_time,
        summary=summary,
        metadata=metadata or {},
        record_type=rec.record_type,
    )
    scheduled_time = assignment.get("scheduled_time")
    requested_at_resp = assignment.get("requested_time")
    return {
        "assignment_status": assignment.get("assignment_status"),
        "selected_member_id": str(assignment["selected_member_id"]) if assignment.get("selected_member_id") else None,
        "selected_member_name": assignment.get("selected_member_name"),
        "reason": assignment.get("reason"),
        "skipped_member_reasons": assignment.get("skipped_member_reasons") or {},
        "requested_time": requested_at_resp.isoformat() if isinstance(requested_at_resp, datetime) else requested_at_resp,
        "scheduled_time": scheduled_time.isoformat() if isinstance(scheduled_time, datetime) else scheduled_time,
        "time_adjusted": bool(assignment.get("time_adjusted")),
    }


async def _maybe_assign_created_resource(
    db: AsyncSession,
    org_id: uuid.UUID,
    user_id: uuid.UUID | None,
    tool: PredefinedTool,
    rec: NokvoOneToolRecord,
    data: dict[str, Any],
) -> dict[str, Any] | None:
    del user_id
    business_type = str((tool.metadata or {}).get("business_type") or "")
    tab = str((tool.metadata or {}).get("tab") or "")
    if business_type == "clinics" and tab == "appointments":
        appointment_at = _parse_tool_datetime(data.get("appointment_time"), field_name="appointment_time")
        metadata = {
            **data,
            "appointment_start": appointment_at.isoformat(),
            "assignment_source": tool.key,
        }
        return await _assign_existing_record(
            db,
            org_id,
            rec,
            request_type="appointment",
            requested_time=appointment_at,
            summary=_assignment_summary_for(data, fallback="Clinic appointment"),
            metadata=metadata,
        )
    return None


async def _fetch_by_id(
    db: AsyncSession, org_id: uuid.UUID, record_id: str, record_type: str | None
) -> NokvoOneToolRecord:
    try:
        uid = uuid.UUID(str(record_id))
    except ValueError as exc:
        raise ValueError(f"Invalid record id: {record_id}") from exc
    clauses: list[Any] = [
        NokvoOneToolRecord.id == uid,
        NokvoOneToolRecord.organization_id == org_id,
    ]
    if record_type is not None:
        clauses.append(NokvoOneToolRecord.record_type == record_type)
    stmt = select(NokvoOneToolRecord).where(and_(*clauses))
    result = await db.execute(stmt)
    rec = result.scalars().first()
    if rec is None:
        rtype = record_type or "record"
        raise ValueError(f"{rtype} {record_id} not found in this organization")
    return rec


async def _resolve_record(
    db: AsyncSession,
    org_id: uuid.UUID,
    record_type: str,
    args: dict[str, Any],
) -> NokvoOneToolRecord:
    """Find a record by id, phone, or email (in that order of precedence)."""
    if args.get("id"):
        return await _fetch_by_id(db, org_id, args["id"], record_type)
    phone = args.get("phone") or args.get("contact_phone")
    email = args.get("email") or args.get("contact_email")
    if not phone and not email:
        raise ValueError("Provide `id`, `phone`, or `email` to identify the record")
    clauses: list[Any] = [
        NokvoOneToolRecord.organization_id == org_id,
        NokvoOneToolRecord.record_type == record_type,
    ]
    match_clauses: list[Any] = []
    if phone:
        match_clauses.append(NokvoOneToolRecord.contact_phone == phone)
    if email:
        match_clauses.append(NokvoOneToolRecord.contact_email == email)
    clauses.append(or_(*match_clauses))
    stmt = (
        select(NokvoOneToolRecord)
        .where(and_(*clauses))
        .order_by(NokvoOneToolRecord.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    rec = result.scalars().first()
    if rec is None:
        raise ValueError(f"No {record_type} found matching the provided identifiers")
    return rec


async def _recent_invocation(
    db: AsyncSession, org_id: uuid.UUID, idem_key: str
) -> AgentToolInvocation | None:
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=IDEMPOTENCY_WINDOW_SECONDS)
    stmt = (
        select(AgentToolInvocation)
        .where(
            AgentToolInvocation.organization_id == org_id,
            AgentToolInvocation.idempotency_key == idem_key,
            AgentToolInvocation.created_at >= cutoff,
            AgentToolInvocation.status == "ok",
        )
        .order_by(AgentToolInvocation.created_at.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalars().first()


async def _record_invocation(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    agent_id: uuid.UUID | None,
    session_id: str | None,
    tool_key: str,
    idempotency_key: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> None:
    result_record_id: uuid.UUID | None = None
    for key in ("id", "lead_id", "ticket_id", "callback_id", "call_log_id", "draft_id", "escalation_id"):
        if isinstance(result.get(key), str):
            try:
                result_record_id = uuid.UUID(result[key])
                break
            except ValueError:
                continue
    db.add(
        AgentToolInvocation(
            id=uuid.uuid4(),
            organization_id=organization_id,
            agent_id=agent_id,
            session_id=session_id,
            tool_key=tool_key,
            idempotency_key=idempotency_key,
            args=arguments,
            result=result,
            result_record_id=result_record_id,
            status="ok",
        )
    )
    await db.flush()


# ─────────── Backwards-compat wrappers ───────────
# Some callers (api/nokvo_one_agents.py) and the runtime still call
# `PredefinedToolsService.execute(db, org, user, key_str, args)`. Provide a
# thin adapter that resolves the key via cross-cutting + legacy aliases.

async def execute_by_key(
    db: AsyncSession,
    organization_id: uuid.UUID,
    user_id: uuid.UUID | None,
    tool_key: str,
    arguments: dict[str, Any],
    *,
    tool_lookup: Callable[[str], PredefinedTool | None] | None = None,
    agent_id: uuid.UUID | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    resolved_key = resolve_legacy_key(tool_key)
    tool: PredefinedTool | None = None
    if tool_lookup is not None:
        tool = tool_lookup(resolved_key)
    if tool is None:
        tool = get_cross_cutting_tool(resolved_key)
    if tool is None:
        raise ValueError(f"Unknown tool '{tool_key}'")
    return await PredefinedToolsService.execute(
        db,
        organization_id,
        user_id,
        tool,
        arguments,
        agent_id=agent_id,
        session_id=session_id,
    )


# Preserve the historical `validate_tool_keys` import surface — but make it
# tolerant of both cross-cutting AND resolver-produced keys. The agent API
# layer will swap to the resolver-aware validator separately.
def validate_tool_keys(keys: Iterable[str]) -> list[str]:
    resolved = [resolve_legacy_key(k) for k in keys]
    return resolved
