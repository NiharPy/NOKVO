from __future__ import annotations

import re
from typing import Any

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
        mapping = execution.get("mapping") or {}
        errors: list[dict[str, str]] = []
        warnings: list[dict[str, str]] = []
        required_changes: list[str] = []
        checks: dict[str, str] = {}

        Reviewer._check(bool(tool.get("name")), "MISSING_TOOL_NAME", "Tool name is required.", errors, required_changes)
        Reviewer._check(Reviewer._snake_case(tool.get("name")), "BAD_TOOL_NAME", "Tool name must be action-based snake_case.", errors, required_changes)
        Reviewer._check(
            not Reviewer._contains_fallback_text(tool),
            "FALLBACK_TITLE_DESCRIPTION",
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

        if tool.get("integration_type") == "database":
            sql = str(mapping.get("sql_template") or "")
            allowed_tables = set(mapping.get("allowed_tables") or [])
            verified_tables = set(verified_context.get("allowed_tables") or [])
            sql_validation = SQLValidator.validate(sql, str(tool.get("provider") or ""), mapping)
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
                "TABLE_NAME_ONLY_TOOL_NAME",
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
                    "SQL_STATEMENT_BLOCKED",
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
            Reviewer._validate_database_outputs(output_schema, plan, sql_info, verified_context, errors, required_changes)
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
            Reviewer._check(allowed_tables.issubset(verified_tables), "UNVERIFIED_TABLE", "allowed_tables must be verified by scanned schema context.", errors, required_changes)
            Reviewer._check(bool(mapping.get("allowed_columns")), "MISSING_ALLOWED_COLUMNS", "Database tools must define allowed_columns.", errors, required_changes)
            Reviewer._check(bool(mapping.get("explain_validation", {}).get("required_before_publish")), "MISSING_EXPLAIN_VALIDATION", "Database SQL requires EXPLAIN validation before publish.", errors, required_changes)
        else:
            Reviewer._check(bool(mapping.get("endpoint") or mapping.get("connector_action")), "MISSING_VERIFIED_CONNECTOR_ACTION", "API tools must use verified connector templates/actions.", errors, required_changes)
            Reviewer._check(mapping.get("tenant_secret_ref") == "tenant_provider_connection_secret_ref", "BAD_SECRET_REFERENCE", "API tools must reference tenant secrets only.", errors, required_changes)

        Reviewer._check(tool.get("review", {}).get("required") is True, "REVIEW_NOT_REQUIRED", "All generated tools require admin review.", errors, required_changes)
        Reviewer._check(tool.get("tests", {}).get("test_run_required") is True, "TEST_RUN_NOT_REQUIRED", "test_run_required must be true.", errors, required_changes)
        Reviewer._check(bool(tool.get("safety", {}).get("pii_policy")), "MISSING_PII_POLICY", "PII policy is required.", errors, required_changes)
        Reviewer._validate_pii_output_schema(tool, errors, required_changes)
        Reviewer._check(bool(tool.get("safety", {}).get("audit_policy")), "MISSING_AUDIT_POLICY", "Audit policy is required.", errors, required_changes)
        if not plan.get("exact_resources_used"):
            errors.append({"code": "MISSING_VERIFIED_CONTEXT", "message": "No verified resource is available for this tool."})
            required_changes.append("Rescan or select integration resources before generating this tool.")

        status = "failed" if errors else "passed"
        review_status = "rejected" if errors else "draft"
        return {
            "validation": {"status": status, "errors": errors, "warnings": warnings, "checks": checks},
            "review": {
                "status": review_status,
                "required": True,
                "reviewer_role": "admin",
                "reason": "Automatic reviewer rejected unsafe or incomplete draft." if errors else "Draft passed automatic checks and awaits admin review.",
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
    def _statement_list(value: Any) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip().upper() for item in value if str(item).strip()]

    @staticmethod
    def _parse_database_sql(sql: str) -> dict[str, Any]:
        cleaned = SQLValidator.clean_sql(sql).rstrip(";")
        first = SQLValidator._split_statements(cleaned)[0] if SQLValidator._split_statements(cleaned) else ""
        token = SQLValidator.first_token(first)
        statement = "SELECT" if token == "WITH" and SQLValidator.main_select_sql(first) else token if token in {"SELECT", "INSERT", "UPDATE", "DELETE"} else ""
        main_select = SQLValidator.main_select_sql(first) if statement == "SELECT" else first
        table = ""
        tables: list[str] = []
        insert_columns: list[str] = []
        selected_columns: list[str] = []
        updated_columns: list[str] = []
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
        elif statement == "UPDATE":
            table_match = re.search(r"\bUPDATE\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\s+SET\s+(.+?)(?:\s+WHERE\s+|$)", first, flags=re.IGNORECASE | re.DOTALL)
            if table_match:
                table = table_match.group(1)
                tables = [table]
                updated_columns = [Reviewer._clean_column_name(item.split("=", 1)[0]) for item in table_match.group(2).split(",") if Reviewer._clean_column_name(item.split("=", 1)[0])]
        elif statement == "DELETE":
            table_match = re.search(r"\bDELETE\s+FROM\s+([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\b", first, flags=re.IGNORECASE)
            table = table_match.group(1) if table_match else ""
            tables = [table] if table else []
        params = sorted(set(re.findall(r"(?<!:):([a-zA-Z_][\w]*)\b", first)))
        return {
            "statement": statement,
            "table": table,
            "tables": tables or ([table] if table else []),
            "params": params,
            "insert_columns": insert_columns,
            "selected_columns": selected_columns,
            "updated_columns": updated_columns,
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
        unused = sorted(properties - sql_params - allowed_non_sql)
        missing = sorted(sql_params - properties)
        Reviewer._check(not unused, "UNUSED_INPUT_FIELDS", f"Input fields are not used by SQL/API mapping: {', '.join(unused)}.", errors, required_changes)
        Reviewer._check(not missing, "SQL_PARAM_MISSING_INPUT", f"SQL parameters are missing from input_schema: {', '.join(missing)}.", errors, required_changes)

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
        if statement == "SELECT":
            record_schema = (((data_schema.get("properties") or {}).get("records") or {}).get("items") or {})
            record_props = set((record_schema.get("properties") or {}).keys())
            selected = set(sql_info.get("selected_columns") or [])
            Reviewer._check("records" in data_props, "READ_OUTPUT_MISSING_RECORDS", "Read output_schema.data must contain records.", errors, required_changes)
            Reviewer._check(record_props.issubset(selected), "READ_OUTPUT_SQL_MISMATCH", "Read output fields must be selected by SQL.", errors, required_changes)
            Reviewer._check(plan_output_names.issubset(selected | {"success", "message"}), "PLAN_OUTPUT_SQL_MISMATCH", "tool_plan.output_fields must match selected SQL columns.", errors, required_changes)
        elif statement in {"INSERT", "UPDATE", "DELETE"}:
            allowed = set(Reviewer.MUTATION_OUTPUT_FIELDS)
            Reviewer._check("affected_count" in data_props, "MUTATION_OUTPUT_MISSING_AFFECTED_COUNT", "Mutation output_schema.data must include affected_count.", errors, required_changes)
            Reviewer._check("records" not in data_props, "MUTATION_OUTPUT_HAS_RECORDS", "Mutation output_schema.data must not return unrelated records.", errors, required_changes)
            Reviewer._check(data_props.issubset(allowed), "MUTATION_OUTPUT_SCHEMA_MISMATCH", "Mutation output_schema.data fields must match mutation execution.", errors, required_changes)
            Reviewer._check(plan_output_names.issubset(allowed | {"success", "message"}), "MUTATION_PLAN_OUTPUT_MISMATCH", "Mutation tool_plan.output_fields must not contain unrelated table fields.", errors, required_changes)

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
