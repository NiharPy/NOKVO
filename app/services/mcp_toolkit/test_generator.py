from __future__ import annotations

from typing import Any

from app.services.mcp_toolkit.models import ToolPlan


class TestGenerator:
    @staticmethod
    def generate(plan: ToolPlan, input_schema: dict[str, Any]) -> dict[str, Any]:
        sample = TestGenerator._sample_input(input_schema)
        tests = [
            {"name": "schema_validation", "type": "schema", "input": sample, "expected": "input_schema accepts valid payload"},
            {"name": "missing_required_input", "type": "validation", "input": {}, "expected_error": "VALIDATION_ERROR"},
            {"name": "invalid_input_format", "type": "validation", "input": TestGenerator._invalid_input(input_schema), "expected_error": "VALIDATION_ERROR"},
            {"name": "happy_path", "type": "execution", "input": sample, "expected": "sanitized success or no-result response"},
            {"name": "no_result", "type": "execution", "input": sample, "expected": "success false or empty records with NO_MATCH message"},
            {"name": "sql_or_api_validation", "type": "static", "expected": "fixed SQL/API mapping validates against verified context"},
            {"name": "pii_redaction", "type": "security", "expected": "email, phone, address, and secret-like fields are masked or dropped"},
            {"name": "tenant_isolation", "type": "security", "expected_error": "TENANT_SCOPE_VIOLATION when scope does not match organization integration"},
            {"name": "permission", "type": "security", "expected_error": "PERMISSION_DENIED for unauthorized role"},
            {"name": "approval_required", "type": "approval", "expected": "human approval required" if plan.approval_policy.get("human_approval_required") else "admin publish approval required"},
            {"name": "timeout_error_handling", "type": "resilience", "expected_error": "TIMEOUT with safe structured error"},
            {"name": "audit_event_emitted", "type": "audit", "expected": "audit event contains organization_id, tenant_integration_id, provider_connection_id, tool_name, version, actor_id, and trace_id"},
        ]
        if plan.workflow_type:
            tests.extend(
                [
                    {"name": "workflow_step_order", "type": "workflow", "expected": "workflow executes all steps in declared order"},
                    {"name": "workflow_partial_failure", "type": "workflow", "expected": "partial failure triggers compensation or safe rollback guidance"},
                    {"name": "workflow_idempotency", "type": "workflow", "expected": "idempotency key required for workflow mutation"},
                ]
            )
        return {"test_run_required": True, "generated_tests": tests, "test_run_results": [], "status": "not_run"}

    @staticmethod
    def _sample_input(input_schema: dict[str, Any]) -> dict[str, Any]:
        sample = {}
        for name, schema in (input_schema.get("properties") or {}).items():
            if schema.get("type") == "integer":
                sample[name] = schema.get("default") or schema.get("minimum") or 1
            elif schema.get("format") == "email":
                sample[name] = "user@example.com"
            elif "phone" in name:
                sample[name] = "+919999999999"
            else:
                sample[name] = f"sample_{name}"
        return sample

    @staticmethod
    def _invalid_input(input_schema: dict[str, Any]) -> dict[str, Any]:
        invalid = {}
        for name, schema in (input_schema.get("properties") or {}).items():
            invalid[name] = -1 if schema.get("type") == "integer" else ""
        return invalid
