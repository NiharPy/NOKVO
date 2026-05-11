"""
Nokvo One predefined-tool catalog and dispatcher.

V1 ships a tightly scoped set of safe, controlled tools:
  - lead_tracker_create_lead
  - lead_tracker_update_status
  - lead_tracker_add_note
  - call_logger_create_entry
  - call_logger_get_history
  - create_ticket
  - schedule_callback
  - send_email_draft

Hard rules for V1:
  - No direct database-write tools beyond the schema we own (NokvoOneToolRecord).
  - No web_search.
  - No refund / payment / order-modification tools.
  - send_email_draft NEVER sends external email — it creates a queued draft requiring
    explicit human confirmation via the portal.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.nokvo_one_tool_record import NokvoOneToolRecord


JSONSchema = dict[str, Any]
ToolHandler = Callable[..., Awaitable[dict[str, Any]]]


@dataclass(frozen=True)
class PredefinedTool:
    key: str
    display_name: str
    description: str
    input_schema: JSONSchema
    record_type: str | None
    handler_name: str
    requires_confirmation: bool = False


CATALOG: tuple[PredefinedTool, ...] = (
    PredefinedTool(
        key="lead_tracker_create_lead",
        display_name="Create lead",
        description="Create a new lead in the Nokvo One lead tracker.",
        record_type="lead",
        handler_name="lead_tracker_create_lead",
        input_schema={
            "type": "object",
            "required": ["full_name"],
            "properties": {
                "full_name": {"type": "string", "minLength": 1, "maxLength": 200},
                "email": {"type": "string", "format": "email"},
                "phone": {"type": "string", "maxLength": 32},
                "company": {"type": "string", "maxLength": 200},
                "source": {"type": "string", "maxLength": 80},
                "notes": {"type": "string", "maxLength": 2000},
            },
            "additionalProperties": False,
        },
    ),
    PredefinedTool(
        key="lead_tracker_update_status",
        display_name="Update lead status",
        description="Change the status of an existing lead.",
        record_type="lead",
        handler_name="lead_tracker_update_status",
        input_schema={
            "type": "object",
            "required": ["lead_id", "status"],
            "properties": {
                "lead_id": {"type": "string", "format": "uuid"},
                "status": {
                    "type": "string",
                    "enum": ["new", "contacted", "qualified", "converted", "lost"],
                },
            },
            "additionalProperties": False,
        },
    ),
    PredefinedTool(
        key="lead_tracker_add_note",
        display_name="Add lead note",
        description="Append a note to a lead's history.",
        record_type="lead",
        handler_name="lead_tracker_add_note",
        input_schema={
            "type": "object",
            "required": ["lead_id", "note"],
            "properties": {
                "lead_id": {"type": "string", "format": "uuid"},
                "note": {"type": "string", "minLength": 1, "maxLength": 2000},
            },
            "additionalProperties": False,
        },
    ),
    PredefinedTool(
        key="call_logger_create_entry",
        display_name="Log call",
        description="Record an interaction with a customer (call, chat, email).",
        record_type="call_log",
        handler_name="call_logger_create_entry",
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
        key="call_logger_get_history",
        display_name="Get call history",
        description="Retrieve recent call/interaction history for the organization.",
        record_type="call_log",
        handler_name="call_logger_get_history",
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
        key="create_ticket",
        display_name="Create support ticket",
        description="Open a support ticket in the Nokvo One ticket inbox.",
        record_type="ticket",
        handler_name="create_ticket",
        input_schema={
            "type": "object",
            "required": ["subject", "description"],
            "properties": {
                "subject": {"type": "string", "minLength": 1, "maxLength": 200},
                "description": {"type": "string", "minLength": 1, "maxLength": 4000},
                "priority": {"type": "string", "enum": ["low", "normal", "high", "urgent"], "default": "normal"},
                "contact_email": {"type": "string", "format": "email"},
                "contact_name": {"type": "string", "maxLength": 200},
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
)


_CATALOG_INDEX: dict[str, PredefinedTool] = {tool.key: tool for tool in CATALOG}


def list_tools() -> list[dict[str, Any]]:
    return [
        {
            "key": tool.key,
            "display_name": tool.display_name,
            "description": tool.description,
            "input_schema": tool.input_schema,
            "requires_confirmation": tool.requires_confirmation,
        }
        for tool in CATALOG
    ]


def get_tool(key: str) -> PredefinedTool | None:
    return _CATALOG_INDEX.get(key)


def validate_tool_keys(keys: Iterable[str]) -> list[str]:
    invalid = [k for k in keys if k not in _CATALOG_INDEX]
    if invalid:
        raise ValueError(f"Unknown predefined tool keys: {invalid}")
    return list(keys)


class PredefinedToolsService:
    """Async dispatcher for Nokvo One predefined tool calls.

    All tool side-effects route through this service so the agent runtime
    never bypasses the safety envelope (no external sends, schema validation
    handled here in the future, etc.).
    """

    @staticmethod
    async def execute(
        db: AsyncSession,
        organization_id: uuid.UUID,
        user_id: uuid.UUID | None,
        tool_key: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        tool = get_tool(tool_key)
        if tool is None:
            raise ValueError(f"Unknown tool '{tool_key}'")
        handler = getattr(PredefinedToolsService, f"_handle_{tool.handler_name}")
        return await handler(db, organization_id, user_id, arguments)

    @staticmethod
    async def _handle_lead_tracker_create_lead(db, org_id, user_id, args):
        rec = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="lead",
            status="new",
            data={
                "full_name": args["full_name"],
                "email": args.get("email"),
                "phone": args.get("phone"),
                "company": args.get("company"),
                "source": args.get("source"),
                "notes_history": [args["notes"]] if args.get("notes") else [],
            },
        )
        db.add(rec)
        await db.flush()
        return {"ok": True, "lead_id": str(rec.id), "status": rec.status}

    @staticmethod
    async def _handle_lead_tracker_update_status(db, org_id, user_id, args):
        rec = await PredefinedToolsService._fetch_record(db, org_id, args["lead_id"], "lead")
        rec.status = args["status"]
        await db.flush()
        return {"ok": True, "lead_id": str(rec.id), "status": rec.status}

    @staticmethod
    async def _handle_lead_tracker_add_note(db, org_id, user_id, args):
        rec = await PredefinedToolsService._fetch_record(db, org_id, args["lead_id"], "lead")
        data = dict(rec.data or {})
        history = list(data.get("notes_history") or [])
        history.append(args["note"])
        data["notes_history"] = history
        rec.data = data
        await db.flush()
        return {"ok": True, "lead_id": str(rec.id), "note_count": len(history)}

    @staticmethod
    async def _handle_call_logger_create_entry(db, org_id, user_id, args):
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
        )
        db.add(rec)
        await db.flush()
        return {"ok": True, "call_log_id": str(rec.id)}

    @staticmethod
    async def _handle_call_logger_get_history(db, org_id, user_id, args):
        limit = int(args.get("limit") or 10)
        stmt = (
            select(NokvoOneToolRecord)
            .where(
                NokvoOneToolRecord.organization_id == org_id,
                NokvoOneToolRecord.record_type == "call_log",
            )
            .order_by(NokvoOneToolRecord.created_at.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = result.scalars().all()
        if args.get("contact_email"):
            rows = [r for r in rows if (r.data or {}).get("contact_email") == args["contact_email"]]
        if args.get("contact_phone"):
            rows = [r for r in rows if (r.data or {}).get("contact_phone") == args["contact_phone"]]
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
    async def _handle_create_ticket(db, org_id, user_id, args):
        rec = NokvoOneToolRecord(
            id=uuid.uuid4(),
            organization_id=org_id,
            created_by_user_id=user_id,
            record_type="ticket",
            status="open",
            data={
                "subject": args["subject"],
                "description": args["description"],
                "priority": args.get("priority", "normal"),
                "contact_email": args.get("contact_email"),
                "contact_name": args.get("contact_name"),
            },
        )
        db.add(rec)
        await db.flush()
        return {"ok": True, "ticket_id": str(rec.id), "status": rec.status}

    @staticmethod
    async def _handle_schedule_callback(db, org_id, user_id, args):
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
        )
        db.add(rec)
        await db.flush()
        return {"ok": True, "callback_id": str(rec.id), "scheduled_for": args["callback_at"]}

    @staticmethod
    async def _handle_send_email_draft(db, org_id, user_id, args):
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
    async def _fetch_record(db, org_id, record_id, expected_type) -> NokvoOneToolRecord:
        try:
            uid = uuid.UUID(str(record_id))
        except ValueError as exc:
            raise ValueError(f"Invalid record id: {record_id}") from exc
        stmt = select(NokvoOneToolRecord).where(
            NokvoOneToolRecord.id == uid,
            NokvoOneToolRecord.organization_id == org_id,
            NokvoOneToolRecord.record_type == expected_type,
        )
        result = await db.execute(stmt)
        rec = result.scalars().first()
        if rec is None:
            raise ValueError(f"{expected_type} {record_id} not found in this organization")
        return rec
