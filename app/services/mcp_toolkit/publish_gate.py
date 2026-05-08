from __future__ import annotations

from typing import Any


class PublishGate:
    @staticmethod
    def evaluate(result: dict[str, Any], admin_approval_exists: bool = False) -> dict[str, Any]:
        missing = []
        tool = result.get("tool") or {}
        retrieval = result.get("retrieval") or {}
        validation = result.get("validation") or {}
        validation_checks = validation.get("checks") or {}
        retrieval_warnings = [str(item) for item in retrieval.get("warnings") or []]
        retrieval_errors = [item for item in retrieval.get("errors") or [] if isinstance(item, dict)]
        source_context = tool.get("source_context") or []
        verified_resources = {
            item.get("name")
            for item in retrieval.get("verified_resources") or []
            if isinstance(item, dict) and item.get("name")
        }
        exact_resources = set((result.get("tool_plan") or {}).get("exact_resources_used") or [])

        if result.get("review", {}).get("status") != "approved":
            missing.append("review.status must be approved")
        if validation.get("status") != "passed":
            missing.append("validation.status must be passed")
        if tool.get("execution", {}).get("type") == "database_sql":
            if validation_checks.get("sql_parser") != "passed":
                missing.append("SQL parser validation passed is required")
            if validation_checks.get("sql_explain") != "passed":
                missing.append("PostgreSQL EXPLAIN validation passed is required")
        if result.get("tests", {}).get("status") != "passed":
            missing.append("tests.status must be passed")
        if not admin_approval_exists:
            missing.append("admin approval record is required")
        if retrieval.get("status") != "passed":
            missing.append("retrieval.status must be passed")
        if float(retrieval.get("confidence") or 0.0) < 0.80:
            missing.append("retrieval.confidence must be >= 0.80")
        if retrieval.get("fallback_used") and not retrieval.get("fallback_admin_approved"):
            missing.append("no fallback context unless admin explicitly approves")
        for blocker in retrieval.get("publish_blockers") or []:
            if blocker == "QDRANT_INDEX_MISSING":
                missing.append("qdrant payload indexes must exist for scoped retrieval filters")
            elif blocker == "RETRIEVAL_BACKEND_ERROR":
                missing.append("no unresolved retrieval backend errors")
            elif blocker == "FALLBACK_RETRIEVAL_USED" and not retrieval.get("fallback_admin_approved"):
                missing.append("no fallback context unless admin explicitly approves")
        scope_filter = retrieval.get("scope_filter") or {}
        for key in ("organization_id", "tenant_integration_id", "provider_connection_id", "selected_context_snapshot_id", "integration_type", "provider"):
            if not scope_filter.get(key):
                missing.append(f"retrieval scope missing {key}")
        if not source_context or not all(isinstance(item, dict) for item in source_context):
            missing.append("source_context must be structured")
        snapshot_id = scope_filter.get("selected_context_snapshot_id")
        if snapshot_id and any(item.get("context_snapshot_id") != snapshot_id for item in source_context if isinstance(item, dict)):
            missing.append("all source_context entries must come from selected context snapshot")
        if source_context and any(item.get("provider") != scope_filter.get("provider") for item in source_context if isinstance(item, dict)):
            missing.append("all source_context entries must match selected provider")
        if source_context and any(item.get("integration_type") != scope_filter.get("integration_type") for item in source_context if isinstance(item, dict)):
            missing.append("all source_context entries must match selected integration type")
        if exact_resources and not exact_resources.issubset(verified_resources):
            missing.append("every generated resource must be present in retrieval.verified_resources")
        rejected = retrieval.get("rejected_chunks") or []
        if any(item.get("reason") in {"TENANT_SCOPE_MISMATCH", "CONTEXT_SNAPSHOT_MISMATCH", "PROVIDER_MISMATCH"} and float(item.get("retrieval_score") or 0.0) >= 0.6 for item in rejected if isinstance(item, dict)):
            missing.append("no high-relevance rejected chunks due to scope mismatch")
        if any(item.get("reason") == "DEPRECATED_CONTEXT" for item in rejected if isinstance(item, dict)):
            missing.append("no stale/deprecated context")
        if any(item.get("reason") in {"QDRANT_INDEX_MISSING", "RETRIEVAL_BACKEND_ERROR"} for item in rejected if isinstance(item, dict)):
            missing.append("no unresolved retrieval backend errors")
        if any(item.get("code") in {"QDRANT_INDEX_MISSING", "RETRIEVAL_BACKEND_ERROR"} for item in retrieval_errors):
            missing.append("no unresolved retrieval backend errors")
        if any("RETRIEVAL_BACKEND_ERROR" in warning or "QDRANT_INDEX_MISSING" in warning for warning in retrieval_warnings):
            missing.append("no unresolved retrieval backend errors")
        if any("FALLBACK_RETRIEVAL_USED" in warning or "FALLBACK" in warning for warning in retrieval_warnings) and not retrieval.get("fallback_admin_approved"):
            missing.append("no fallback context unless admin explicitly approves")
        if tool.get("execution", {}).get("type") == "database_sql" and validation_checks.get("sql_parser") != "passed":
            missing.append("no malformed SQL")
        if not tool.get("version", {}).get("version"):
            missing.append("version is required")
        if not tool.get("safety", {}).get("audit_policy"):
            missing.append("audit policy is required")
        if not tool.get("version", {}).get("rollback"):
            missing.append("rollback/deactivation support is required")
        missing = list(dict.fromkeys(missing))
        return {"can_publish": not missing, "missing_requirements": missing}
