from __future__ import annotations

from typing import Any

from app.services.mcp_toolkit.models import ToolPlan
from app.services.mcp_toolkit.pii_policy import PIIPolicyBuilder


class SchemaGenerator:
    @staticmethod
    def input_schema(plan: ToolPlan) -> dict[str, Any]:
        properties = {}
        required = []
        for field in plan.input_fields:
            name = field.get("name")
            if not name:
                continue
            schema = {key: value for key, value in field.items() if key != "name" and value is not None}
            schema.setdefault("description", f"Input field {name} for {plan.name}.")
            if schema.get("type") == "string":
                schema.setdefault("minLength", 1)
                schema.setdefault("maxLength", 200)
            properties[name] = schema
            if field.get("required", True) is not False and name != "limit":
                required.append(name)
        if "limit" not in properties and plan.operation_type == "read":
            properties["limit"] = {"type": "integer", "minimum": 1, "maximum": 100, "default": 25, "description": "Maximum rows to return."}
        return {"type": "object", "properties": properties, "required": required, "additionalProperties": False}

    @staticmethod
    def output_schema(plan: ToolPlan) -> dict[str, Any]:
        record_properties = {}
        for field in plan.output_fields:
            name = field.get("name")
            if not name or name in {"success", "message"}:
                continue
            output_name = PIIPolicyBuilder.output_field_name(str(name), str(field.get("type") or ""))
            record_properties[output_name] = {"type": field.get("type") or "string", "nullable": True}
        if not record_properties:
            record_properties = {
                "provider_reference": {"type": "string", "nullable": True},
                "status": {"type": "string", "nullable": True},
            }
        data_schema: dict[str, Any]
        if plan.operation_type == "read":
            data_schema = {
                "type": "object",
                "properties": {
                    "records": {"type": "array", "items": {"type": "object", "properties": record_properties, "additionalProperties": False}},
                    "count": {"type": "integer"},
                },
                "required": ["records", "count"],
                "additionalProperties": False,
            }
        else:
            mutation_id_field = {
                "create": "created_id",
                "update": "updated_id",
                "delete": "deleted_id",
                "workflow": "updated_id",
            }.get(plan.operation_type)
            mutation_properties = {
                "operation": {"type": "string", "enum": [plan.operation_type]},
                "provider_reference": {"type": "string", "nullable": True},
                "affected_count": {"type": "integer", "minimum": 0},
                "idempotency_key": {"type": "string", "nullable": True},
            }
            if mutation_id_field:
                mutation_properties[mutation_id_field] = {"type": "string", "nullable": True}
            for field in plan.output_fields:
                name = field.get("name")
                if not name or name in mutation_properties:
                    continue
                mutation_properties[name] = {"type": field.get("type") or "string", "nullable": True}
            data_schema = {
                "type": "object",
                "properties": mutation_properties,
                "required": ["operation", "affected_count"],
                "additionalProperties": False,
            }
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "data": data_schema,
                "message": {"type": "string"},
                "error_code": {"type": "string", "nullable": True},
                "trace_id": {"type": "string"},
            },
            "required": ["success", "data", "message", "trace_id"],
            "additionalProperties": False,
        }
