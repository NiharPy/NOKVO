from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import re
from typing import Any
from urllib import error as urllib_error
from urllib import parse as urllib_parse
from urllib import request as urllib_request
import uuid

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


TOOLKIT_SYSTEM_PROMPT = """You are an intelligent MCP tool creator for NOKVO agents.

You only create tools from the integration context supplied in this request. The context is retrieved from the tenant's indexed integration embeddings and stored integration snapshots. Do not invent external APIs, credentials, hidden data, or tools outside the selected integration.

Return a single valid JSON object only. No markdown. No prose outside JSON. The JSON must match:
{
  "name": "snake_case_tool_name",
  "title": "Human readable title",
  "description": "What the tool does",
  "integration_type": "crm|zoho_desk|erp|shipping|database",
  "provider": "provider name",
  "mcp": {
    "server": "tenant-integration-mcp",
    "tool_name": "snake_case_tool_name",
    "transport": "stdio-or-http-compatible",
    "registry_scope": "organization_integration"
  },
  "input_schema": {
    "type": "object",
    "properties": {
      "example_parameter": {
        "type": "string",
        "description": "Concrete input needed by the selected integration action"
      }
    },
    "required": []
  },
  "output_schema": {
    "type": "object",
    "properties": {
      "success": {"type": "boolean"},
      "data": {"type": "object"},
      "message": {"type": "string"}
    },
    "required": ["success"]
  },
  "execution": {
    "type": "integration_action|database_sql",
    "mode": "read_only|write_requires_admin_approval|required_review",
    "mapping": {}
  },
  "execution_plan": [
    "Step-by-step plan using only available integration actions"
  ],
  "source_context": [
    "Short references to the relevant modules/actions used"
  ],
  "safety_notes": [
    "Validation, permission, or review notes"
  ]
}

Prefer narrow tools that do one operational task well. If the prompt asks for a tool that cannot be created from the supplied context, return a JSON object with name "unsupported_tool_request" and explain why in description and safety_notes.

Database tools may be read-only or write-capable. Read-only tools should use SELECT. Write-capable database tools may use only parameterized INSERT, UPDATE, or DELETE and must set execution.mode to "write_requires_admin_approval". Never generate DROP, ALTER, CREATE, TRUNCATE, GRANT, REVOKE, COPY, EXECUTE, CALL, MERGE, REPLACE, VACUUM, ANALYZE, COMMIT, ROLLBACK, or transaction-control SQL. If the request needs destructive schema/admin SQL, return unsupported_tool_request.

Hard rules:
1. Generate specific, action-based snake_case tool names.
2. The name, title, description, input_schema, output_schema, execution plan, and execution mapping must all describe the same capability.
3. Never generate empty input_schema properties unless the tool truly requires no input.
4. Every input referenced by execution logic must exist in input_schema.
5. Every generated tool must include a structured output_schema. Avoid generic data object unless the output is genuinely unknown.
6. Database tools are read-only by default. Generate write-capable database tools only when the user explicitly asks for insert, update, or delete.
7. Database tools must use parameterized SQL only.
8. Never use SELECT *.
9. Never use dynamic table names from user input.
10. Every SQL query must include LIMIT or a hard execution limit.
11. Use fully qualified schema.table names consistently.
12. Use only tables and columns present in the indexed schema context.
13. Include allowed_tables and allowed_columns.
14. Include relationship discovery with join keys and confidence scores.
15. Include search_strategy for lookup tools, including match priority, normalization, and confidence scoring.
16. Include limits: max_entity_matches, max_rows_per_table, max_total_rows, timeout_seconds, and max_response_size_bytes.
17. Include tenant isolation rules using only the current tenant's stored integration secret reference.
18. Never include raw credentials, connection strings, API keys, tokens, or secrets.
19. Include field-level PII classification and redaction policy.
20. Include role-based permissions.
21. Include audit logging metadata.
22. Include pre-execution validation rules.
23. Include safe error handling with structured error codes.
24. Include test cases for valid, invalid, edge, and security scenarios.
25. Generated tools must default to draft status and require admin review before publishing.
26. If schema context is insufficient, return a review_required draft with missing_context listed. Do not invent tables or columns.
"""


DML_SQL_PATTERN = re.compile(r"\b(insert|update|delete)\b", flags=re.IGNORECASE)
DANGEROUS_SQL_PATTERN = re.compile(
    r"\b(drop|alter|create|truncate|grant|revoke|copy|execute|call|merge|replace|vacuum|analyze|commit|rollback)\b",
    flags=re.IGNORECASE,
)
DESTRUCTIVE_PROMPT_PATTERN = re.compile(
    r"\b("
    r"drop\s+(table|schema|database|index|view)|"
    r"alter\s+(table|schema|database|index|view)|"
    r"create\s+(table|schema|database|index|view|function|procedure|trigger)|"
    r"truncate\s+(table\s+)?[a-zA-Z_][\w.]*|"
    r"grant\s+.+\s+on\s+|"
    r"revoke\s+.+\s+on\s+|"
    r"copy\s+.+\s+(from|to)\s+|"
    r"execute\s+(function|procedure)|"
    r"call\s+(function|procedure)|"
    r"commit\s+transaction|"
    r"rollback\s+transaction|"
    r"vacuum\s+[a-zA-Z_][\w.]*|"
    r"analyze\s+[a-zA-Z_][\w.]*"
    r")\b",
    flags=re.IGNORECASE,
)


