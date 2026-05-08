from __future__ import annotations

from typing import Any


class PublishGate:
    @staticmethod
    def evaluate(result: dict[str, Any], admin_approval_exists: bool = False) -> dict[str, Any]:
        missing = []
        if result.get("review", {}).get("status") != "approved":
            missing.append("review.status must be approved")
        if result.get("validation", {}).get("status") != "passed":
            missing.append("validation.status must be passed")
        validation_checks = result.get("validation", {}).get("checks") or {}
        if (result.get("tool") or {}).get("execution", {}).get("type") == "database_sql":
            if validation_checks.get("sql_parser") != "passed":
                missing.append("SQL parser validation passed is required")
            if validation_checks.get("sql_explain") != "passed":
                missing.append("PostgreSQL EXPLAIN validation passed is required")
        if result.get("tests", {}).get("status") != "passed":
            missing.append("tests.status must be passed")
        if not admin_approval_exists:
            missing.append("admin approval record is required")
        tool = result.get("tool") or {}
        retrieval = result.get("retrieval") or {}
        retrieval_warnings = [str(item) for item in retrieval.get("warnings") or []]
        if retrieval.get("status") != "passed":
            missing.append("retrieval.status must be passed")
        if float(retrieval.get("confidence") or 0.0) < 0.80:
            missing.append("retrieval.confidence must be >= 0.80")
        scope_filter = retrieval.get("scope_filter") or {}
        for key in ("organization_id", "tenant_integration_id", "provider_connection_id", "selected_context_snapshot_id", "integration_type", "provider"):
            if not scope_filter.get(key):
                missing.append(f"retrieval scope missing {key}")
        source_context = tool.get("source_context") or []
        if not source_context or not all(isinstance(item, dict) for item in source_context):
            missing.append("source_context must be structured")
        snapshot_id = scope_filter.get("selected_context_snapshot_id")
        if snapshot_id and any(item.get("context_snapshot_id") != snapshot_id for item in source_context if isinstance(item, dict)):
            missing.append("all source_context entries must come from selected context snapshot")
        rejected = retrieval.get("rejected_chunks") or []
        if any(item.get("reason") in {"TENANT_SCOPE_MISMATCH", "CONTEXT_SNAPSHOT_MISMATCH", "PROVIDER_MISMATCH"} and float(item.get("retrieval_score") or 0.0) >= 0.6 for item in rejected if isinstance(item, dict)):
            missing.append("no high-relevance rejected chunks due to scope mismatch")
        if any(item.get("reason") == "DEPRECATED_CONTEXT" for item in rejected if isinstance(item, dict)):
            missing.append("no stale/deprecated context")
        if any(item.get("reason") in {"QDRANT_INDEX_MISSING", "RETRIEVAL_BACKEND_ERROR"} for item in rejected if isinstance(item, dict)):
            missing.append("no unresolved retrieval backend errors")
        if any("RETRIEVAL_BACKEND_ERROR" in warning or "QDRANT_INDEX_MISSING" in warning for warning in retrieval_warnings):
            missing.append("no unresolved retrieval backend errors")
        if any("FALLBACK_RETRIEVAL_USED" in warning or "FALLBACK" in warning for warning in retrieval_warnings) and not retrieval.get("fallback_admin_approved"):
            missing.append("no fallback context unless admin explicitly approves")
        if (result.get("tool") or {}).get("execution", {}).get("type") == "database_sql" and validation_checks.get("sql_parser") != "passed":
            missing.append("no malformed SQL")
        if not tool.get("version", {}).get("version"):
            missing.append("version is required")
        if not tool.get("safety", {}).get("audit_policy"):
            missing.append("audit policy is required")
        if not tool.get("version", {}).get("rollback"):
            missing.append("rollback/deactivation support is required")
        missing = list(dict.fromkeys(missing))
        return {"can_publish": not missing, "missing_requirements": missing}
