from __future__ import annotations

import re
from typing import Any

from app.services.mcp_toolkit.intent_classifier import IntentClassifier
from app.services.mcp_toolkit.name_utils import has_valid_operation_prefix, is_table_name_only
from app.services.mcp_toolkit.sql_validator import SQLValidator


class Reviewer:
    OPERATION_SQL_STATEMENT = {"read": "SELECT", "create": "INSERT", "update": "UPDATE", "delete": "DELETE"}
    READ_OUTPUT_FIELDS = {"records", "count"}
    MUTATION_OUTPUT_FIELDS = {"operation", "provider_reference", "affected_count", "idempotency_key", "created_id", "updated_id", "deleted_id"}
    FALLBACK_TEXT_PATTERN = re.compile(
        r"(postgresql generated tool|draft generated from indexed integration context|configure azure openai for richer tool synthesis)",
        flags=re.IGNORECASE,
    )

    @staticmethod
    def validate(generation: dict[str, Any], verified_context: dict[str, Any], existing_tool_names: set[str] | None = None) -> dict[str, Any]:
        tool = generation.get("tool") or {}
        plan = generation.get("tool_plan") or {}
        execution = tool.get("execution") or {}
        execution_type = str(execution.get("type") or "")
        mapping = execution.get("mapping") or {}
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        required_changes: list[str] = []
        checks: dict[str, str] = {}

        Reviewer._check(bool(tool.get("name")), "MISSING_TOOL_NAME", "Tool name is required.", errors, required_changes)
        Reviewer._check(Reviewer._snake_case(tool.get("name")), "BAD_TOOL_NAME", "Tool name must be action-based snake_case.", errors, required_changes)
        Reviewer._check(
            not Reviewer._contains_fallback_text(tool),
            "FALLBACK_TITLE_USED",
            "Tool title/description must not contain fallback generator phrases.",
            errors,
            required_changes,
        )
        if existing_tool_names and tool.get("name") in existing_tool_names:
            errors.append({"code": "DUPLICATE_TOOL_NAME", "message": "Tool name already exists in this registry scope."})
            required_changes.append("Choose a unique tool name for this organization integration registry.")
        input_schema = tool.get("input_schema") or {}
        output_schema = tool.get("output_schema") or {}
        Reviewer._validate_retrieval(generation, errors, warnings, required_changes)
        Reviewer._check(input_schema.get("type") == "object", "INVALID_INPUT_SCHEMA", "input_schema must be an object schema.", errors, required_changes)
        Reviewer._check(input_schema.get("additionalProperties") is False, "LOOSE_INPUT_SCHEMA", "input_schema must set additionalProperties false.", errors, required_changes)
        Reviewer._check(bool(input_schema.get("properties")), "EMPTY_INPUT_SCHEMA", "input_schema must define concrete fields unless no input is truly required.", errors, required_changes)
        Reviewer._check(output_schema.get("type") == "object", "INVALID_OUTPUT_SCHEMA", "output_schema must be an object schema.", errors, required_changes)
        Reviewer._check("error_code" in (output_schema.get("properties") or {}), "MISSING_ERROR_CODE", "output_schema must include error_code.", errors, required_changes)
        Reviewer._check("trace_id" in (output_schema.get("properties") or {}), "MISSING_TRACE_ID", "output_schema must include trace_id.", errors, required_changes)
        operation_type = str(plan.get("operation_type") or "")
        Reviewer._check(
            not Reviewer._has_read_intent_text(tool) or operation_type == "read",
            "INTENT_OPERATION_MISMATCH",
            "Tool title/description uses read intent verbs but operation_type is not read.",
            errors,
            required_changes,
        )
        intent_signals = Reviewer._effective_intent_signals(plan)
        if intent_signals.get("read_detected") and (intent_signals.get("update_detected") or intent_signals.get("create_detected") or intent_signals.get("delete_detected")):
            Reviewer._check(
                operation_type in {"workflow", "update", "create", "delete", "bulk_update", "workflow_bulk_update"},
                "MUTATION_INTENT_DOWNGRADED_TO_READ",
                "Prompt contains both read and mutation intent; generated tool must not be read-only.",
                errors,
                required_changes,
            )
        if operation_type == "workflow":
            Reviewer._check(bool(plan.get("workflow_type")), "MULTI_STEP_WORKFLOW_DETECTED", "Workflow prompts must declare an explicit workflow_type.", errors, required_changes)
        Reviewer._validate_prompt_intent_contract(tool, plan, execution, mapping, input_schema, intent_signals, errors, required_changes)
        if any("username column" in str(item).lower() for item in tool.get("missing_context") or []):
            Reviewer._check(False, "MISSING_TARGET_FIELD_CONFIRMATION", "Username target field is missing or ambiguous and requires admin confirmation.", errors, required_changes)
        if any("latest order means" in str(item).lower() for item in tool.get("missing_context") or []):
            Reviewer._check(False, "LATEST_ORDER_POLICY_REQUIRED", "Confirm whether latest order means the order with the greatest created_at for the customer.", errors, required_changes)
        if any("bulk mutation policy" in str(item).lower() for item in tool.get("missing_context") or []):
            Reviewer._check(False, "BULK_MUTATION_POLICY_REQUIRED", "Approved bulk mutation policy is required before generating executable UPDATE SQL.", errors, required_changes)
        if any("source and target order status" in str(item).lower() for item in tool.get("missing_context") or []):
            Reviewer._check(False, "PROMPT_VALUE_EXTRACTION_FAILED", "Bulk order status prompt must extract source and target status values.", errors, required_changes)

        if execution_type == "database_sql":
            sql = str(mapping.get("sql_template") or "")
            validation_mapping = {**mapping, "input_schema": input_schema}
            allowed_tables = set(mapping.get("allowed_tables") or [])
            verified_tables = set(verified_context.get("allowed_tables") or [])
            sql_validation = SQLValidator.validate(sql, str(tool.get("provider") or ""), validation_mapping)
            checks["sql_parser"] = sql_validation["parser"]["status"]
            checks["sql_explain"] = sql_validation["explain"]["status"]
            sql_info = Reviewer._parse_database_sql(sql)
            expected_statement = Reviewer.OPERATION_SQL_STATEMENT.get(operation_type)
            allowed_statements = Reviewer._statement_list(mapping.get("allowed_statements"))
            blocked_statements = Reviewer._statement_list(mapping.get("blocked_statements"))
            plan_resources = set(plan.get("exact_resources_used") or [])
            Reviewer._check(
                has_valid_operation_prefix(str(tool.get("name") or ""), operation_type),
                "BAD_OPERATION_NAME_PREFIX",
                "tool.name must start with an allowed action prefix for its operation_type.",
                errors,
                required_changes,
            )
            Reviewer._check(
                not is_table_name_only(str(tool.get("name") or ""), list(plan_resources)),
                "GENERIC_TABLE_NAME_USED",
                "tool.name must not be a bare table name.",
                errors,
                required_changes,
            )
            Reviewer._check(bool(sql), "MISSING_SQL_TEMPLATE", "Database tools require fixed-purpose SQL.", errors, required_changes)
            Reviewer._check(
                sql_validation["parser"]["status"] == "passed",
                "SQL_SYNTAX_INVALID",
                sql_validation["parser"].get("message") or "SQL parser validation failed.",
                errors,
                required_changes,
            )
            Reviewer._check(
                sql_validation["explain"]["status"] == "passed",
                "SQL_EXPLAIN_FAILED",
                sql_validation["explain"].get("message") or "PostgreSQL EXPLAIN validation failed.",
                errors,
                required_changes,
            )
            Reviewer._check("SELECT *" not in sql.upper(), "SELECT_STAR", "SQL must not use SELECT *.", errors, required_changes)
            Reviewer._check("<allowed_table>" not in sql, "GENERIC_SQL_TEMPLATE", "SQL must not contain generic table placeholders.", errors, required_changes)
            Reviewer._check(not re.search(r"\{|\}|\$\{", sql), "DYNAMIC_SQL", "SQL must not contain dynamic table or column interpolation.", errors, required_changes)
            Reviewer._check(bool(sql_info["statement"]), "UNKNOWN_SQL_STATEMENT", "SQL first statement must be SELECT, INSERT, UPDATE, or DELETE.", errors, required_changes)
            if sql_info["statement"]:
                Reviewer._check(
                    allowed_statements == [sql_info["statement"]],
                    "SQL_ALLOWED_STATEMENT_MISMATCH",
                    "SQL first statement must exactly match allowed_statements.",
                    errors,
                    required_changes,
                )
                Reviewer._check(
                    sql_info["statement"] not in blocked_statements,
                    "SQL_BLOCKED_STATEMENT_USED",
                    "SQL first statement must not appear in blocked_statements.",
                    errors,
                    required_changes,
                )
            if expected_statement:
                Reviewer._check(
                    sql_info["statement"] == expected_statement,
                    "OPERATION_SQL_MISMATCH",
                    f"operation_type {operation_type} must generate {expected_statement} SQL.",
                    errors,
                    required_changes,
                )
            if operation_type == "workflow":
                Reviewer._check(
                    sql_info["statement"] in {"INSERT", "UPDATE", "DELETE"},
                    "MUTATION_SQL_BLOCKED_DUE_TO_MISSING_TARGET_FIELD" if any("username field" in str(item).lower() for item in tool.get("missing_context") or []) else "MUTATION_INTENT_DOWNGRADED_TO_READ",
                    "Workflow SQL generation is blocked until the target mutation field is explicitly confirmed." if any("username field" in str(item).lower() for item in tool.get("missing_context") or []) else "Workflow prompt included mutation intent but generated SQL is not a mutation statement.",
                    errors,
                    required_changes,
                )
                Reviewer._validate_workflow_target_selection(sql, plan, input_schema, errors, required_changes)
                Reviewer._validate_workflow_precheck(mapping, plan, errors, required_changes)
            if operation_type in {"bulk_update", "workflow_bulk_update"}:
                Reviewer._validate_bulk_mutation_contract(sql, tool, plan, mapping, input_schema, sql_info, errors, required_changes)
            if sql_info["table"]:
                sql_tables = set(sql_info.get("tables") or [sql_info["table"]])
                Reviewer._check(
                    sql_tables == allowed_tables,
                    "SQL_ALLOWED_TABLE_MISMATCH",
                    "SQL referenced tables must exactly match least-privilege allowed_tables.",
                    errors,
                    required_changes,
                )
                Reviewer._check(
                    sql_tables == plan_resources,
                    "SQL_PLAN_TABLE_MISMATCH",
                    "SQL referenced tables must exactly match tool_plan.exact_resources_used.",
                    errors,
                    required_changes,
                )
                Reviewer._check(
                    Reviewer._tool_name_matches_plan_and_table(tool, plan, list(plan_resources)),
                    "TOOL_NAME_TABLE_MISMATCH",
                    "tool.name, mcp.tool_name, tool_plan.name, and SQL target table must describe the same capability.",
                    errors,
                    required_changes,
                )
            Reviewer._check(
                tool.get("name") == plan.get("name") == (tool.get("mcp") or {}).get("tool_name"),
                "TOOL_PLAN_NAME_MISMATCH",
                "tool.name, tool_plan.name, and mcp.tool_name must be identical.",
                errors,
                required_changes,
            )
            Reviewer._check(
                Reviewer._tool_name_matches_operation(str(tool.get("name") or ""), operation_type),
                "TOOL_NAME_OPERATION_MISMATCH",
                "Tool name must contain an action verb that matches operation_type.",
                errors,
                required_changes,
            )
            Reviewer._validate_database_inputs(input_schema, sql_info, mapping, errors, required_changes)
            Reviewer._validate_mutation_sql_contract(sql, plan, input_schema, sql_info, errors, required_changes)
            Reviewer._validate_database_outputs(output_schema, plan, sql_info, verified_context, errors, required_changes)
            Reviewer._validate_database_resource_subset(sql, sql_info, generation.get("retrieval") or {}, verified_context, errors, required_changes)
            Reviewer._validate_insert_required_columns(sql_info, verified_context, errors, required_changes)
            if execution.get("mode") == "read_only":
                Reviewer._check(sql_info["statement"] == "SELECT", "READ_SQL_NOT_SELECT", "Read-only database tools may only execute SELECT.", errors, required_changes)
                Reviewer._check(bool(re.search(r"\bLIMIT\b", sql, re.IGNORECASE)), "MISSING_LIMIT", "Read SQL must include LIMIT.", errors, required_changes)
                Reviewer._check(not re.search(r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|EXECUTE|CALL|MERGE|REPLACE|VACUUM|ANALYZE|COMMIT|ROLLBACK)\b", sql, re.IGNORECASE), "BLOCKED_SQL_STATEMENT", "Read SQL contains blocked mutation/admin statement.", errors, required_changes)
            else:
                Reviewer._check(bool(plan.get("approval_policy", {}).get("human_approval_required")), "WRITE_WITHOUT_APPROVAL", "Write tools require human approval policy.", errors, required_changes)
                if sql.upper().lstrip().startswith(("UPDATE", "DELETE")):
                    Reviewer._check(bool(re.search(r"\bWHERE\b", sql, re.IGNORECASE)), "MUTATION_WITHOUT_WHERE", "UPDATE/DELETE SQL must include strict WHERE clause.", errors, required_changes)
            Reviewer._check(bool(allowed_tables), "EMPTY_ALLOWED_TABLES", "Database tools must define least-privilege allowed_tables.", errors, required_changes)
            Reviewer._check(
                allowed_tables.issubset(verified_tables),
                "SQL_UNVERIFIED_TABLE_USED",
                "allowed_tables must be verified by scanned schema context.",
                errors,
                required_changes,
            )
            Reviewer._check(bool(mapping.get("allowed_columns")), "MISSING_ALLOWED_COLUMNS", "Database tools must define allowed_columns.", errors, required_changes)
            Reviewer._check(bool(mapping.get("explain_validation", {}).get("required_before_publish")), "MISSING_EXPLAIN_VALIDATION", "Database SQL requires EXPLAIN validation before publish.", errors, required_changes)
        elif execution_type == "database_nosql":
            collection = str(mapping.get("collection") or "")
            allowed_collections = set(mapping.get("allowed_collections") or [])
            input_props = set((input_schema.get("properties") or {}).keys())
            filter_fields = set(mapping.get("filter_fields") or [])
            Reviewer._check(bool(collection), "MISSING_COLLECTION", "MongoDB tools must define a verified collection.", errors, required_changes)
            Reviewer._check(collection in allowed_collections, "UNVERIFIED_RESOURCE_USED", "MongoDB collection must be present in allowed_collections.", errors, required_changes)
            Reviewer._check(bool(allowed_collections), "EMPTY_ALLOWED_COLLECTIONS", "MongoDB tools must define least-privilege allowed_collections.", errors, required_changes)
            Reviewer._check(filter_fields.issubset(input_props), "MISSING_INPUT_SCHEMA_FIELD", "MongoDB filter fields must exist in input_schema.", errors, required_changes)
            if execution.get("mode") == "read_only":
                Reviewer._check(bool((mapping.get("query") or {}).get("filter")), "MISSING_MONGO_FILTER", "MongoDB read tools require a verified filter shape.", errors, required_changes)
            else:
                Reviewer._check(bool((mapping.get("idempotency") or {}).get("required")), "IDEMPOTENCY_REQUIRED", "MongoDB mutation tools require idempotency.", errors, required_changes)
                Reviewer._check(bool(mapping.get("update_filter") or mapping.get("insert_document")), "MISSING_MUTATION_FILTER", "MongoDB mutation tools must define a bounded insert or update filter.", errors, required_changes)
        else:
            Reviewer._check(bool(mapping.get("endpoint") or mapping.get("connector_action")), "MISSING_VERIFIED_CONNECTOR_ACTION", "API tools must use verified connector templates/actions.", errors, required_changes)
            Reviewer._check(mapping.get("tenant_secret_ref") == "tenant_provider_connection_secret_ref", "BAD_SECRET_REFERENCE", "API tools must reference tenant secrets only.", errors, required_changes)
            if execution.get("mode") != "read_only":
                Reviewer._check(bool((mapping.get("idempotency") or {}).get("required")), "IDEMPOTENCY_REQUIRED", "Mutation connector/API tools require idempotency.", errors, required_changes)
            Reviewer._check(bool(mapping.get("rate_limit") or execution_type in {"erp_xml", "his_action"}), "RATE_LIMIT_REQUIRED", "Provider-backed tools must include rate limit or backend execution controls.", errors, required_changes)

        Reviewer._check(tool.get("review", {}).get("required") is True, "REVIEW_NOT_REQUIRED", "All generated tools require admin review.", errors, required_changes)
        Reviewer._check(tool.get("tests", {}).get("test_run_required") is True, "TEST_RUN_NOT_REQUIRED", "test_run_required must be true.", errors, required_changes)
        Reviewer._check(bool(tool.get("safety", {}).get("pii_policy")), "MISSING_PII_POLICY", "PII policy is required.", errors, required_changes)
        Reviewer._validate_pii_output_schema(tool, errors, required_changes)
        Reviewer._validate_pii_policy_scope(tool, plan, errors, required_changes)
        Reviewer._check(bool(tool.get("safety", {}).get("audit_policy")), "MISSING_AUDIT_POLICY", "Audit policy is required.", errors, required_changes)
        if not plan.get("exact_resources_used"):
            errors.append({"code": "MISSING_VERIFIED_CONTEXT", "message": "No verified resource is available for this tool."})
            required_changes.append("Rescan or select integration resources before generating this tool.")

        Reviewer._enforce_validation_check_consistency(errors, checks)
        errors = Reviewer._dedupe_errors(errors)
        warnings = Reviewer._dedupe_errors(warnings)
        status = "failed" if errors else "passed"
        review_status = Reviewer._review_status(errors, generation)
        return {
            "validation": {"status": status, "errors": errors, "warnings": warnings, "checks": checks},
            "review": {
                "status": review_status,
                "required": True,
                "reviewer_role": "admin",
                "reason": Reviewer._review_reason(review_status),
                "required_changes": list(dict.fromkeys(required_changes)),
            },
        }

    @staticmethod
    def _snake_case(value: Any) -> bool:
        return isinstance(value, str) and bool(re.fullmatch(r"[a-z][a-z0-9_]{1,63}", value))

    @staticmethod
    def _has_read_intent_text(tool: dict[str, Any]) -> bool:
        text = f"{tool.get('title') or ''} {tool.get('description') or ''}".lower()
        return bool(re.search(r"\b(find|lookup|search|retrieve|check|get|view|list|fetch)\b", text))

    @staticmethod
    def _contains_fallback_text(tool: dict[str, Any]) -> bool:
        text = f"{tool.get('title') or ''} {tool.get('description') or ''}"
        return bool(Reviewer.FALLBACK_TEXT_PATTERN.search(text))

    @staticmethod
    def _validate_retrieval(
        generation: dict[str, Any],
        errors: list[dict[str, str]],
        warnings: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        retrieval = generation.get("retrieval") or {}
        tool = generation.get("tool") or {}
        plan = generation.get("tool_plan") or {}
        scope_filter = retrieval.get("scope_filter") or {}
        required_scope = ["organization_id", "tenant_integration_id", "provider_connection_id", "selected_context_snapshot_id", "integration_type", "provider"]
        if not retrieval:
            Reviewer._check(False, "RETRIEVAL_FAILED", "Embedding retrieval result is required.", errors, required_changes)
            return
        missing_scope = [key for key in required_scope if not scope_filter.get(key)]
        Reviewer._check(not missing_scope, "SELECTED_INTEGRATION_REQUIRED", f"Selected integration scope is missing: {', '.join(missing_scope)}.", errors, required_changes)
        Reviewer._check(not missing_scope, "UNSCOPED_EMBEDDING_SEARCH", "Embedding retrieval must use selected integration scope filters.", errors, required_changes)
        if retrieval.get("status") == "failed":
            Reviewer._check(False, "RETRIEVAL_FAILED", "Embedding retrieval failed; executable SQL/API must not be generated.", errors, required_changes)
        elif retrieval.get("status") == "low_confidence" or float(retrieval.get("confidence") or 0.0) < 0.80:
            Reviewer._check(False, "RETRIEVAL_LOW_CONFIDENCE", "Embedding retrieval confidence is below production threshold.", errors, required_changes)
        if retrieval.get("fallback_used"):
            Reviewer._check(False, "FALLBACK_RETRIEVAL_USED", "Fallback context was used and cannot publish without explicit admin approval.", errors, required_changes)
        retrieval_warnings = [str(item) for item in retrieval.get("warnings") or []]
        for warning in retrieval_warnings:
            warnings.append({"code": Reviewer._retrieval_warning_code(warning), "message": warning})
        if any("QDRANT_INDEX_MISSING" in warning for warning in retrieval_warnings):
            Reviewer._check(False, "QDRANT_INDEX_MISSING", "Qdrant payload index is missing for scoped retrieval filters.", errors, required_changes)
        if any("QDRANT_INDEX_MISSING_FALLBACK_USED" in warning for warning in retrieval_warnings):
            Reviewer._check(False, "FALLBACK_RETRIEVAL_USED", "Fallback context was used after Qdrant index failure and requires explicit admin approval.", errors, required_changes)
        if any("RETRIEVAL_BACKEND_ERROR" in warning for warning in retrieval_warnings):
            Reviewer._check(False, "RETRIEVAL_BACKEND_ERROR", "Embedding retrieval backend returned an unresolved error.", errors, required_changes)

        source_context = tool.get("source_context") or []
        Reviewer._check(bool(source_context) and all(isinstance(item, dict) for item in source_context), "SOURCE_CONTEXT_NOT_STRUCTURED", "source_context must contain structured context objects.", errors, required_changes)
        providers = {item.get("provider") for item in source_context if isinstance(item, dict) and item.get("provider")}
        snapshots = {item.get("context_snapshot_id") for item in source_context if isinstance(item, dict) and item.get("context_snapshot_id")}
        integrations = {item.get("integration_type") for item in source_context if isinstance(item, dict) and item.get("integration_type")}
        Reviewer._check(not providers or providers == {scope_filter.get("provider")}, "PROVIDER_MISMATCH", "Retrieved chunks must not mix providers.", errors, required_changes)
        Reviewer._check(not integrations or integrations == {scope_filter.get("integration_type")}, "CONTEXT_SCOPE_VIOLATION", "Retrieved chunks must not mix integration types.", errors, required_changes)
        Reviewer._check(not snapshots or snapshots == {scope_filter.get("selected_context_snapshot_id")}, "CONTEXT_SNAPSHOT_MISMATCH", "Retrieved chunks must come from the selected context snapshot.", errors, required_changes)
        if any(item.get("status") not in {"active", "approved"} for item in source_context if isinstance(item, dict)):
            Reviewer._check(False, "STALE_CONTEXT_USED", "Generated tool uses stale, deprecated, or unapproved context.", errors, required_changes)

        verified_resources = {item.get("name") for item in retrieval.get("verified_resources") or [] if isinstance(item, dict) and item.get("name")}
        exact_resources = set(plan.get("exact_resources_used") or [])
        source_resources = {item.get("resource") for item in source_context if isinstance(item, dict) and item.get("resource")}
        Reviewer._check(not exact_resources or exact_resources.issubset(verified_resources), "UNVERIFIED_RESOURCE_USED", "Generated SQL/API uses resources outside retrieval.verified_resources.", errors, required_changes)
        Reviewer._check(not exact_resources or source_resources == exact_resources, "SOURCE_CONTEXT_RESOURCE_MISMATCH", "source_context resources must match tool_plan.exact_resources_used.", errors, required_changes)
        for rejected in retrieval.get("rejected_chunks") or []:
            if not isinstance(rejected, dict):
                continue
            reason = rejected.get("reason")
            score = float(rejected.get("retrieval_score") or 0.0)
            if reason == "SOURCE_KIND_NOT_ALLOWED" and score >= 0.8:
                Reviewer._check(False, "SOURCE_KIND_NOT_ALLOWED", "Retrieved context included source kinds not allowed for the selected provider.", errors, required_changes)
            if reason == "DEPRECATED_CONTEXT" and score >= 0.6:
                Reviewer._check(False, "STALE_CONTEXT_USED", "High-relevance stale/deprecated context was retrieved.", errors, required_changes)
            if reason == "CONTEXT_SNAPSHOT_MISMATCH" and score >= 0.6:
                Reviewer._check(False, "CONTEXT_SNAPSHOT_MISMATCH", "High-relevance context from another snapshot was retrieved.", errors, required_changes)
            if reason == "PROVIDER_MISMATCH" and score >= 0.6:
                Reviewer._check(False, "PROVIDER_MISMATCH", "High-relevance context from another provider was retrieved.", errors, required_changes)
            if reason == "QDRANT_INDEX_MISSING":
                Reviewer._check(False, "QDRANT_INDEX_MISSING", "Qdrant payload index is missing for scoped retrieval filters.", errors, required_changes)
            if reason == "RETRIEVAL_BACKEND_ERROR":
                Reviewer._check(False, "RETRIEVAL_BACKEND_ERROR", "Embedding retrieval backend returned an unresolved error.", errors, required_changes)
            if reason == "FALLBACK_RETRIEVAL_USED":
                Reviewer._check(False, "FALLBACK_RETRIEVAL_USED", "Fallback context was used and requires explicit admin approval.", errors, required_changes)

    @staticmethod
    def _retrieval_warning_code(warning: str) -> str:
        upper = warning.upper()
        if "QDRANT_INDEX_MISSING" in upper:
            return "QDRANT_INDEX_MISSING"
        if "FALLBACK" in upper:
            return "FALLBACK_RETRIEVAL_USED"
        if "BACKEND" in upper or "QDRANT" in upper:
            return "RETRIEVAL_BACKEND_ERROR"
        return "RETRIEVAL_WARNING"

    @staticmethod
    def _check(condition: bool, code: str, message: str, errors: list[dict[str, str]], required_changes: list[str]) -> None:
        if condition:
            return
        errors.append({"code": code, "message": message})
        required_changes.append(message)

    @staticmethod
    def _dedupe_errors(items: list[dict[str, str]]) -> list[dict[str, Any]]:
        deduped: dict[tuple[str, str], dict[str, Any]] = {}
        for item in items:
            code = str(item.get("code") or "")
            message = str(item.get("message") or "")
            key = (code, message)
            if key not in deduped:
                deduped[key] = {"code": code, "message": message, "occurrence_count": 0}
            deduped[key]["occurrence_count"] += 1
        return list(deduped.values())

    @staticmethod
    def _review_status(errors: list[dict[str, Any]], generation: dict[str, Any]) -> str:
        if not errors:
            return "draft"
        codes = {error.get("code") for error in errors}
        if codes & {"RETRIEVAL_LOW_CONFIDENCE", "MISSING_TARGET_FIELD_CONFIRMATION", "MULTI_STEP_WORKFLOW_DETECTED", "LATEST_ORDER_POLICY_REQUIRED", "BULK_MUTATION_POLICY_REQUIRED"}:
            return "needs_context_confirmation"
        retrieval = generation.get("retrieval") or {}
        if retrieval.get("fallback_used") or retrieval.get("status") == "low_confidence":
            return "needs_context_confirmation"
        return "rejected"

    @staticmethod
    def _review_reason(review_status: str) -> str:
        if review_status == "draft":
            return "Draft passed automatic checks and awaits admin review."
        if review_status == "needs_context_confirmation":
            return "Draft requires explicit admin context confirmation before it can be reviewed further."
        return "Automatic reviewer rejected unsafe or incomplete draft."

    @staticmethod
    def _statement_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip().upper() for item in value if str(item).strip()]

    @staticmethod
    def _parse_database_sql(sql: str) -> dict[str, Any]:
        cleaned = SQLValidator.clean_sql(sql).rstrip(";")
        first = SQLValidator._split_statements(cleaned)[0] if SQLValidator._split_statements(cleaned) else ""
        token = SQLValidator.first_token(first)
        statement = SQLValidator.cte_statement_type(first) if token == "WITH" else token if token in {"SELECT", "INSERT", "UPDATE", "DELETE"} else ""
        main_select = SQLValidator.main_select_sql(first) if statement == "SELECT" else first
        table = ""
        tables: list[str] = []
        insert_columns: list[str] = []
        selected_columns: list[str] = []
        updated_columns: list[str] = []
        returning_columns: list[str] = []
        if statement == "SELECT":
            table_match = re.search(r"\bFROM\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\b", first, flags=re.IGNORECASE)
            table = table_match.group(1) if table_match else ""
            tables = re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\b", first, flags=re.IGNORECASE)
            select_list = Reviewer._top_level_select_list(main_select)
            if select_list:
                selected_columns = Reviewer._extract_column_names(select_list, table)
        elif statement == "INSERT":
            table_match = re.search(r"\bINSERT\s+INTO\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\s*(?:\(([^)]*)\))?", first, flags=re.IGNORECASE | re.DOTALL)
            if table_match:
                table = table_match.group(1)
                tables = [table]
                insert_columns = [Reviewer._clean_column_name(item) for item in (table_match.group(2) or "").split(",") if Reviewer._clean_column_name(item)]
            returning_match = re.search(r"\bRETURNING\s+(.+)$", first, flags=re.IGNORECASE | re.DOTALL)
            if returning_match:
                returning_columns = Reviewer._extract_column_names(returning_match.group(1), table)
        elif statement == "UPDATE":
            table_match = re.search(r"\bUPDATE\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)(?:\s+(?:AS\s+)?[a-zA-Z_][\w]*)?\s+SET\s+(.+?)(?:\s+WHERE\s+|$)", first, flags=re.IGNORECASE | re.DOTALL)
            if table_match:
                table = table_match.group(1)
                tables = list(dict.fromkeys([table, *re.findall(r"\b(?:FROM|JOIN)\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\b", first, flags=re.IGNORECASE)]))
                updated_columns = [Reviewer._clean_column_name(item.split("=", 1)[0]) for item in table_match.group(2).split(",") if Reviewer._clean_column_name(item.split("=", 1)[0])]
            returning_match = re.search(r"\bRETURNING\s+(.+)$", first, flags=re.IGNORECASE | re.DOTALL)
            if returning_match:
                returning_columns = Reviewer._extract_column_names(returning_match.group(1), table)
        elif statement == "DELETE":
            table_match = re.search(r"\bDELETE\s+FROM\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\b", first, flags=re.IGNORECASE)
            table = table_match.group(1) if table_match else ""
            tables = [table] if table else []
            returning_match = re.search(r"\bRETURNING\s+(.+)$", first, flags=re.IGNORECASE | re.DOTALL)
            if returning_match:
                returning_columns = Reviewer._extract_column_names(returning_match.group(1), table)
        params = sorted(set(re.findall(r"(?<!:):([a-zA-Z_][\w]*)\b", first)))
        return {
            "statement": statement,
            "table": table,
            "tables": tables or ([table] if table else []),
            "params": params,
            "insert_columns": insert_columns,
            "selected_columns": selected_columns,
            "updated_columns": updated_columns,
            "returning_columns": returning_columns,
        }

    @staticmethod
    def _extract_column_names(select_list: str, table: str) -> list[str]:
        columns = []
        table_prefix = f"{table}." if table else ""
        for item in Reviewer._split_sql_list(select_list):
            cleaned = item.strip()
            alias_parts = re.split(r"\s+AS\s+", cleaned, flags=re.IGNORECASE)
            cleaned = alias_parts[-1].strip() if len(alias_parts) > 1 else alias_parts[0].strip()
            cleaned = Reviewer._clean_column_name(cleaned)
            if table_prefix and cleaned.startswith(table_prefix):
                cleaned = cleaned[len(table_prefix):]
            if cleaned:
                columns.append(cleaned)
        return columns

    @staticmethod
    def _clean_column_name(value: str) -> str:
        value = re.sub(r"[^a-zA-Z0-9_.]", "", value or "")
        return value.split(".")[-1] if value else ""

    @staticmethod
    def _split_sql_list(value: str) -> list[str]:
        items: list[str] = []
        current: list[str] = []
        depth = 0
        quote: str | None = None
        for char in value:
            if quote:
                current.append(char)
                if char == quote:
                    quote = None
                continue
            if char in {"'", '"'}:
                quote = char
                current.append(char)
                continue
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            if char == "," and depth == 0:
                item = "".join(current).strip()
                if item:
                    items.append(item)
                current = []
            else:
                current.append(char)
        item = "".join(current).strip()
        if item:
            items.append(item)
        return items

    @staticmethod
    def _top_level_select_list(sql: str) -> str:
        match = re.match(r"\s*SELECT\s+", sql, flags=re.IGNORECASE)
        if not match:
            return ""
        start = match.end()
        depth = 0
        quote: str | None = None
        index = start
        while index < len(sql):
            char = sql[index]
            if quote:
                if char == quote:
                    quote = None
                index += 1
                continue
            if char in {"'", '"'}:
                quote = char
                index += 1
                continue
            if char == "(":
                depth += 1
            elif char == ")" and depth:
                depth -= 1
            if depth == 0 and sql[index:index + 6].upper() == " FROM ":
                return sql[start:index].strip()
            index += 1
        return ""

    @staticmethod
    def _tool_name_matches_plan_and_table(tool: dict[str, Any], plan: dict[str, Any], plan_resources: list[str]) -> bool:
        tool_name = str(tool.get("name") or "")
        plan_name = str(plan.get("name") or "")
        if tool_name != plan_name:
            return False
        resource_stems = set()
        for resource in plan_resources:
            resource_stems.update(Reviewer._resource_name_tokens(resource.split(".")[-1].lower()))
        name_tokens = set(re.findall(r"[a-z0-9]+", tool_name.lower()))
        return bool(name_tokens & resource_stems)

    @staticmethod
    def _resource_name_tokens(table_name: str) -> set[str]:
        parts = set(filter(None, re.split(r"[^a-z0-9]+", table_name.lower())))
        stems = set(parts)
        for part in parts:
            stems.add(part.rstrip("s"))
            if part.endswith("ies"):
                stems.add(f"{part[:-3]}y")
        return stems

    @staticmethod
    def _tool_name_matches_operation(tool_name: str, operation_type: str) -> bool:
        tokens = set(re.findall(r"[a-z0-9]+", tool_name.lower()))
        verbs = {
            "read": {"read", "get", "fetch", "find", "lookup", "search", "query", "list", "check", "retrieve", "view"},
            "create": {"create", "add", "insert", "open", "raise", "generate"},
            "update": {"update", "modify", "patch", "change", "set", "assign"},
            "delete": {"delete", "remove", "deactivate", "cancel"},
            "workflow": {"update", "modify", "patch", "change", "set", "assign", "workflow"},
            "bulk_update": {"bulk", "update", "mark"},
            "workflow_bulk_update": {"bulk", "update", "mark", "workflow"},
        }
        return bool(tokens & verbs.get(operation_type, set()))

    @staticmethod
    def _validate_database_inputs(
        input_schema: dict[str, Any],
        sql_info: dict[str, Any],
        mapping: dict[str, Any],
        errors: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        properties = set((input_schema.get("properties") or {}).keys())
        sql_params = set(sql_info.get("params") or [])
        allowed_non_sql = set()
        idempotency_source = ((mapping.get("idempotency") or {}).get("source") or "")
        if idempotency_source.startswith("input."):
            allowed_non_sql.add(idempotency_source.split(".", 1)[1])
        for name, schema in (input_schema.get("properties") or {}).items():
            if isinstance(schema, dict) and schema.get("runtime_control"):
                allowed_non_sql.add(str(name))
        binding_sources = {
            source.split(".", 1)[1]
            for source in ((mapping.get("parameter_binding") or {}).get("sources") or [])
            if isinstance(source, str) and source.startswith("input.")
        }
        unused = sorted(properties - sql_params - allowed_non_sql)
        missing = sorted(sql_params - properties)
        unbound = sorted(sql_params - binding_sources)
        Reviewer._check(not unused, "UNUSED_INPUT_FIELD", f"Input fields are not used by SQL/API mapping: {', '.join(unused)}.", errors, required_changes)
        Reviewer._check(not missing, "MISSING_INPUT_SCHEMA_FIELD", f"SQL parameters are missing from input_schema: {', '.join(missing)}.", errors, required_changes)
        Reviewer._check(not unbound, "UNBOUND_SQL_PARAMETER", f"SQL parameters are not bound in parameter_binding.sources: {', '.join(unbound)}.", errors, required_changes)

    @staticmethod
    def _effective_intent_signals(plan: dict[str, Any]) -> dict[str, Any]:
        signals = dict(plan.get("intent_signals") or {})
        prompt = str(signals.get("normalized_prompt") or "")
        if prompt:
            classification = IntentClassifier.classify(prompt)
            classified = IntentClassifier.to_dict(classification)
            if classified.get("update_detected") or classified.get("create_detected") or classified.get("delete_detected"):
                signals.update(classified)
        return signals

    @staticmethod
    def _validate_prompt_intent_contract(
        tool: dict[str, Any],
        plan: dict[str, Any],
        execution: dict[str, Any],
        mapping: dict[str, Any],
        input_schema: dict[str, Any],
        intent_signals: dict[str, Any],
        errors: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        has_mutation_prompt = bool(intent_signals.get("update_detected") or intent_signals.get("create_detected") or intent_signals.get("delete_detected"))
        has_read_prompt = bool(intent_signals.get("read_detected"))
        if not (has_read_prompt and has_mutation_prompt):
            return
        operation_type = str(plan.get("operation_type") or "")
        input_props = set((input_schema.get("properties") or {}).keys())
        blocked_statements = set(Reviewer._statement_list(mapping.get("blocked_statements")))
        is_read_only = execution.get("mode") == "read_only" or operation_type == "read"
        if is_read_only:
            Reviewer._check(False, "PROMPT_TOOL_INTENT_MISMATCH", "Prompt asks for read plus mutation workflow, but generated tool is read-only.", errors, required_changes)
            Reviewer._check(False, "MUTATION_INTENT_DOWNGRADED_TO_READ", "Mutation prompt generated read-only SQL/tool metadata.", errors, required_changes)
        if "UPDATE" in blocked_statements and intent_signals.get("update_detected") and operation_type not in {"bulk_update", "workflow_bulk_update"}:
            Reviewer._check(False, "MUTATION_INTENT_DOWNGRADED_TO_READ", "Mutation prompt generated a tool whose blocked_statements includes UPDATE.", errors, required_changes)
        if intent_signals.get("bulk_mutation_detected"):
            Reviewer._check(operation_type in {"bulk_update", "workflow_bulk_update"}, "PROMPT_TOOL_INTENT_MISMATCH", "Bulk mutation prompt must generate a bulk_update/workflow_bulk_update tool.", errors, required_changes)
        if intent_signals.get("target_field") == "order_status":
            latest_order_requested = bool(intent_signals.get("latest_order_requested"))
            if intent_signals.get("bulk_mutation_detected"):
                for required_field in ("idempotency_key", "approval_reason", "dry_run", "max_rows"):
                    Reviewer._check(required_field in input_props, "MISSING_REQUIRED_INPUT_FIELD", f"Required bulk mutation input is missing: {required_field}.", errors, required_changes)
                Reviewer._check(bool(intent_signals.get("source_status")) and bool(intent_signals.get("target_status")), "PROMPT_VALUE_EXTRACTION_FAILED", "Bulk order status prompt must extract pending and delivered status values.", errors, required_changes)
                return
            required_lookup_fields = ("phone_number",) if latest_order_requested else ("customer_name", "phone_number")
            for required_field in required_lookup_fields:
                Reviewer._check(required_field in input_props, "MISSING_REQUIRED_INPUT_FIELD", f"Required workflow input is missing: {required_field}.", errors, required_changes)
            if latest_order_requested:
                Reviewer._check("customer_name" not in input_props, "PROMPT_TOOL_INTENT_MISMATCH", "Prompt says using only phone number; customer_name must not be required.", errors, required_changes)
            Reviewer._check("new_order_status" in input_props, "MISSING_MUTATION_TARGET_VALUE", "Order status workflow must require new_order_status.", errors, required_changes)
            Reviewer._check("idempotency_key" in input_props, "MISSING_REQUIRED_INPUT_FIELD", "Required workflow input is missing: idempotency_key.", errors, required_changes)
            Reviewer._check(
                bool((plan.get("approval_policy") or {}).get("human_approval_required")) and execution.get("mode") == "write_requires_human_approval",
                "WORKFLOW_REQUIRES_HUMAN_APPROVAL",
                "Read-then-update order status workflows require human approval and write_requires_human_approval mode.",
                errors,
                required_changes,
            )
            if not latest_order_requested and "order_id" not in input_props and "order_number" not in input_props:
                Reviewer._check(False, "ORDER_TARGET_AMBIGUOUS", "Order status workflow must accept order_id or order_number to disambiguate multiple matches.", errors, required_changes)

    @staticmethod
    def _validate_workflow_target_selection(
        sql: str,
        plan: dict[str, Any],
        input_schema: dict[str, Any],
        errors: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        if plan.get("target_entity") != "order" or plan.get("target_field") != "order_status":
            return
        intent_signals = plan.get("intent_signals") or {}
        if intent_signals.get("latest_order_requested"):
            Reviewer._check(bool(intent_signals.get("latest_order_policy_approved")), "LATEST_ORDER_POLICY_REQUIRED", "Latest-order update requires an approved created_at ordering policy.", errors, required_changes)
            Reviewer._check(
                bool(re.search(r"\bORDER\s+BY\s+\w+\.created_at\s+DESC\s*,\s*\w+\.order_id\s+DESC\b", sql, flags=re.IGNORECASE)),
                "LATEST_ORDER_POLICY_REQUIRED",
                "Latest-order SQL must order by created_at DESC, order_id DESC.",
                errors,
                required_changes,
            )
            return
        input_props = set((input_schema.get("properties") or {}).keys())
        has_disambiguator = "order_id" in input_props or "order_number" in input_props
        has_candidate_count = bool(re.search(r"\bcandidate_count\b", sql, flags=re.IGNORECASE))
        has_match_count_guard = bool(re.search(r"\bmatch_count\s*=\s*1\b", sql, flags=re.IGNORECASE))
        has_limit_one = bool(re.search(r"\bLIMIT\s+1\b", sql, flags=re.IGNORECASE))
        Reviewer._check(has_disambiguator, "ORDER_TARGET_AMBIGUOUS", "Order update workflows must accept order_id or order_number to disambiguate multiple matches.", errors, required_changes)
        Reviewer._check(
            has_candidate_count and has_match_count_guard and not has_limit_one,
            "UNSAFE_MUTATION_TARGET_SELECTION",
            "Order update workflow must count candidate orders and avoid LIMIT 1 target selection.",
            errors,
            required_changes,
        )

    @staticmethod
    def _validate_workflow_precheck(
        mapping: dict[str, Any],
        plan: dict[str, Any],
        errors: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        if plan.get("target_entity") != "order" or plan.get("target_field") != "order_status":
            return
        if (plan.get("intent_signals") or {}).get("latest_order_requested"):
            return
        precheck = mapping.get("precheck") if isinstance(mapping.get("precheck"), dict) else {}
        result_mapping = precheck.get("result_mapping") if isinstance(precheck.get("result_mapping"), dict) else {}
        precheck_sql = str(precheck.get("sql_template") or "")
        Reviewer._check(bool(precheck.get("required")), "MUTATION_TARGET_AMBIGUOUS", "Order update workflow requires an explicit precheck before mutation.", errors, required_changes)
        Reviewer._check(bool(precheck.get("must_pass_before_mutation")), "MUTATION_TARGET_AMBIGUOUS", "Order update workflow precheck must block mutation unless it passes.", errors, required_changes)
        Reviewer._check("NO_MATCH" in result_mapping, "MUTATION_TARGET_AMBIGUOUS", "Order update precheck must map no-match candidates to NO_MATCH.", errors, required_changes)
        Reviewer._check("ORDER_TARGET_AMBIGUOUS" in result_mapping, "ORDER_TARGET_AMBIGUOUS", "Order update precheck must map multiple matches to ORDER_TARGET_AMBIGUOUS.", errors, required_changes)
        Reviewer._check("READY" in result_mapping, "MUTATION_TARGET_AMBIGUOUS", "Order update precheck must map safe target selection to READY.", errors, required_changes)
        Reviewer._check(bool(re.search(r"\bmatch_count\s*=\s*0\b", precheck_sql, flags=re.IGNORECASE)), "MUTATION_TARGET_AMBIGUOUS", "Order update precheck must explicitly distinguish zero matches.", errors, required_changes)
        Reviewer._check(bool(re.search(r"\bmatch_count\s*>\s*1\b", precheck_sql, flags=re.IGNORECASE)), "ORDER_TARGET_AMBIGUOUS", "Order update precheck must explicitly distinguish multiple matches.", errors, required_changes)

    @staticmethod
    def _validate_mutation_sql_contract(
        sql: str,
        plan: dict[str, Any],
        input_schema: dict[str, Any],
        sql_info: dict[str, Any],
        errors: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        if plan.get("operation_type") not in {"update", "workflow"}:
            return
        input_props = set((input_schema.get("properties") or {}).keys())
        params = set(sql_info.get("params") or [])
        if plan.get("target_entity") == "order" and plan.get("target_field") == "order_status":
            resolved = str(plan.get("resolved_target_column") or "").split(".")[-1] or "order_status"
            updated_columns = set(sql_info.get("updated_columns") or [])
            Reviewer._check(
                resolved in updated_columns or "order_status" in updated_columns or "status" in updated_columns,
                "TARGET_FIELD_SQL_MISMATCH",
                "Order status workflow must update order_status/status, not another order field.",
                errors,
                required_changes,
            )
            if "new_order_status" in input_props:
                Reviewer._check(
                    "new_order_status" in params and bool(re.search(r"\bSET\s+[a-zA-Z_][\w]*\s*=\s*:new_order_status\b", sql, flags=re.IGNORECASE)),
                    "UNBOUND_OR_UNUSED_MUTATION_VALUE",
                    "new_order_status must be bound and used in the UPDATE SET clause.",
                    errors,
                    required_changes,
                )
            if "phone_number" in input_props:
                Reviewer._check(
                    "phone_number" in params and bool(re.search(r"\bphone\b[^)]*=\s*:phone_number|:phone_number\b", sql, flags=re.IGNORECASE)),
                    "MUTATION_TARGET_LOOKUP_MISSING",
                    "phone_number must be bound and used to resolve the mutation target.",
                    errors,
                    required_changes,
                )

    @staticmethod
    def _validate_bulk_mutation_contract(
        sql: str,
        tool: dict[str, Any],
        plan: dict[str, Any],
        mapping: dict[str, Any],
        input_schema: dict[str, Any],
        sql_info: dict[str, Any],
        errors: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        input_props = set((input_schema.get("properties") or {}).keys())
        intent_signals = plan.get("intent_signals") or {}
        bulk = mapping.get("bulk_mutation") if isinstance(mapping.get("bulk_mutation"), dict) else {}
        approval_policy = (tool.get("safety") or {}).get("approval_policy") or plan.get("approval_policy") or {}
        Reviewer._check(bool(bulk.get("detected") or intent_signals.get("bulk_mutation_detected")), "PROMPT_TOOL_INTENT_MISMATCH", "Bulk mutation prompt must be marked as bulk_mutation_detected.", errors, required_changes)
        Reviewer._check(plan.get("target_entity") == "order", "PROMPT_TOOL_INTENT_MISMATCH", "Bulk pending/delivered mutation must target orders.", errors, required_changes)
        Reviewer._check(plan.get("target_field") == "order_status", "TARGET_FIELD_NOT_RESOLVED", "Bulk pending/delivered mutation must resolve target_field to order_status.", errors, required_changes)
        Reviewer._check(bool(plan.get("resolved_target_column")), "TARGET_FIELD_NOT_RESOLVED", "Bulk mutation target column must be resolved.", errors, required_changes)
        Reviewer._check(bool(intent_signals.get("source_status")) and bool(intent_signals.get("target_status")), "PROMPT_VALUE_EXTRACTION_FAILED", "Bulk mutation must extract pending and delivered status values.", errors, required_changes)
        for required_field in ("idempotency_key", "approval_reason", "dry_run", "max_rows"):
            Reviewer._check(required_field in input_props, "MISSING_REQUIRED_INPUT_FIELD", f"Required bulk mutation input is missing: {required_field}.", errors, required_changes)
        Reviewer._check("max_rows" in input_props, "BULK_MUTATION_LIMIT_REQUIRED", "Bulk mutation requires max_rows batch limit.", errors, required_changes)
        Reviewer._check("dry_run" in input_props, "BULK_MUTATION_PREVIEW_REQUIRED", "Bulk mutation requires dry_run preview.", errors, required_changes)
        Reviewer._check(bool(approval_policy.get("human_approval_required")) and bool(approval_policy.get("admin_approval_required")), "BULK_MUTATION_REQUIRES_ADMIN_APPROVAL", "Bulk mutation requires human and admin approval.", errors, required_changes)
        Reviewer._check(bool(mapping.get("dry_run_required")) and bool(mapping.get("preview_required")), "BULK_MUTATION_PREVIEW_REQUIRED", "Bulk mutation mapping must require preview/dry-run.", errors, required_changes)
        Reviewer._check(bool(mapping.get("batch_limit_required")), "BULK_MUTATION_LIMIT_REQUIRED", "Bulk mutation mapping must require batch limit.", errors, required_changes)
        Reviewer._check(bool(mapping.get("rollback_or_deactivation")), "BULK_MUTATION_REQUIRES_ADMIN_APPROVAL", "Bulk mutation requires rollback or compensation plan.", errors, required_changes)
        if not bulk.get("policy_approved"):
            Reviewer._check(False, "BULK_MUTATION_POLICY_REQUIRED", "Approved bulk mutation policy is required before executable UPDATE SQL.", errors, required_changes)
            Reviewer._check(sql_info.get("statement") == "SELECT", "BULK_MUTATION_PREVIEW_REQUIRED", "Without bulk policy, SQL must be preview-only SELECT.", errors, required_changes)
            Reviewer._check("affected_count_preview" in set(sql_info.get("selected_columns") or []), "BULK_MUTATION_PREVIEW_REQUIRED", "Bulk preview SQL must return affected_count_preview.", errors, required_changes)
            Reviewer._check(not re.search(r"\bUPDATE\b", sql, flags=re.IGNORECASE), "BULK_MUTATION_POLICY_REQUIRED", "Executable UPDATE SQL is blocked until bulk policy approval.", errors, required_changes)
            return
        Reviewer._check(sql_info.get("statement") == "UPDATE", "BULK_MUTATION_POLICY_REQUIRED", "Approved bulk mutation policy must generate the reviewed UPDATE SQL.", errors, required_changes)
        Reviewer._check("updated_order_status" in set(sql_info.get("returning_columns") or []), "OUTPUT_SQL_RETURNING_MISMATCH", "Bulk update SQL must return updated_order_status.", errors, required_changes)
        Reviewer._check(
            bool(re.search(r"\bSET\s+(?:order_status|status)\s*=\s*'delivered'", sql, flags=re.IGNORECASE)),
            "TARGET_FIELD_SQL_MISMATCH",
            "Bulk mutation must update order_status/status to delivered.",
            errors,
            required_changes,
        )
        Reviewer._check(
            bool(re.search(r"\bWHERE\s+(?:order_status|status)\s*=\s*'pending'", sql, flags=re.IGNORECASE)),
            "MUTATION_TARGET_LOOKUP_MISSING",
            "Bulk mutation must target only pending orders.",
            errors,
            required_changes,
        )

    @staticmethod
    def _validate_database_outputs(
        output_schema: dict[str, Any],
        plan: dict[str, Any],
        sql_info: dict[str, Any],
        verified_context: dict[str, Any],
        errors: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        statement = sql_info.get("statement")
        plan_output_names = {str(field.get("name")) for field in plan.get("output_fields") or [] if field.get("name")}
        data_schema = (output_schema.get("properties") or {}).get("data") or {}
        data_props = set((data_schema.get("properties") or {}).keys())
        if plan.get("operation_type") in {"bulk_update", "workflow_bulk_update"} and statement == "SELECT":
            selected = set(sql_info.get("selected_columns") or [])
            Reviewer._check("affected_count_preview" in data_props, "READ_OUTPUT_SQL_MISMATCH", "Bulk preview output_schema.data must include affected_count_preview.", errors, required_changes)
            Reviewer._check("affected_count_preview" in selected, "READ_OUTPUT_SQL_MISMATCH", "Bulk preview SQL must select affected_count_preview.", errors, required_changes)
            Reviewer._check(plan_output_names.issubset(selected | {"affected_count", "idempotency_key", "affected_count_preview"}), "PLAN_OUTPUT_SQL_MISMATCH", "Bulk preview plan outputs must match preview SQL or mutation metadata.", errors, required_changes)
            return
        if statement == "SELECT":
            record_schema = (((data_schema.get("properties") or {}).get("records") or {}).get("items") or {})
            record_props = set((record_schema.get("properties") or {}).keys())
            selected = set(sql_info.get("selected_columns") or [])
            Reviewer._check("records" in data_props, "READ_OUTPUT_MISSING_RECORDS", "Read output_schema.data must contain records.", errors, required_changes)
            Reviewer._check(record_props.issubset(selected), "READ_OUTPUT_SQL_MISMATCH", "Read output fields must be selected by SQL.", errors, required_changes)
            Reviewer._check(plan_output_names.issubset(selected | {"success", "message"}), "PLAN_OUTPUT_SQL_MISMATCH", "tool_plan.output_fields must match selected SQL columns.", errors, required_changes)
        elif statement in {"INSERT", "UPDATE", "DELETE"}:
            allowed = set(Reviewer.MUTATION_OUTPUT_FIELDS)
            allowed.update(plan_output_names)
            returned = set(sql_info.get("returning_columns") or [])
            Reviewer._check("affected_count" in data_props, "MUTATION_OUTPUT_MISSING_AFFECTED_COUNT", "Mutation output_schema.data must include affected_count.", errors, required_changes)
            Reviewer._check("records" not in data_props, "MUTATION_OUTPUT_HAS_RECORDS", "Mutation output_schema.data must not return unrelated records.", errors, required_changes)
            Reviewer._check(data_props.issubset(allowed), "MUTATION_OUTPUT_SCHEMA_MISMATCH", "Mutation output_schema.data fields must match mutation execution.", errors, required_changes)
            mutation_metadata = {"affected_count", "idempotency_key", "provider_reference", "created_id", "updated_id", "deleted_id"}
            if plan.get("operation_type") in {"bulk_update", "workflow_bulk_update"}:
                mutation_metadata.add("affected_count_preview")
            missing_returning = sorted(plan_output_names - returned - mutation_metadata)
            Reviewer._check(not missing_returning, "MUTATION_PLAN_OUTPUT_MISMATCH", "Mutation tool_plan.output_fields must match SQL RETURNING fields or mutation metadata.", errors, required_changes)
            Reviewer._check(not missing_returning, "OUTPUT_SQL_RETURNING_MISMATCH", f"SQL RETURNING is missing fields declared by the tool plan: {', '.join(missing_returning)}.", errors, required_changes)

    @staticmethod
    def _validate_database_resource_subset(
        sql: str,
        sql_info: dict[str, Any],
        retrieval: dict[str, Any],
        verified_context: dict[str, Any],
        errors: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        verified_resources = {item.get("name"): item for item in retrieval.get("verified_resources") or [] if isinstance(item, dict) and item.get("name")}
        verified_tables = set(verified_resources)
        sql_tables = set(sql_info.get("tables") or [])
        Reviewer._check(sql_tables.issubset(verified_tables), "SQL_UNVERIFIED_TABLE_USED", "SQL referenced an unverified table.", errors, required_changes)
        table_columns = {table.get("fqn"): {column.get("name") for column in table.get("columns") or []} for table in verified_context.get("tables") or [] if table.get("fqn")}
        referenced_columns: dict[str, set[str]] = {}
        for table_name, column_name in re.findall(r"([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\.([a-zA-Z_][\w]*)", sql or ""):
            referenced_columns.setdefault(table_name, set()).add(column_name)
        for table_name, columns in referenced_columns.items():
            if table_name not in table_columns:
                Reviewer._check(False, "SQL_UNVERIFIED_TABLE_USED", f"SQL used table {table_name} outside verified_resources.", errors, required_changes)
                continue
            unknown = sorted(columns - table_columns[table_name])
            Reviewer._check(not unknown, "SQL_UNVERIFIED_COLUMN_USED", f"SQL used unverified columns for {table_name}: {', '.join(unknown)}.", errors, required_changes)

    @staticmethod
    def _validate_pii_output_schema(tool: dict[str, Any], errors: list[dict[str, str]], required_changes: list[str]) -> None:
        output_schema = tool.get("output_schema") or {}
        data_schema = (output_schema.get("properties") or {}).get("data") or {}
        record_schema = (((data_schema.get("properties") or {}).get("records") or {}).get("items") or {})
        record_props = set((record_schema.get("properties") or {}).keys())
        if not record_props:
            return
        field_policy = ((tool.get("safety") or {}).get("pii_policy") or {}).get("field_policy") or {}
        masked_raw_fields = set()
        for field_name, policy in field_policy.items():
            if not isinstance(policy, dict):
                continue
            if policy.get("redaction") not in {"mask_email", "phone_last4", "address_summary"}:
                continue
            raw_name = str(field_name).split(".")[-1]
            if raw_name == policy.get("output_name"):
                continue
            masked_raw_fields.add(raw_name)
        exposed = sorted(masked_raw_fields & record_props)
        Reviewer._check(
            not exposed,
            "PII_OUTPUT_SCHEMA_MISMATCH",
            f"output_schema exposes raw PII fields that policy requires masking: {', '.join(exposed)}.",
            errors,
            required_changes,
        )

    @staticmethod
    def _validate_pii_policy_scope(tool: dict[str, Any], plan: dict[str, Any], errors: list[dict[str, str]], required_changes: list[str]) -> None:
        field_policy = ((tool.get("safety") or {}).get("pii_policy") or {}).get("field_policy") or {}
        exact_resources = set(plan.get("exact_resources_used") or [])
        if not isinstance(field_policy, dict) or not exact_resources:
            return
        unused = []
        for field_name in field_policy:
            parts = str(field_name).split(".")
            if len(parts) < 3:
                continue
            resource = ".".join(parts[:2])
            if resource not in exact_resources:
                unused.append(str(field_name))
        Reviewer._check(
            not unused,
            "TOOL_PII_POLICY_UNUSED_RESOURCE",
            f"PII policy contains fields outside trusted_resources_used: {', '.join(sorted(unused))}.",
            errors,
            required_changes,
        )

    @staticmethod
    def _enforce_validation_check_consistency(errors: list[dict[str, str]], checks: dict[str, str]) -> None:
        codes = {item.get("code") for item in errors}
        contradiction = False
        if "SQL_SYNTAX_INVALID" in codes:
            if checks.get("sql_parser") == "passed":
                contradiction = True
            checks["sql_parser"] = "failed"
            if checks.get("sql_explain") == "passed":
                contradiction = True
            checks["sql_explain"] = "failed"
        if "SQL_EXPLAIN_FAILED" in codes:
            if checks.get("sql_explain") == "passed":
                contradiction = True
            checks["sql_explain"] = "failed"
        if contradiction:
            errors.append(
                {
                    "code": "VALIDATION_CHECK_STATUS_CONTRADICTION",
                    "message": "Validation checks contradicted validation errors and were normalized to failed.",
                }
            )

    @staticmethod
    def _validate_insert_required_columns(
        sql_info: dict[str, Any],
        verified_context: dict[str, Any],
        errors: list[dict[str, str]],
        required_changes: list[str],
    ) -> None:
        if sql_info.get("statement") != "INSERT" or not sql_info.get("table"):
            return
        table = next((item for item in verified_context.get("tables") or [] if item.get("fqn") == sql_info["table"]), None)
        if not table:
            return
        primary_key = table.get("primary_key") or []
        primary_keys = {primary_key} if isinstance(primary_key, str) else set(primary_key)
        provided = set(sql_info.get("insert_columns") or [])
        missing = []
        for column in table.get("columns") or []:
            name = column.get("name")
            if not name or name in primary_keys:
                continue
            if column.get("nullable") is False and not column.get("default") and not column.get("has_default") and not column.get("autoincrement"):
                if name not in provided:
                    missing.append(name)
        Reviewer._check(
            not missing,
            "INSERT_MISSING_REQUIRED_COLUMNS",
            f"INSERT is missing required non-null columns from schema metadata: {', '.join(sorted(missing))}.",
            errors,
            required_changes,
        )