class ToolkitGeneratorService:
    @staticmethod
    def integration_registry_key(integration_type: str, provider: str) -> str:
        return f"{integration_type.strip().lower()}:{provider.strip().lower()}"

    @staticmethod
    def _normalize_tool_name(value: str) -> str:
        normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value or "").strip("_").lower()
        if not normalized:
            return f"generated_tool_{uuid.uuid4().hex[:8]}"
        if normalized[0].isdigit():
            normalized = f"tool_{normalized}"
        return normalized[:64]

    @staticmethod
    def _default_input_schema(integration_type: str, nlp_prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        if integration_type == "database":
            write_requested = ToolkitGeneratorService._database_write_requested(nlp_prompt)
            return {
                "type": "object",
                "properties": {
                    "values": {
                        "type": "object",
                        "description": "Parameterized values for a reviewed database write operation." if write_requested else "Optional values used by the reviewed read query.",
                        "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
                    },
                    "filters": {
                        "type": "object",
                        "description": "Optional filters to apply to the reviewed database operation.",
                        "additionalProperties": {"type": ["string", "number", "boolean", "null"]},
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum rows to return.",
                        "minimum": 1,
                        "maximum": 100,
                        "default": 25,
                    },
                },
                "required": [],
                "additionalProperties": False,
            }

        action_payload = next(
            (
                item.get("payload", {})
                for item in context.get("snapshot_context", [])
                if isinstance(item.get("payload"), dict) and item.get("payload", {}).get("name")
            ),
            {},
        )
        action_name = action_payload.get("name") or "integration_action"
        return {
            "type": "object",
            "properties": {
                "parameters": {
                    "type": "object",
                    "description": f"Validated parameters for {action_name}.",
                    "additionalProperties": True,
                }
            },
            "required": ["parameters"],
            "additionalProperties": False,
        }

    @staticmethod
    def _default_output_schema(integration_type: str) -> dict[str, Any]:
        data_description = "Rows affected or rows returned by the reviewed database operation." if integration_type == "database" else "Provider response data."
        return {
            "type": "object",
            "properties": {
                "success": {"type": "boolean"},
                "data": {"type": "array" if integration_type == "database" else "object", "description": data_description},
                "message": {"type": "string"},
                "metadata": {
                    "type": "object",
                    "properties": {
                        "integration_type": {"type": "string"},
                        "provider": {"type": "string"},
                        "tool_name": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
            "required": ["success", "data"],
            "additionalProperties": False,
        }

    @staticmethod
    def _database_write_requested(text: str) -> bool:
        return bool(DML_SQL_PATTERN.search(text or ""))

    @staticmethod
    def _database_dangerous_requested(text: str) -> bool:
        return bool(DESTRUCTIVE_PROMPT_PATTERN.search(text or ""))

    @staticmethod
    def _database_sql_template_dangerous(text: str) -> bool:
        return bool(DANGEROUS_SQL_PATTERN.search(text or ""))

    @staticmethod
    def _database_schema_context(context: dict[str, Any]) -> dict[str, Any]:
        tables: dict[str, dict[str, Any]] = {}
        for item in context.get("snapshot_context", []):
            payload = item.get("payload", {})
            if not isinstance(payload, dict):
                continue
            source = item.get("source")
            if source == "provider_status.db_schema_snapshot":
                schema = str(payload.get("schema") or "public")
                table_name = payload.get("name") or payload.get("table")
                raw_columns = payload.get("columns") or []
            elif source == "provider_status.db_selected_sources":
                schema = str(payload.get("schema") or "public")
                table_name = payload.get("table") or payload.get("name")
                raw_columns = payload.get("columns") or []
            else:
                continue
            if not table_name:
                continue
            fqn = f"{schema}.{table_name}"
            table = tables.setdefault(
                fqn,
                {
                    "schema": schema,
                    "table": str(table_name),
                    "fqn": fqn,
                    "columns": {},
                },
            )
            for column in raw_columns:
                if isinstance(column, dict):
                    column_name = column.get("name")
                    column_type = column.get("type") or column.get("data_type") or "unknown"
                    nullable = column.get("nullable")
                else:
                    column_name = column
                    column_type = "unknown"
                    nullable = None
                if not column_name:
                    continue
                table["columns"][str(column_name)] = {
                    "name": str(column_name),
                    "type": str(column_type),
                    "nullable": nullable,
                    "fqn": f"{fqn}.{column_name}",
                }

        table_list = []
        for table in tables.values():
            table_list.append(
                {
                    **table,
                    "columns": list(table["columns"].values()),
                }
            )
        table_list.sort(key=lambda item: item["fqn"])
        return {
            "tables": table_list,
            "allowed_tables": [table["fqn"] for table in table_list],
            "allowed_columns": {
                table["fqn"]: [column["name"] for column in table["columns"]]
                for table in table_list
            },
        }

    @staticmethod
    def _pii_classification(column_name: str, column_type: str = "") -> str:
        value = column_name.lower()
        if any(token in value for token in ["email"]):
            return "email"
        if any(token in value for token in ["phone", "mobile", "msisdn"]):
            return "phone"
        if any(token in value for token in ["address", "street", "city", "state", "zip", "postal"]):
            return "address"
        if any(token in value for token in ["name", "full_name", "first_name", "last_name"]):
            return "person_name"
        if any(token in value for token in ["token", "secret", "password", "key"]):
            return "secret"
        if value.endswith("_id") or value == "id":
            return "identifier"
        if any(token in value for token in ["amount", "price", "total", "balance"]):
            return "financial"
        return "none"

    @staticmethod
    def _database_relationships(schema_context: dict[str, Any]) -> list[dict[str, Any]]:
        relationships: list[dict[str, Any]] = []
        tables = schema_context.get("tables", [])
        for left in tables:
            left_columns = {column["name"]: column for column in left.get("columns", [])}
            for right in tables:
                if left["fqn"] >= right["fqn"]:
                    continue
                right_columns = {column["name"]: column for column in right.get("columns", [])}
                for column_name in sorted(set(left_columns) & set(right_columns)):
                    if not (column_name == "id" or column_name.endswith("_id")):
                        continue
                    relationships.append(
                        {
                            "left": f"{left['fqn']}.{column_name}",
                            "right": f"{right['fqn']}.{column_name}",
                            "join": f"{left['fqn']}.{column_name} = {right['fqn']}.{column_name}",
                            "confidence": 0.72 if column_name.endswith("_id") else 0.55,
                            "reason": "matching identifier column names in indexed schema",
                        }
                    )
                left_id = f"{left['table'].rstrip('s')}_id"
                right_id = f"{right['table'].rstrip('s')}_id"
                if left_id in right_columns and "id" in left_columns:
                    relationships.append(
                        {
                            "left": f"{left['fqn']}.id",
                            "right": f"{right['fqn']}.{left_id}",
                            "join": f"{left['fqn']}.id = {right['fqn']}.{left_id}",
                            "confidence": 0.86,
                            "reason": "foreign-key-like column matches referenced table name",
                        }
                    )
                if right_id in left_columns and "id" in right_columns:
                    relationships.append(
                        {
                            "left": f"{left['fqn']}.{right_id}",
                            "right": f"{right['fqn']}.id",
                            "join": f"{left['fqn']}.{right_id} = {right['fqn']}.id",
                            "confidence": 0.86,
                            "reason": "foreign-key-like column matches referenced table name",
                        }
                    )
        unique: dict[str, dict[str, Any]] = {}
        for relation in relationships:
            unique.setdefault(relation["join"], relation)
        return list(unique.values())[:20]

    @staticmethod
    def _database_search_strategy(schema_context: dict[str, Any], input_schema: dict[str, Any], nlp_prompt: str) -> dict[str, Any]:
        searchable = []
        for table in schema_context.get("tables", []):
            for column in table.get("columns", []):
                name = column["name"].lower()
                if any(token in name for token in ["name", "email", "phone", "mobile", "sku", "order", "ticket", "status"]):
                    searchable.append(f"{table['fqn']}.{column['name']}")
        input_fields = list((input_schema.get("properties") or {}).keys())
        return {
            "type": "lookup" if re.search(r"\b(query|search|find|lookup|get|fetch)\b", nlp_prompt, re.IGNORECASE) else "operation",
            "input_fields": input_fields,
            "match_priority": [
                "exact identifier match",
                "exact normalized email or phone match",
                "case-insensitive exact text match",
                "prefix text match",
                "bounded partial text match",
            ],
            "normalization": [
                "trim whitespace",
                "lowercase text comparisons",
                "remove non-digits for phone comparisons",
                "reject empty filters",
            ],
            "confidence_scoring": {
                "exact_identifier": 1.0,
                "exact_email_or_phone": 0.95,
                "exact_text": 0.85,
                "prefix_text": 0.7,
                "partial_text": 0.55,
            },
            "searchable_columns": searchable[:30],
        }

    @staticmethod
    def _database_select_template(schema_context: dict[str, Any], input_schema: dict[str, Any]) -> str:
        tables = schema_context.get("tables", [])
        if not tables:
            return ""
        primary = tables[0]
        selected_columns = primary.get("columns", [])[:8]
        if not selected_columns:
            return ""
        select_list = ", ".join(f"{primary['fqn']}.{column['name']}" for column in selected_columns)
        filter_fields = [
            key
            for key in (input_schema.get("properties") or {}).keys()
            if key not in {"limit", "filters", "values"}
        ]
        if filter_fields:
            where_clause = " WHERE " + " AND ".join(f"/* map :{field} to an allowed column */ TRUE" for field in filter_fields)
        else:
            where_clause = ""
        return f"SELECT {select_list} FROM {primary['fqn']}{where_clause} LIMIT :limit"

    @staticmethod
    def _sanitize_database_sql_template(sql_template: str, schema_context: dict[str, Any], input_schema: dict[str, Any]) -> str:
        sql_template = (sql_template or "").strip()
        if not sql_template:
            return ToolkitGeneratorService._database_select_template(schema_context, input_schema)
        if ToolkitGeneratorService._database_sql_template_dangerous(sql_template):
            return ToolkitGeneratorService._database_select_template(schema_context, input_schema)
        if re.search(r"select\s+\*", sql_template, flags=re.IGNORECASE):
            return ToolkitGeneratorService._database_select_template(schema_context, input_schema)
        referenced_tables = set(re.findall(r"\b([a-zA-Z_][\w]*\.[a-zA-Z_][\w]*)\b", sql_template))
        allowed_tables = set(schema_context.get("allowed_tables", []))
        if referenced_tables and not referenced_tables.issubset(allowed_tables):
            return ToolkitGeneratorService._database_select_template(schema_context, input_schema)
        if sql_template.lower().lstrip().startswith("select") and not re.search(r"\blimit\b", sql_template, flags=re.IGNORECASE):
            sql_template = f"{sql_template.rstrip(';')} LIMIT :limit"
        return sql_template

    @staticmethod
    def _field_pii_policy(integration_type: str, schema_context: dict[str, Any] | None = None) -> dict[str, Any]:
        fields: dict[str, dict[str, str]] = {}
        for table in (schema_context or {}).get("tables", []):
            for column in table.get("columns", []):
                classification = ToolkitGeneratorService._pii_classification(column["name"], column.get("type", ""))
                fields[column["fqn"]] = {
                    "classification": classification,
                    "redaction": "mask" if classification not in {"none", "identifier"} else "none",
                }
        return {
            "default": "mask secrets and direct identifiers before returning output",
            "fields": fields,
            "never_return": ["password", "secret", "token", "api_key", "connection_string", "refresh_token", "access_token"],
        }

    @staticmethod
    def _default_permissions(execution_mode: str) -> dict[str, Any]:
        return {
            "roles_allowed": ["admin"] if execution_mode == "write_requires_admin_approval" else ["admin", "manager"],
            "requires_admin_review": True,
            "requires_execution_confirmation": execution_mode == "write_requires_admin_approval",
        }

    @staticmethod
    def _default_validation(input_schema: dict[str, Any], integration_type: str) -> list[dict[str, Any]]:
        fields = list((input_schema.get("properties") or {}).keys())
        rules = [
            {"code": "VALIDATE_SCHEMA", "description": "Input must match input_schema before execution."},
            {"code": "VALIDATE_TENANT_SECRET", "description": "Use only the current tenant's stored integration secret reference."},
        ]
        if fields:
            rules.append({"code": "VALIDATE_NON_EMPTY_INPUT", "description": f"At least one meaningful input field must be supplied from: {', '.join(fields)}."})
        if integration_type == "database":
            rules.extend(
                [
                    {"code": "VALIDATE_SQL_PARAMETERIZED", "description": "SQL must use bound parameters only; never interpolate raw user input."},
                    {"code": "VALIDATE_SQL_ALLOWED_TABLES", "description": "SQL may reference only allowed schema.table names and columns."},
                    {"code": "VALIDATE_SQL_LIMIT", "description": "Read queries must include LIMIT or be capped by hard execution limits."},
                ]
            )
        return rules

    @staticmethod
    def _default_error_handling() -> dict[str, Any]:
        return {
            "format": {
                "success": False,
                "error": {
                    "code": "STRING_CODE",
                    "message": "Safe user-facing message",
                    "retryable": False,
                },
            },
            "codes": [
                "VALIDATION_ERROR",
                "PERMISSION_DENIED",
                "TENANT_SECRET_NOT_FOUND",
                "INTEGRATION_UNAVAILABLE",
                "RATE_LIMITED",
                "TIMEOUT",
                "NO_MATCH",
                "TOO_MANY_MATCHES",
                "POLICY_VIOLATION",
            ],
        }

    @staticmethod
    def _default_test_cases(input_schema: dict[str, Any], execution_mode: str) -> list[dict[str, Any]]:
        fields = list((input_schema.get("properties") or {}).keys())
        sample_input = {field: f"sample_{field}" for field in fields[:2]}
        if "limit" in fields:
            sample_input["limit"] = 10
        return [
            {"name": "valid_minimal_input", "type": "valid", "input": sample_input, "expected": "success or controlled no-match response"},
            {"name": "missing_required_or_empty_input", "type": "invalid", "input": {}, "expected_error": "VALIDATION_ERROR"},
            {"name": "edge_limit_boundary", "type": "edge", "input": {**sample_input, "limit": 1}, "expected": "bounded response"},
            {"name": "security_injection_attempt", "type": "security", "input": {field: "'; DROP TABLE users; --" for field in fields[:1]}, "expected_error": "POLICY_VIOLATION"},
            {"name": "permission_check", "type": "security", "input": sample_input, "expected_error": "PERMISSION_DENIED" if execution_mode == "write_requires_admin_approval" else "success for allowed roles"},
        ]

    @staticmethod
    def _redact_sensitive_values(value: Any) -> Any:
        if isinstance(value, dict):
            redacted = {}
            for key, item in value.items():
                if any(token in str(key).lower() for token in ["password", "secret", "token", "api_key", "apikey", "connection_string", "credential"]):
                    redacted[key] = "[redacted]"
                else:
                    redacted[key] = ToolkitGeneratorService._redact_sensitive_values(item)
            return redacted
        if isinstance(value, list):
            return [ToolkitGeneratorService._redact_sensitive_values(item) for item in value]
        return value

    @staticmethod
    def _ensure_parameter_sources_exist(input_schema: dict[str, Any], execution: dict[str, Any]) -> None:
        properties = input_schema.setdefault("properties", {})
        for source in ((execution.get("mapping") or {}).get("parameter_sources") or []):
            if not isinstance(source, str) or not source.startswith("input."):
                continue
            field = source.split(".", 1)[1].split(".", 1)[0]
            if field and field not in properties:
                properties[field] = {
                    "type": "object",
                    "description": f"Parameters referenced by execution mapping source {source}.",
                    "additionalProperties": True,
                }

    @staticmethod
    def _database_execution_mapping(nlp_prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        write_requested = ToolkitGeneratorService._database_write_requested(nlp_prompt)
        schema_context = ToolkitGeneratorService._database_schema_context(context)
        input_schema = ToolkitGeneratorService._default_input_schema("database", nlp_prompt, context)
        return {
            "type": "database_sql",
            "mode": "write_requires_admin_approval" if write_requested else "read_only",
            "mapping": {
                "sql_template": (
                    "/* Admin must review and replace with parameterized INSERT/UPDATE/DELETE over allowed schema.table and columns only. */"
                    if write_requested
                    else ToolkitGeneratorService._database_select_template(schema_context, input_schema)
                ),
                "allowed_statements": ["INSERT", "UPDATE", "DELETE"] if write_requested else ["SELECT"],
                "blocked_statements": [
                    "DROP",
                    "ALTER",
                    "CREATE",
                    "TRUNCATE",
                    "GRANT",
                    "REVOKE",
                    "COPY",
                    "EXECUTE",
                    "CALL",
                    "MERGE",
                    "REPLACE",
                    "VACUUM",
                    "ANALYZE",
                    "COMMIT",
                    "ROLLBACK",
                ],
                "allowed_tables": schema_context["allowed_tables"],
                "allowed_columns": schema_context["allowed_columns"],
                "relationships": ToolkitGeneratorService._database_relationships(schema_context),
                "search_strategy": ToolkitGeneratorService._database_search_strategy(schema_context, input_schema, nlp_prompt),
                "parameter_sources": ["input.values", "input.filters", "input.limit"],
                "tenant_isolation": "Use only the tenant's stored database connection secret and selected schema context.",
                "pii_redaction": "Mask email, phone, address, token, key, and secret-like values before returning data.",
                "requires_admin_confirmation": write_requested,
                "test_run_required": write_requested,
                "dry_run_strategy": "Validate SQL statement type, allowed tables, parameters, and estimated affected rows before execution." if write_requested else "Run with LIMIT and no mutation.",
                "limits": {
                    "max_entity_matches": 10,
                    "max_rows_per_table": 50,
                    "max_total_rows": 100,
                    "timeout_seconds": 10,
                    "max_response_size_bytes": 262144,
                },
            },
        }

    @staticmethod
    def _integration_execution_mapping(integration_type: str, provider: str, context: dict[str, Any]) -> dict[str, Any]:
        actions = [
            item.get("payload", {})
            for item in context.get("snapshot_context", [])
            if isinstance(item.get("payload"), dict) and item.get("payload", {}).get("name")
        ]
        if not actions:
            actions = [
                item.get("payload", {})
                for item in context.get("embedding_context", [])
                if isinstance(item.get("payload"), dict) and item.get("payload", {}).get("action")
            ]
        first_action = actions[0] if actions else {}
        action_name = str(first_action.get("name") or first_action.get("action") or "selected_integration_action")
        method = str(first_action.get("method") or "").upper()
        write_capable = method not in {"", "GET", "READ"} or bool(re.search(r"\b(create|update|delete|assign|generate|import|post|patch)\b", action_name, flags=re.IGNORECASE))
        return {
            "type": "integration_action",
            "mode": "write_requires_admin_approval" if write_capable else "required_review",
            "mapping": {
                "provider": provider,
                "action": action_name,
                "method": first_action.get("method"),
                "endpoint": first_action.get("endpoint"),
                "tenant_isolation": "Use only the tenant-scoped connection secret for this integration.",
                "pii_redaction": "Mask email, phone, address, token, key, and secret-like values unless explicitly required.",
                "requires_admin_confirmation": write_capable,
                "test_run_required": write_capable,
            },
        }

    @staticmethod
    def _sanitize_schema(schema: Any, fallback: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(schema, dict) or schema.get("type") != "object":
            return fallback
        properties = schema.get("properties")
        if not isinstance(properties, dict) or not properties:
            return fallback
        required = schema.get("required")
        if not isinstance(required, list):
            schema["required"] = []
        schema.setdefault("additionalProperties", False)
        return schema

    @staticmethod
    def _sanitize_execution(execution: Any, integration_type: str, provider: str, nlp_prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        fallback = (
            ToolkitGeneratorService._database_execution_mapping(nlp_prompt, context)
            if integration_type == "database"
            else ToolkitGeneratorService._integration_execution_mapping(integration_type, provider, context)
        )
        if not isinstance(execution, dict):
            return fallback
        execution_type = execution.get("type")
        if integration_type == "database":
            schema_context = ToolkitGeneratorService._database_schema_context(context)
            input_schema = ToolkitGeneratorService._sanitize_schema(
                None,
                ToolkitGeneratorService._default_input_schema(integration_type, nlp_prompt, context),
            )
            mapping = execution.get("mapping") if isinstance(execution.get("mapping"), dict) else {}
            sql_template = str(mapping.get("sql_template") or "")
            if ToolkitGeneratorService._database_sql_template_dangerous(sql_template):
                return fallback
            fallback["mapping"].update({key: value for key, value in mapping.items() if value is not None})
            fallback["type"] = "database_sql"
            fallback["mapping"]["allowed_tables"] = schema_context["allowed_tables"]
            fallback["mapping"]["allowed_columns"] = schema_context["allowed_columns"]
            fallback["mapping"]["relationships"] = ToolkitGeneratorService._database_relationships(schema_context)
            fallback["mapping"]["search_strategy"] = ToolkitGeneratorService._database_search_strategy(schema_context, input_schema, nlp_prompt)
            fallback["mapping"]["sql_template"] = ToolkitGeneratorService._sanitize_database_sql_template(
                str(fallback["mapping"].get("sql_template") or ""),
                schema_context,
                input_schema,
            )
            if DML_SQL_PATTERN.search(sql_template) or ToolkitGeneratorService._database_write_requested(nlp_prompt):
                fallback["mode"] = "write_requires_admin_approval"
                fallback["mapping"]["allowed_statements"] = ["INSERT", "UPDATE", "DELETE"]
                fallback["mapping"]["requires_admin_confirmation"] = True
                fallback["mapping"]["test_run_required"] = True
            else:
                fallback["mode"] = "read_only"
                fallback["mapping"]["allowed_statements"] = ["SELECT"]
                fallback["mapping"]["requires_admin_confirmation"] = False
            return fallback
        if execution_type not in {"integration_action", "database_sql"}:
            return fallback
        execution.setdefault("mode", "required_review")
        execution.setdefault("mapping", {})
        if not isinstance(execution["mapping"], dict):
            execution["mapping"] = {}
        execution["mapping"].setdefault("provider", provider)
        execution["mapping"].setdefault("tenant_isolation", "Use only the tenant-scoped connection secret for this integration.")
        execution["mapping"].setdefault("pii_redaction", "Mask email, phone, address, token, key, and secret-like values unless explicitly required.")
        return execution

    @staticmethod
    def _sanitize_tool(tool: dict[str, Any], integration_type: str, provider: str, nlp_prompt: str, context: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(tool, dict):
            tool = {}
        name = ToolkitGeneratorService._normalize_tool_name(tool.get("name") or tool.get("title") or nlp_prompt)
        tool["name"] = name
        tool.setdefault("title", name.replace("_", " ").title())
        tool.setdefault("description", f"Generated {integration_type}/{provider} MCP tool draft.")
        tool["integration_type"] = integration_type
        tool["provider"] = provider
        tool.setdefault("mcp", {})
        tool["mcp"]["tool_name"] = ToolkitGeneratorService._normalize_tool_name(tool["mcp"].get("tool_name") or name)
        tool["mcp"].setdefault("server", "tenant-integration-mcp")
        tool["mcp"].setdefault("transport", "stdio-or-http-compatible")
        tool["mcp"].setdefault("registry_scope", "organization_integration")
        tool["input_schema"] = ToolkitGeneratorService._sanitize_schema(
            tool.get("input_schema"),
            ToolkitGeneratorService._default_input_schema(integration_type, nlp_prompt, context),
        )
        tool["output_schema"] = ToolkitGeneratorService._sanitize_schema(
            tool.get("output_schema"),
            ToolkitGeneratorService._default_output_schema(integration_type),
        )
        tool["execution"] = ToolkitGeneratorService._sanitize_execution(
            tool.get("execution"),
            integration_type,
            provider,
            nlp_prompt,
            context,
        )
        tool.setdefault("execution_plan", [])
        tool.setdefault("source_context", [])
        tool.setdefault("safety_notes", [])
        if not isinstance(tool["execution_plan"], list):
            tool["execution_plan"] = []
        if not isinstance(tool["source_context"], list):
            tool["source_context"] = []
        if not isinstance(tool["safety_notes"], list):
            tool["safety_notes"] = []
        tool["safety_notes"].extend(
            note
            for note in [
                "Admin review is required before publishing.",
                "Tool execution must stay within the selected tenant and integration.",
                "PII and secrets must be redacted from outputs unless explicitly approved.",
            ]
            if note not in tool["safety_notes"]
        )
        if integration_type == "database":
            if ToolkitGeneratorService._database_dangerous_requested(nlp_prompt):
                tool["name"] = "unsupported_tool_request"
                tool["mcp"]["tool_name"] = "unsupported_tool_request"
                tool["description"] = "Destructive database schema/admin operations are not allowed for generated tools."
                tool["execution"]["mapping"]["sql_template"] = ""
                tool["safety_notes"].append("Rejected because destructive schema/admin SQL is not allowed.")
            elif tool["name"] == "unsupported_tool_request":
                recovered_name = ToolkitGeneratorService._normalize_tool_name(tool.get("title") or nlp_prompt)
                tool["name"] = recovered_name
                tool["mcp"]["tool_name"] = recovered_name
                if not tool.get("description") or "not allowed" in str(tool.get("description")).lower():
                    tool["description"] = f"Generated database tool for {provider}."
                tool["safety_notes"] = [
                    note
                    for note in tool["safety_notes"]
                    if "Rejected because destructive schema/admin SQL is not allowed." not in note
                ]
            elif tool["execution"].get("mode") == "write_requires_admin_approval":
                tool["safety_notes"].append("Database write tools require admin approval, test-run validation, and explicit execution confirmation.")
            else:
                tool["safety_notes"].append("Database read tools are constrained to SELECT operations.")
        elif tool["execution"].get("mode") == "write_requires_admin_approval":
            tool["safety_notes"].append("Write-capable integration tools require admin approval, test-run validation, and explicit execution confirmation.")
        tool["review"] = {
            "status": "draft",
            "required": True,
            "reviewer_role": "admin",
        }
        return tool

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        match = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not match:
            raise RuntimeError("Toolkit generator did not return JSON")
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise RuntimeError("Toolkit generator returned malformed JSON") from exc

    @staticmethod
    def _extract_model_text(response: dict[str, Any]) -> str:
        if isinstance(response.get("output_text"), str) and response["output_text"].strip():
            return response["output_text"]

        output_parts: list[str] = []
        for output in response.get("output", []) or []:
            if not isinstance(output, dict):
                continue
            for content in output.get("content", []) or []:
                if not isinstance(content, dict):
                    continue
                text = content.get("text")
                if isinstance(text, str):
                    output_parts.append(text)
        if output_parts:
            return "\n".join(output_parts)

        choices = response.get("choices", []) or []
        if choices:
            message = choices[0].get("message", {}) if isinstance(choices[0], dict) else {}
            content = message.get("content")
            if isinstance(content, str):
                return content

        raise RuntimeError("Azure OpenAI response did not include text content")

    @staticmethod
    def _snapshot_context(provider_status: dict[str, Any], integration_type: str, provider: str) -> list[dict[str, Any]]:
        context: list[dict[str, Any]] = []
        if integration_type == "crm":
            context.extend(
                {"source": "provider_status.crm_schema_snapshot", "payload": item}
                for item in provider_status.get("crm_schema_snapshot", [])[:20]
            )
            context.extend(
                {"source": "provider_status.crm_action_snapshot", "payload": item}
                for item in provider_status.get("crm_action_snapshot", [])[:40]
            )
        elif integration_type == "zoho_desk":
            context.append(
                {
                    "source": "provider_status.zoho_desk",
                    "payload": {
                        "status": provider_status.get("zoho_desk_status"),
                        "account_name": provider_status.get("zoho_desk_account_name"),
                        "module_count": provider_status.get("zoho_desk_module_count"),
                        "action_count": provider_status.get("zoho_desk_action_count"),
                        "folder_path": provider_status.get("zoho_desk_folder_path"),
                    },
                }
            )
        elif integration_type == "erp":
            context.extend(
                {"source": "provider_status.erp_schema_snapshot", "payload": item}
                for item in provider_status.get("erp_schema_snapshot", [])[:20]
            )
            context.extend(
                {"source": "provider_status.erp_action_snapshot", "payload": item}
                for item in provider_status.get("erp_action_snapshot", [])[:40]
            )
        elif integration_type == "shipping":
            context.extend(
                {"source": "provider_status.shipping_schema_snapshot", "payload": item}
                for item in provider_status.get("shipping_schema_snapshot", [])[:20]
            )
            context.extend(
                {"source": "provider_status.shipping_action_snapshot", "payload": item}
                for item in provider_status.get("shipping_action_snapshot", [])[:40]
            )
        elif integration_type == "database":
            context.extend(
                {"source": "provider_status.db_schema_snapshot", "payload": item}
                for item in provider_status.get("db_schema_snapshot", [])[:20]
            )
            context.extend(
                {"source": "provider_status.db_selected_sources", "payload": item}
                for item in provider_status.get("db_selected_sources", [])[:40]
            )

        return [
            item
            for item in context
            if not provider or provider in json.dumps(item.get("payload", {}), default=str).lower() or integration_type == "database"
        ]

    @staticmethod
    async def _embedding_context(
        tenant_res: TenantResources,
        prompt: str,
        integration_type: str,
        provider: str,
    ) -> list[dict[str, Any]]:
        provider_filter = provider if integration_type not in {"zoho_desk", "database"} else None
        filters = {"integration_type": integration_type}
        if provider_filter:
            filters["provider"] = provider_filter
        try:
            query_vector = TextEmbeddingService.embed_text(prompt)
            points = await QdrantService.search_points(
                tenant_res,
                query_vector,
                limit=12,
                payload_filters=filters,
            )
        except Exception as exc:
            return [{"source": "qdrant_search_error", "payload": {"error": str(exc)}}]

        context = []
        for point in points:
            payload = getattr(point, "payload", {}) or {}
            context.append(
                {
                    "source": "qdrant",
                    "score": getattr(point, "score", None),
                    "payload": {
                        "folder_path": payload.get("folder_path"),
                        "module": payload.get("module"),
                        "action": payload.get("action"),
                        "method": payload.get("method"),
                        "endpoint": payload.get("endpoint"),
                        "text": payload.get("text"),
                    },
                }
            )
        return context

    @staticmethod
    async def build_context(
        tenant_res: TenantResources,
        integration_type: str,
        provider: str,
        prompt: str,
    ) -> dict[str, Any]:
        provider_status = dict(tenant_res.provider_status or {})
        embedding_context = await ToolkitGeneratorService._embedding_context(
            tenant_res,
            prompt,
            integration_type,
            provider,
        )
        snapshot_context = ToolkitGeneratorService._snapshot_context(provider_status, integration_type, provider)
        return {
            "tenant_id": tenant_res.tenant_id,
            "integration_type": integration_type,
            "provider": provider,
            "embedding_context": embedding_context[:12],
            "snapshot_context": snapshot_context[:30],
        }

    @staticmethod
    async def _azure_generate(messages: list[dict[str, str]]) -> dict[str, Any]:
        if not settings.AZURE_OPENAI_GLOBAL_ENDPOINT or not settings.AZURE_OPENAI_GLOBAL_API_KEY:
            raise RuntimeError(
                "Global Azure OpenAI is not configured. Set AZURE_OPENAI_GLOBAL_ENDPOINT, "
                "AZURE_OPENAI_GLOBAL_API_KEY, and AZURE_OPENAI_GLOBAL_DEPLOYMENT."
            )

        endpoint = settings.AZURE_OPENAI_GLOBAL_ENDPOINT.rstrip("/")
        deployment = settings.AZURE_OPENAI_GLOBAL_DEPLOYMENT.strip()
        if endpoint.endswith("/responses"):
            url = endpoint
            payload_variants = [
                {
                    "model": deployment,
                    "input": messages,
                    "temperature": 0.1,
                },
                {
                    "model": deployment,
                    "input": "\n\n".join(f"{message['role'].upper()}:\n{message['content']}" for message in messages),
                },
            ]
        else:
            deployment_path = urllib_parse.quote(deployment)
            api_version = urllib_parse.quote(settings.AZURE_OPENAI_GLOBAL_API_VERSION.strip())
            url = f"{endpoint}/openai/deployments/{deployment_path}/chat/completions?api-version={api_version}"
            payload_variants = [
                {
                    "messages": messages,
                    "temperature": 0.1,
                    "response_format": {"type": "json_object"},
                },
                {
                    "messages": messages,
                    "temperature": 0.1,
                },
            ]

        def _work() -> dict[str, Any]:
            last_error: RuntimeError | None = None
            for body_payload in payload_variants:
                request = urllib_request.Request(
                    url=url,
                    data=json.dumps(body_payload).encode("utf-8"),
                    method="POST",
                )
                request.add_header("Content-Type", "application/json")
                request.add_header("api-key", settings.AZURE_OPENAI_GLOBAL_API_KEY)
                try:
                    with urllib_request.urlopen(request, timeout=60) as response:
                        raw = response.read().decode("utf-8", errors="replace")
                    return json.loads(raw)
                except urllib_error.HTTPError as exc:
                    detail = exc.read().decode("utf-8", errors="replace")
                    last_error = RuntimeError(f"Azure OpenAI request failed ({exc.code}): {detail}")
                    if exc.code not in {400, 422}:
                        raise last_error from exc
                except urllib_error.URLError as exc:
                    raise RuntimeError(f"Azure OpenAI request failed: {exc.reason}") from exc
                except json.JSONDecodeError as exc:
                    raise RuntimeError("Azure OpenAI returned invalid JSON") from exc
            raise last_error or RuntimeError("Azure OpenAI request failed")

        return await asyncio.to_thread(_work)

    @staticmethod
    def _fallback_tool(
        integration_type: str,
        provider: str,
        nlp_prompt: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        action_items = [
            item.get("payload", {})
            for item in context.get("snapshot_context", [])
            if isinstance(item.get("payload"), dict) and item.get("payload", {}).get("name")
        ]
        first_action = action_items[0] if action_items else {}
        name_seed = first_action.get("name") or nlp_prompt
        return {
            "name": ToolkitGeneratorService._normalize_tool_name(name_seed),
            "title": f"{provider.title()} generated tool",
            "description": "Draft generated from indexed integration context. Configure Azure OpenAI for richer tool synthesis.",
            "integration_type": integration_type,
            "provider": provider,
            "mcp": {
                "server": "tenant-integration-mcp",
                "tool_name": ToolkitGeneratorService._normalize_tool_name(name_seed),
                "transport": "stdio-or-http-compatible",
                "registry_scope": "organization_integration",
            },
            "input_schema": {"type": "object", "properties": {}, "required": []},
            "output_schema": ToolkitGeneratorService._default_output_schema(integration_type),
            "execution": (
                ToolkitGeneratorService._database_execution_mapping(nlp_prompt, context)
                if integration_type == "database"
                else ToolkitGeneratorService._integration_execution_mapping(integration_type, provider, context)
            ),
            "execution_plan": [
                f"Use only indexed {integration_type}/{provider} context.",
                f"Requested capability: {nlp_prompt}",
            ],
            "source_context": [str(item.get("source")) for item in context.get("snapshot_context", [])[:5]],
            "safety_notes": ["Admin review is required before this tool is added to the MCP registry."],
        }

    @staticmethod
    async def generate_tool(
        tenant_res: TenantResources,
        integration_type: str,
        provider: str,
        nlp_prompt: str,
        system_prompt: str | None = None,
    ) -> dict[str, Any]:
        integration_type = integration_type.strip().lower()
        provider = provider.strip().lower()
        context = await ToolkitGeneratorService.build_context(tenant_res, integration_type, provider, nlp_prompt)
        user_payload = {
            "task": nlp_prompt,
            "selected_integration": {"integration_type": integration_type, "provider": provider},
            "available_context": context,
            "hard_rules": [
                "Use only the supplied integration context.",
                "Generate a draft MCP tool, not executable code.",
                "The admin must review and approve before registry insertion.",
            ],
        }
        messages = [
            {"role": "system", "content": system_prompt or TOOLKIT_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user_payload, default=str)},
        ]

        try:
            completion = await ToolkitGeneratorService._azure_generate(messages)
            content = ToolkitGeneratorService._extract_model_text(completion)
            tool = ToolkitGeneratorService._extract_json_object(content)
        except Exception as exc:
            tool = ToolkitGeneratorService._fallback_tool(integration_type, provider, nlp_prompt, context)
            tool["generation_warning"] = str(exc)

        tool = ToolkitGeneratorService._sanitize_tool(tool, integration_type, provider, nlp_prompt, context)
        return {
            "id": str(uuid.uuid4()),
            "status": "draft",
            "integration_type": integration_type,
            "provider": provider,
            "nlp_prompt": nlp_prompt,
            "tool": tool,
            "context_summary": {
                "embedding_items": len(context.get("embedding_context", [])),
                "snapshot_items": len(context.get("snapshot_context", [])),
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
