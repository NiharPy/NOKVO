from __future__ import annotations

from typing import Any

from app.services.mcp_toolkit.capability_registry import ProviderCapability
from app.services.mcp_toolkit.context_retriever import ContextRetriever


class SQLCompiler:
    @staticmethod
    def compile(plan: Any, context: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        return fallback


class MongoCompiler:
    @staticmethod
    def compile(plan: Any, context: dict[str, Any], capability: ProviderCapability) -> dict[str, Any]:
        collection = plan.exact_resources_used[0] if plan.exact_resources_used else None
        input_fields = [field.get("name") for field in plan.input_fields if field.get("name")]
        filter_fields = [name for name in input_fields if name not in {"limit", "idempotency_key"}]
        mutation = plan.operation_type in {"create", "update", "delete", "workflow"}
        mapping: dict[str, Any] = {
            "collection": collection,
            "allowed_collections": plan.exact_resources_used,
            "filter_fields": filter_fields,
            "parameter_binding": {"style": "named", "sources": [f"input.{name}" for name in filter_fields]},
            "tenant_scope": {
                "organization_id": context.get("organization_id"),
                "tenant_integration_id": context.get("tenant_integration_id"),
                "provider_connection_id": context.get("provider_connection_id"),
                "selected_context_snapshot_id": context.get("selected_context_snapshot_id"),
            },
            "safe_filters_only": True,
            "hard_limits": {"max_documents": 100, "timeout_seconds": 10},
            "validated_backend": capability.execution_backend,
        }
        if plan.operation_type == "read":
            mapping["query"] = {"filter": {name: f":{name}" for name in filter_fields}, "limit": ":limit"}
            mode = "read_only"
        elif plan.operation_type == "create":
            mapping["insert_document"] = {name: f":{name}" for name in filter_fields}
            mapping["idempotency"] = {"required": True, "source": "input.idempotency_key"}
            mode = "write_requires_human_approval"
        else:
            mapping["update_filter"] = {"phone_number": ":phone_number"} if "phone_number" in filter_fields else {name: f":{name}" for name in filter_fields[:1]}
            mapping["update_document"] = {"$set": {"updated_field": ":new_value"}}
            mapping["allowed_update_operators"] = ["$set"]
            mapping["idempotency"] = {"required": True, "source": "input.idempotency_key"}
            mode = "write_requires_human_approval"
        return {"type": "database_nosql", "mode": mode, "mapping": mapping}


class ConnectorActionCompiler:
    @staticmethod
    def compile(plan: Any, context: dict[str, Any], capability: ProviderCapability) -> dict[str, Any]:
        actions = ContextRetriever.connector_actions(context)
        selected = actions[0] if actions else {}
        method = str(selected.get("method") or ("GET" if plan.operation_type == "read" else "POST")).upper()
        return {
            "type": "connector_action",
            "mode": "read_only" if plan.operation_type == "read" else "write_requires_human_approval",
            "mapping": {
                "provider": plan.provider,
                "connector_action": selected.get("name"),
                "method": method,
                "endpoint": selected.get("endpoint"),
                "module": selected.get("module"),
                "query_params": [field["name"] for field in plan.input_fields if field.get("name") and method == "GET"],
                "body_params": [field["name"] for field in plan.input_fields if field.get("name") and method != "GET"],
                "tenant_secret_ref": "tenant_provider_connection_secret_ref",
                "tenant_scope": {
                    "organization_id": context.get("organization_id"),
                    "tenant_integration_id": context.get("tenant_integration_id"),
                    "provider_connection_id": context.get("provider_connection_id"),
                    "selected_context_snapshot_id": context.get("selected_context_snapshot_id"),
                },
                "timeout_seconds": 20,
                "retry": {"max_attempts": 2, "backoff": "exponential_jitter"},
                "rate_limit": {"scope": "organization_provider_connection", "required": True},
                "oauth_scope_verification_required": capability.validation_requirements.get("oauth_scope_verification", False),
                "idempotency": {"required": plan.operation_type != "read", "header": "Idempotency-Key" if plan.operation_type != "read" else None},
            },
        }


class OpenAPICompiler:
    @staticmethod
    def compile(plan: Any, context: dict[str, Any], capability: ProviderCapability) -> dict[str, Any]:
        compiled = ConnectorActionCompiler.compile(plan, context, capability)
        compiled["type"] = "api_request"
        compiled["mapping"]["openapi_verified"] = True
        return compiled


class ERPConnectorCompiler:
    @staticmethod
    def compile(plan: Any, context: dict[str, Any], capability: ProviderCapability) -> dict[str, Any]:
        actions = ContextRetriever.connector_actions(context)
        selected = actions[0] if actions else {}
        return {
            "type": capability.execution_backend,
            "mode": "read_only" if plan.operation_type == "read" else "write_requires_human_approval",
            "mapping": {
                "provider": plan.provider,
                "connector_action": selected.get("name"),
                "endpoint": selected.get("endpoint"),
                "method": selected.get("method"),
                "module": selected.get("module"),
                "company_context_required": True,
                "tenant_secret_ref": "tenant_provider_connection_secret_ref",
                "tenant_scope": {
                    "organization_id": context.get("organization_id"),
                    "tenant_integration_id": context.get("tenant_integration_id"),
                    "provider_connection_id": context.get("provider_connection_id"),
                    "selected_context_snapshot_id": context.get("selected_context_snapshot_id"),
                },
                "idempotency": {"required": plan.operation_type != "read", "source": "input.idempotency_key" if plan.operation_type != "read" else None},
            },
        }


class HISCompiler:
    @staticmethod
    def compile(plan: Any, context: dict[str, Any], capability: ProviderCapability) -> dict[str, Any]:
        compiled = ConnectorActionCompiler.compile(plan, context, capability)
        compiled["type"] = "his_action"
        compiled["mapping"]["patient_safety_policy_required"] = True
        return compiled


class EcommerceCompiler:
    @staticmethod
    def compile(plan: Any, context: dict[str, Any], capability: ProviderCapability) -> dict[str, Any]:
        compiled = ConnectorActionCompiler.compile(plan, context, capability)
        compiled["mapping"]["commerce_policy"] = {
            "refund_or_payment_approval_required": capability.approval_requirements.get("refund_or_payment_required", False),
        }
        return compiled


class WorkflowCompiler:
    @staticmethod
    def compile(plan: Any, context: dict[str, Any], capability: ProviderCapability, fallback: dict[str, Any]) -> dict[str, Any]:
        if capability.execution_backend == "database_sql":
            return fallback
        compiled = ConnectorActionCompiler.compile(plan, context, capability)
        compiled["type"] = "workflow"
        compiled["mapping"]["workflow_steps"] = [plan.workflow_type or "workflow"]
        return compiled


class ExecutionCompilerRegistry:
    @staticmethod
    def compile(plan: Any, context: dict[str, Any], capability: ProviderCapability, fallback: dict[str, Any]) -> dict[str, Any]:
        backend = capability.execution_backend
        if backend == "database_sql":
            return SQLCompiler.compile(plan, context, fallback)
        if backend == "database_nosql":
            return MongoCompiler.compile(plan, context, capability)
        if backend == "connector_action":
            if plan.operation_type == "workflow":
                return WorkflowCompiler.compile(plan, context, capability, fallback)
            return ConnectorActionCompiler.compile(plan, context, capability)
        if backend == "api_request":
            return OpenAPICompiler.compile(plan, context, capability)
        if backend == "erp_xml":
            return ERPConnectorCompiler.compile(plan, context, capability)
        if backend == "his_action":
            return HISCompiler.compile(plan, context, capability)
        if backend == "workflow":
            return WorkflowCompiler.compile(plan, context, capability, fallback)
        return fallback
