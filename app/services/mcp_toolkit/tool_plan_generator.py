from __future__ import annotations

import re
from typing import Any

from app.services.mcp_toolkit.models import ToolIntent, ToolPlan
from app.services.mcp_toolkit.name_utils import humanize_tool_name, normalize_tool_name
from app.services.mcp_toolkit.pii_policy import PIIPolicyBuilder
from app.services.mcp_toolkit.safety_classifier import SafetyClassifier


class ToolPlanGenerator:
    @staticmethod
    def generate(intent: ToolIntent, verified_resources: list[dict[str, Any]]) -> ToolPlan:
        action_text = SafetyClassifier.action_text(intent.purpose)
        trusted_resources = [item for item in verified_resources if item.get("name") in set(intent.target_resources)] or verified_resources
        resource_names = [str(item.get("name") or "") for item in trusted_resources if item.get("name")]
        name = ToolPlanGenerator._name_for_intent(intent, resource_names, action_text)
        if intent.operation_type not in {"read", "workflow"} and not name.startswith(("create_", "update_", "delete_", "cancel_", "refund_")):
            name = f"{intent.operation_type}_{name}"
        title = humanize_tool_name(name)
        approval_policy = {
            "required": True,
            "human_approval_required": intent.human_approval_needed,
            "minimum_reviewer_role": "admin" if intent.risk_level in {"high", "critical"} else "manager",
            "reason": f"{intent.risk_level} risk {intent.operation_type} operation" if intent.human_approval_needed else "admin review required before publish",
        }
        return ToolPlan(
            name=name,
            title=title,
            description=ToolPlanGenerator._description_for_intent(intent, title, resource_names),
            integration_type=intent.integration_type,
            provider=intent.provider,
            operation_type=intent.operation_type,
            risk_level=intent.risk_level,
            required_permissions=ToolPlanGenerator._permissions(intent),
            exact_resources_used=resource_names,
            input_fields=intent.required_inputs,
            output_fields=ToolPlanGenerator._output_fields_for_operation(intent.operation_type, intent.expected_outputs, resource_names, action_text, intent.target_field),
            preconditions=intent.business_preconditions,
            postconditions=["return sanitized structured response", "write audit log with organization_id and trace_id"],
            error_cases=[
                {"code": "VALIDATION_ERROR", "message": "Input does not match schema or preconditions."},
                {"code": "PERMISSION_DENIED", "message": "Caller lacks role or approval."},
                {"code": "TENANT_SCOPE_VIOLATION", "message": "Requested resource is outside tenant integration scope."},
                {"code": "INTEGRATION_ERROR", "message": "Provider returned a safe, sanitized error."},
                {"code": "TIMEOUT", "message": "Execution exceeded configured timeout."},
            ],
            approval_policy=approval_policy,
            audit_policy={
                "enabled": True,
                "events": ["tool_requested", "tool_validated", "tool_executed", "tool_failed"],
                "include": ["organization_id", "tenant_id", "tenant_integration_id", "provider_connection_id", "context_snapshot_id", "tool_name", "version", "trace_id", "actor_id", "actor_role"],
                "exclude": ["raw_credentials", "tokens", "secrets", "passwords", "connection_strings"],
            },
            workflow_type=intent.workflow_type,
            target_field=intent.target_field,
            resolved_target_column=intent.resolved_target_column,
            trusted_resources_used=resource_names,
            intent_signals=intent.intent_signals,
            trusted_resources=trusted_resources,
            missing_context=intent.missing_context,
        )

    @staticmethod
    def _permissions(intent: ToolIntent) -> list[str]:
        base = [f"{intent.integration_type}:{intent.provider}:tool:execute"]
        if intent.operation_type == "read":
            return base + [f"{intent.integration_type}:{intent.provider}:read"]
        return base + [f"{intent.integration_type}:{intent.provider}:{intent.operation_type}", "approval:request"]

    @staticmethod
    def _output_fields_for_operation(
        operation_type: str,
        output_fields: list[dict[str, Any]],
        verified_resources: list[str],
        action_text: str = "",
        target_field: str | None = None,
    ) -> list[dict[str, Any]]:
        if operation_type in {"update", "workflow"} and target_field == "username":
            fields = [
                {"name": "affected_count", "type": "integer"},
                {"name": "idempotency_key", "type": "string"},
            ]
            for field in output_fields:
                name = str(field.get("name") or "")
                if name in {"customer_id", "customer_code", "updated_username", "username", "user_name", "phone"}:
                    transformed = dict(field)
                    transformed["raw_name"] = field.get("raw_name") or field.get("name")
                    transformed["name"] = "updated_username" if name in {"username", "user_name"} else PIIPolicyBuilder.output_field_name(name, str(field.get("type") or ""))
                    fields.append(transformed)
            return fields
        if operation_type != "read":
            id_field = {
                "create": "created_id",
                "update": "updated_id",
                "delete": "deleted_id",
            }.get(operation_type, "provider_reference")
            return [
                {"name": "affected_count", "type": "integer"},
                {"name": "idempotency_key", "type": "string"},
                {"name": id_field, "type": "string"},
            ]
        if not verified_resources:
            return output_fields
        primary = verified_resources[0]
        filtered = []
        for field in output_fields:
            if field.get("source") and not str(field.get("source")).startswith(f"{primary}."):
                continue
            transformed = dict(field)
            transformed["raw_name"] = field.get("name")
            transformed["name"] = PIIPolicyBuilder.output_field_name(str(field.get("name") or ""), str(field.get("type") or ""))
            filtered.append(transformed)
        if len(verified_resources) > 1:
            for resource in verified_resources[1:]:
                related_fields = ToolPlanGenerator._related_read_fields(action_text, output_fields, resource)
                if related_fields:
                    filtered.extend(related_fields)
                else:
                    filtered.append({"name": f"{resource.split('.')[-1]}_count", "type": "integer", "source": resource})
        return filtered or output_fields[:3]

    @staticmethod
    def _related_read_fields(action_text: str, output_fields: list[dict[str, Any]], resource: str) -> list[dict[str, Any]]:
        text = action_text.lower()
        table = resource.split(".")[-1].lower()
        if "order" not in table or "status" not in text:
            return []
        wanted = ("order_id", "order_status", "payment_status", "shipment_partner", "shipment_status", "created_at", "delivered_at", "status")
        selected = []
        for field in output_fields:
            if not str(field.get("source") or "").startswith(f"{resource}."):
                continue
            name = str(field.get("name") or "")
            if name in wanted or any(token in name.lower() for token in ("status", "shipment", "payment")):
                transformed = dict(field)
                transformed["raw_name"] = name
                transformed["name"] = PIIPolicyBuilder.output_field_name(name, str(field.get("type") or ""))
                selected.append(transformed)
        return selected[:8]

    @staticmethod
    def _name_for_intent(intent: ToolIntent, verified_resources: list[str], action_text: str) -> str:
        text = action_text.lower()
        if intent.operation_type in {"workflow", "update"} and re.search(r"\bcustomer\b", text) and re.search(r"\bphone\b", text) and re.search(r"\buser\s*name\b|\busername\b", text):
            return "update_customer_username_by_phone"
        if intent.operation_type == "read" and re.search(r"\bcustomer\b", text) and re.search(r"\bphone\b", text):
            if re.search(r"\b(call|calls|call\s+history|history)\b", text):
                return "lookup_customer_call_history_by_phone"
            if re.search(r"\b(support|ticket|call|case)\b", text):
                return "lookup_customer_support_summary_by_phone"
            if re.search(r"\b(detail|details|record|records|related|summary)\b", text):
                return "lookup_customer_details_by_phone"
            return "lookup_customer_by_phone"
        if intent.operation_type == "read" and re.search(r"\border\b", text) and re.search(r"\bstatus\b", text):
            if re.search(r"\bphone\b", text):
                return "check_latest_order_status_by_phone" if re.search(r"\blatest\b", text) else "check_order_status_by_phone"
            return "check_order_status"
        if intent.operation_type == "create" and re.search(r"\bsupport\s+ticket\b|\bticket\b", text):
            return "create_support_ticket_for_customer" if re.search(r"\bcustomer\b", text) else "create_support_ticket"
        resource = ToolPlanGenerator._resource_name(verified_resources[0]) if verified_resources else ""
        if intent.operation_type == "read":
            verb = ToolPlanGenerator._read_verb(text)
            key = ToolPlanGenerator._lookup_key(intent)
            if not resource or not key:
                return "name_generation_failed"
            seed = f"{verb}_{resource}_by_{key}"
        else:
            entity = resource or ToolPlanGenerator._entity_from_text(text)
            if not entity:
                return "name_generation_failed"
            if intent.operation_type == "create":
                seed = f"create_{entity}"
                if "customer" in text and "customer" not in entity:
                    seed = f"{seed}_for_customer"
            elif intent.operation_type == "update":
                key = ToolPlanGenerator._lookup_key(intent)
                seed = f"update_{entity}_by_{key}" if key else f"update_{entity}"
            elif intent.operation_type == "delete":
                key = ToolPlanGenerator._lookup_key(intent)
                seed = f"delete_{entity}_by_{key}" if key else f"delete_{entity}"
            else:
                seed = f"{intent.operation_type}_{entity}"
        return normalize_tool_name(seed)

    @staticmethod
    def _resource_name(resource: str) -> str:
        table = resource.split(".")[-1]
        return table.rstrip("s") if table.endswith("s") else table

    @staticmethod
    def _read_verb(text: str) -> str:
        if re.search(r"\b(check|view)\b", text):
            return "check"
        if re.search(r"\blist\b", text):
            return "list"
        if re.search(r"\bsearch\b", text):
            return "search"
        if re.search(r"\bretrieve\b", text):
            return "retrieve"
        if re.search(r"\bget\b", text):
            return "get"
        return "lookup"

    @staticmethod
    def _lookup_key(intent: ToolIntent) -> str:
        field_names = [str(field.get("name") or "") for field in intent.required_inputs]
        if "phone_number" in field_names:
            return "phone"
        if "email" in field_names:
            return "email"
        if "order_id" in field_names:
            return "order_id"
        if "ticket_id" in field_names:
            return "ticket_id"
        for field in field_names:
            if field and field != "limit":
                return field
        return ""

    @staticmethod
    def _entity_from_text(text: str) -> str:
        if re.search(r"\bsupport\s+ticket\b|\bticket\b", text):
            return "support_ticket"
        if re.search(r"\border\b", text):
            return "order"
        if re.search(r"\bcustomer\b", text):
            return "customer"
        return ""

    @staticmethod
    def _description_for_intent(intent: ToolIntent, title: str, verified_resources: list[str]) -> str:
        resources = ", ".join(verified_resources) if verified_resources else "verified selected integration resources"
        if intent.operation_type == "read":
            key = ToolPlanGenerator._lookup_key(intent)
            suffix = f" by {key.replace('_', ' ')}" if key else ""
            return f"{title}: read-only lookup{suffix} using {resources}."
        if intent.operation_type == "workflow":
            return f"{title}: read-then-update workflow using {resources} with explicit human approval and audit controls."
        if intent.operation_type == "create":
            return f"{title}: create a new record using {resources} with reviewed inputs and approval controls."
        if intent.operation_type == "update":
            return f"{title}: update an existing record using {resources} with strict preconditions and approval controls."
        if intent.operation_type == "delete":
            return f"{title}: delete or cancel a scoped record using {resources} with explicit approval controls."
        return f"{title}: execute a scoped {intent.integration_type}/{intent.provider} operation using {resources}."
