from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.services.mcp_toolkit.context_retriever import ContextRetriever
from app.services.mcp_toolkit.embedding_retriever import EmbeddingRetriever
from app.services.mcp_toolkit.execution_generator import ExecutionGenerator
from app.services.mcp_toolkit.intent_parser import IntentParser
from app.services.mcp_toolkit.models import to_plain
from app.services.mcp_toolkit.name_utils import has_valid_operation_prefix, is_table_name_only
from app.services.mcp_toolkit.pii_policy import PIIPolicyBuilder
from app.services.mcp_toolkit.publish_gate import PublishGate
from app.services.mcp_toolkit.reviewer import Reviewer
from app.services.mcp_toolkit.schema_generator import SchemaGenerator
from app.services.mcp_toolkit.test_generator import TestGenerator
from app.services.mcp_toolkit.tool_plan_generator import ToolPlanGenerator


class MCPToolkitPipeline:
    @staticmethod
    def generate(
        nlp_prompt: str,
        integration_type: str,
        provider: str,
        context: dict[str, Any],
        model_tool: dict[str, Any] | None = None,
        existing_tool_names: set[str] | None = None,
    ) -> dict[str, Any]:
        retrieval = context.get("retrieval")
        if not isinstance(retrieval, dict) or not retrieval:
            retrieval = EmbeddingRetriever.retrieve_from_context(
                context,
                nlp_prompt,
                integration_type,
                provider,
            )
        scoped_context = {
            **context,
            **(retrieval.get("selected_context") or {}),
            "retrieval": retrieval,
            "source_context": retrieval.get("source_context") or [],
            "organization_id": (retrieval.get("scope_filter") or {}).get("organization_id") or context.get("organization_id"),
            "tenant_integration_id": (retrieval.get("scope_filter") or {}).get("tenant_integration_id") or context.get("tenant_integration_id"),
            "provider_connection_id": (retrieval.get("scope_filter") or {}).get("provider_connection_id") or context.get("provider_connection_id"),
            "selected_context_snapshot_id": (retrieval.get("scope_filter") or {}).get("selected_context_snapshot_id") or context.get("selected_context_snapshot_id"),
            "integration_type": integration_type,
            "provider": provider,
        }
        intent = IntentParser.parse(nlp_prompt, integration_type, provider, scoped_context)
        verified_resources = MCPToolkitPipeline._verified_resources(intent.target_resources, integration_type, scoped_context)
        plan = ToolPlanGenerator.generate(intent, verified_resources)
        if model_tool and isinstance(model_tool, dict):
            plan.name = MCPToolkitPipeline._prefer_model_name(plan.name, model_tool.get("name"), plan.operation_type, verified_resources)

        input_schema = SchemaGenerator.input_schema(plan)
        output_schema = SchemaGenerator.output_schema(plan)
        execution = ExecutionGenerator.generate(plan, scoped_context)
        schema_context = ContextRetriever.database_schema(scoped_context) if integration_type == "database" else {}
        pii_policy = PIIPolicyBuilder.build(integration_type, schema_context, plan.output_fields)
        tests = TestGenerator.generate(plan, input_schema)
        now = datetime.now(timezone.utc).isoformat()
        tool = {
            "name": plan.name,
            "title": plan.title,
            "description": plan.description,
            "integration_type": integration_type,
            "provider": provider,
            "mcp": {
                "server": "tenant-integration-mcp",
                "tool_name": plan.name,
                "transport": "stdio-or-http-compatible",
                "registry_scope": "organization_integration",
            },
            "input_schema": input_schema,
            "output_schema": output_schema,
            "execution": execution,
            "safety": {
                "risk_level": plan.risk_level,
                "least_privilege": True,
                "tenant_isolation": ExecutionGenerator.tenant_scope(),
                "pii_policy": pii_policy,
                "permissions": {
                    "roles_allowed": ["admin"] if plan.risk_level in {"high", "critical"} else ["admin", "manager"],
                    "required_permissions": plan.required_permissions,
                    "human_approval_required": plan.approval_policy["human_approval_required"],
                },
                "approval_policy": plan.approval_policy,
                "audit_policy": plan.audit_policy,
                "output_sanitization": {"enabled": True, "policy": "apply field-level PII policy and drop secret-like fields"},
                "pre_execution_validation": MCPToolkitPipeline._pre_execution_validation(integration_type, execution, input_schema),
                "error_handling": MCPToolkitPipeline._error_handling(),
            },
            "review": {"status": "draft", "required": True, "reviewer_role": "admin", "reason": "admin review required before publish", "required_changes": []},
            "tests": tests,
            "version": {
                "tool_id": None,
                "version": "0.1.0",
                "created_by": None,
                "reviewed_by": None,
                "approved_by": None,
                "created_at": now,
                "updated_at": now,
                "status": "draft",
                "changelog": ["initial generated draft"],
                "previous_version_id": None,
                "rollback": {"supported": True, "strategy": "disable this version and reactivate previous published version"},
            },
            "status": "draft",
            "execution_plan": MCPToolkitPipeline._execution_plan(plan, execution),
            "source_context": MCPToolkitPipeline._source_context_for_plan(scoped_context, plan.exact_resources_used),
            "missing_context": plan.missing_context,
        }
        result = {
            "tool": tool,
            "tool_plan": to_plain(plan),
            "retrieval": MCPToolkitPipeline._retrieval_report(retrieval),
            "validation": {"status": "passed", "errors": [], "warnings": []},
            "review": tool["review"],
            "tests": tests,
            "publish_gate": {"can_publish": False, "missing_requirements": []},
        }
        review_result = Reviewer.validate(result, ContextRetriever.database_schema(scoped_context) if integration_type == "database" else {"allowed_tables": verified_resources}, existing_tool_names)
        result["validation"] = review_result["validation"]
        result["review"] = review_result["review"]
        result["tool"]["review"] = review_result["review"]
        if result["review"]["status"] == "rejected":
            result["tool"]["status"] = "rejected"
            result["tool"]["version"]["status"] = "rejected"
        result["publish_gate"] = PublishGate.evaluate(result, admin_approval_exists=False)
        return PIIPolicyBuilder.redact_sensitive_values(result)

    @staticmethod
    def _verified_resources(target_resources: list[str], integration_type: str, context: dict[str, Any]) -> list[str]:
        retrieval = context.get("retrieval") or {}
        retrieved = [item.get("name") for item in retrieval.get("verified_resources") or [] if item.get("name")]
        if retrieved:
            return retrieved
        if integration_type == "database":
            allowed = set(ContextRetriever.database_schema(context).get("allowed_tables") or [])
            return [resource for resource in target_resources if resource in allowed]
        actions = ContextRetriever.connector_actions(context)
        names = {str(action.get("name") or action.get("endpoint")) for action in actions}
        matched = [resource for resource in target_resources if resource in names]
        return matched or list(names)[:1]

    @staticmethod
    def _retrieval_report(retrieval: dict[str, Any]) -> dict[str, Any]:
        return {
            "status": retrieval.get("status") or retrieval.get("retrieval_status") or "failed",
            "confidence": float(retrieval.get("confidence") or 0.0),
            "query_variants": retrieval.get("query_variants") or [],
            "scope_filter": retrieval.get("scope_filter") or {},
            "verified_resources": retrieval.get("verified_resources") or [],
            "retrieved_context_ids": retrieval.get("retrieved_context_ids") or [],
            "rejected_context_ids": retrieval.get("rejected_context_ids") or [],
            "warnings": retrieval.get("warnings") or [],
            "rejected_chunks": retrieval.get("rejected_chunks") or [],
        }

    @staticmethod
    def _source_context_for_plan(context: dict[str, Any], exact_resources: list[str]) -> list[dict[str, Any]]:
        source_context = ContextRetriever.source_refs(context)
        if not exact_resources:
            return source_context
        exact = set(exact_resources)
        return [item for item in source_context if item.get("resource") in exact]

    @staticmethod
    def _prefer_model_name(default_name: str, model_name: Any, operation_type: str, verified_resources: list[str]) -> str:
        from app.services.mcp_toolkit.name_utils import normalize_tool_name

        name = normalize_tool_name(str(model_name or default_name))
        if name in {"unsupported_tool_request", "name_generation_failed"}:
            return default_name
        if is_table_name_only(name, verified_resources):
            return default_name
        if not has_valid_operation_prefix(name, operation_type):
            return default_name
        return name

    @staticmethod
    def _pre_execution_validation(integration_type: str, execution: dict[str, Any], input_schema: dict[str, Any]) -> list[dict[str, str]]:
        rules = [
            {"code": "VALIDATE_INPUT_SCHEMA", "description": "Reject payloads that do not match input_schema."},
            {"code": "VALIDATE_TENANT_SCOPE", "description": "Require organization_id, tenant integration id, provider connection id, and selected context snapshot."},
            {"code": "VALIDATE_SECRET_REFERENCE", "description": "Use only current tenant stored secret reference; reject raw credentials."},
            {"code": "VALIDATE_PERMISSION", "description": "Check role-based permissions and approval policy before execution."},
        ]
        if integration_type == "database":
            rules.extend(
                [
                    {"code": "VALIDATE_SQL_FIXED_PURPOSE", "description": "Reject dynamic SQL, SELECT *, placeholders, and unverified tables/columns."},
                    {"code": "VALIDATE_SQL_PARAMETERS_BOUND", "description": "Require named bound parameters for all user inputs."},
                    {"code": "VALIDATE_SQL_EXPLAIN", "description": "Run EXPLAIN validation before publish and in test-run mode."},
                ]
            )
        return rules

    @staticmethod
    def _error_handling() -> dict[str, Any]:
        return {
            "format": {"success": False, "data": None, "message": "Safe error message.", "error_code": "ERROR_CODE", "trace_id": "trace id"},
            "codes": [
                "VALIDATION_ERROR",
                "PERMISSION_DENIED",
                "APPROVAL_REQUIRED",
                "TENANT_SCOPE_VIOLATION",
                "SECRET_NOT_FOUND",
                "NO_MATCH",
                "TOO_MANY_MATCHES",
                "PROVIDER_RATE_LIMITED",
                "TIMEOUT",
                "INTEGRATION_ERROR",
                "POLICY_VIOLATION",
            ],
        }

    @staticmethod
    def _execution_plan(plan: Any, execution: dict[str, Any]) -> list[str]:
        if plan.integration_type == "database":
            return [
                "Validate input schema, tenant scope, role permissions, and approval policy.",
                "Validate fixed SQL against allowed_tables, allowed_columns, blocked statements, and bound parameters.",
                "Run EXPLAIN/test-run validation before publish.",
                "Execute against the current tenant stored database secret only.",
                "Apply field-level PII redaction and return structured output with trace_id.",
                "Write audit log with organization_id, tenant_id, provider_connection_id, tool_name, version, actor_id, and trace_id.",
            ]
        return [
            "Validate input schema, tenant scope, role permissions, and approval policy.",
            "Resolve verified connector action/template for the selected provider.",
            "Call provider using tenant secret reference only with timeout, retry, and rate-limit policy.",
            "Apply PII/secret output redaction and return structured output with trace_id.",
            "Write audit log with organization_id, tenant_id, provider_connection_id, tool_name, version, actor_id, and trace_id.",
        ]
