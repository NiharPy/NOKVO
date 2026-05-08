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
from app.services.database_integration_service import DatabaseIntegrationService
from app.services.mcp_toolkit.capability_registry import ProviderCapabilityRegistry
from app.services.mcp_toolkit.embedding_retriever import EmbeddingRetrievalRequest, EmbeddingRetriever
from app.services.mcp_toolkit.context_retriever import ContextRetriever
from app.services.mcp_toolkit.name_utils import normalize_tool_name
from app.services.mcp_toolkit.pipeline import MCPToolkitPipeline
from app.services.mcp_toolkit.publish_gate import PublishGate
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


TOOLKIT_SYSTEM_PROMPT = """You are the MCP Tool Generator for a multi-tenant enterprise AI agent platform. Convert the natural-language request into a narrow, production-grade MCP tool draft. Be strict, not creative.

Use only verified context supplied in the request: scanned DB schema, connector templates, OpenAPI/provider specs, or explicit admin context. Never invent tables, columns, API endpoints, provider fields, auth flows, credentials, or business rules. Never include secrets.

Default to least privilege. Every tool must be typed, tenant-scoped, auditable, testable, reviewable, and blocked from publish until validation, tests, and admin approval pass.

Database rules: no SELECT *, no generic table-selection tools, bound parameters only, fully qualified schema.table, exact verified tables/columns only, LIMIT for reads, SELECT only for read-only tools, block mutation/admin statements for reads, writes require approval policy, preconditions, strict WHERE for UPDATE/DELETE, idempotency where applicable, rollback/deactivation notes, and EXPLAIN validation before publish.

API rules: use only verified connector templates/specs, tenant secret references only, no embedded secrets, exact endpoint/method/params, timeout/retry/rate-limit, idempotency for mutations.

Schemas: strict input_schema with additionalProperties false and length/pattern/enum constraints where possible. output_schema must define exact fields and include success, data, message, error_code, trace_id.

Safety: classify risk low/medium/high/critical. Low read-only may run after publish. Medium writes require business checks/audit. High requires human/supervisor approval. Critical requires explicit admin policy and may be blocked. Mask phone/email/address/tokens/keys/secrets/passwords/connection strings unless explicitly approved; prefer phone_last4/email_masked/address_summary.

Return only valid JSON. No markdown or prose. The platform will normalize your draft into ToolGenerationResult and reject unsafe output."""


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
    def provider_capability(integration_type: str, provider: str):
        return ProviderCapabilityRegistry.resolve(integration_type, provider)

    @staticmethod
    def builder_key(integration_type: str, provider: str) -> str:
        capability = ProviderCapabilityRegistry.require(integration_type, provider)
        return capability.builder_key

    @staticmethod
    def connected_toolkit_builders(provider_status: dict[str, Any]) -> list[dict[str, Any]]:
        builders: list[dict[str, Any]] = []
        for capability in ProviderCapabilityRegistry.list_capabilities():
            if not ToolkitGeneratorService.is_integration_connected(provider_status, capability.integration_type, capability.provider):
                continue
            detail = ToolkitGeneratorService._builder_detail(provider_status, capability.integration_type, capability.provider)
            builders.append(
                {
                    "builder_key": capability.builder_key,
                    "integration_type": capability.integration_type,
                    "provider": capability.provider,
                    "execution_backend": capability.execution_backend,
                    "resource_model": capability.resource_model,
                    "label": capability.builder_label,
                    "description": capability.builder_description,
                    "detail": detail,
                    "supported_operations": list(capability.supported_operations),
                    "connected": True,
                }
            )
        return builders

    @staticmethod
    def is_integration_connected(provider_status: dict[str, Any], integration_type: str, provider: str) -> bool:
        integration_type = integration_type.strip().lower()
        provider = provider.strip().lower()
        if integration_type == "database":
            return provider_status.get("db_status") in {"schema_scanned", "indexed"} and provider_status.get("db_provider") == provider
        if integration_type == "crm":
            return provider_status.get("crm_status") == "indexed" and provider_status.get("crm_provider") == provider
        if integration_type == "zoho_desk":
            return provider_status.get("zoho_desk_status") == "indexed" and provider in {"zoho", "zoho_desk", "desk"}
        if integration_type == "erp":
            return provider_status.get("erp_status") == "indexed" and provider_status.get("erp_provider") == provider
        if integration_type == "shipping":
            return provider_status.get("shipping_status") == "indexed" and provider_status.get("shipping_provider") == provider
        if integration_type == "ecommerce":
            return provider_status.get("ecommerce_status") == "indexed" and provider_status.get("ecommerce_provider") == provider
        if integration_type == "his":
            return provider_status.get("his_status") == "indexed" and provider_status.get("his_provider") == provider
        if integration_type == "custom_api":
            return provider_status.get("custom_api_status") == "indexed" and provider_status.get("custom_api_provider") == provider
        return False

    @staticmethod
    def _builder_detail(provider_status: dict[str, Any], integration_type: str, provider: str) -> str:
        if integration_type == "database":
            return provider_status.get("db_database_name") or f"{len(provider_status.get('db_selected_sources') or [])} sources"
        prefix = ToolkitGeneratorService._status_prefix(integration_type)
        return (
            provider_status.get(f"{prefix}_account_name")
            or f"{provider_status.get(f'{prefix}_module_count') or 0} modules"
            or provider
        )

    @staticmethod
    def _normalize_tool_name(value: str) -> str:
        return normalize_tool_name(value)

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
        write_mode = execution_mode in {"write_requires_admin_approval", "write_requires_human_approval"}
        return {
            "roles_allowed": ["admin"] if write_mode else ["admin", "manager"],
            "requires_admin_review": True,
            "requires_execution_confirmation": write_mode,
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
            {"name": "permission_check", "type": "security", "input": sample_input, "expected_error": "PERMISSION_DENIED" if execution_mode in {"write_requires_admin_approval", "write_requires_human_approval"} else "success for allowed roles"},
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
    def publish_gate(result: dict[str, Any], admin_approval_exists: bool = False) -> dict[str, Any]:
        return PublishGate.evaluate(result, admin_approval_exists=admin_approval_exists)

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
            "mode": "write_requires_human_approval" if write_requested else "read_only",
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
                "test_run_required": True,
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
            "mode": "write_requires_human_approval" if write_capable else "read_only",
            "mapping": {
                "provider": provider,
                "action": action_name,
                "method": first_action.get("method"),
                "endpoint": first_action.get("endpoint"),
                "tenant_isolation": "Use only the tenant-scoped connection secret for this integration.",
                "pii_redaction": "Mask email, phone, address, token, key, and secret-like values unless explicitly required.",
                "requires_admin_confirmation": write_capable,
                "test_run_required": True,
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
                fallback["mode"] = "write_requires_human_approval"
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
        model_tool = tool.get("tool") if isinstance(tool, dict) and isinstance(tool.get("tool"), dict) else tool
        result = MCPToolkitPipeline.generate(
            nlp_prompt=nlp_prompt,
            integration_type=integration_type,
            provider=provider,
            context=context,
            model_tool=model_tool if isinstance(model_tool, dict) else None,
        )
        if integration_type == "database" and ToolkitGeneratorService._database_dangerous_requested(nlp_prompt):
            result["validation"]["status"] = "failed"
            result["validation"]["errors"].append(
                {"code": "DESTRUCTIVE_DATABASE_OPERATION", "message": "Destructive database schema/admin operations are not allowed for generated tools."}
            )
            result["review"] = {
                "status": "rejected",
                "required": True,
                "reviewer_role": "admin",
                "reason": "Destructive database schema/admin operation requested.",
                "required_changes": ["Request a narrow read-only lookup or a reviewed INSERT/UPDATE/DELETE business operation instead."],
            }
            result["tool"]["review"] = result["review"]
            result["tool"]["status"] = "rejected"
            result["tool"]["execution"]["mapping"]["sql_template"] = ""
            result["publish_gate"] = PublishGate.evaluate(result, admin_approval_exists=False)
        return result

    @staticmethod
    async def _run_database_explain_validation(tenant_res: TenantResources, result: dict[str, Any]) -> dict[str, Any]:
        tool = result.get("tool") or {}
        execution = tool.get("execution") or {}
        mapping = execution.get("mapping") or {}
        sql_template = str(mapping.get("sql_template") or "")
        if tool.get("integration_type") != "database" or execution.get("type") != "database_sql" or not sql_template:
            return result
        try:
            secret_provider, connection_string = await DatabaseIntegrationService.load_connection_secret(tenant_res)
            explain_result = await DatabaseIntegrationService.explain_sql(
                str(tool.get("provider") or secret_provider),
                connection_string,
                sql_template,
                mapping,
            )
            mapping["explain_validation"] = {
                **(mapping.get("explain_validation") or {}),
                **explain_result,
                "required_before_publish": True,
            }
            validation_checks = result.setdefault("validation", {}).setdefault("checks", {})
            validation_checks["sql_explain"] = "passed" if explain_result.get("status") in {"passed", "skipped"} else "failed"
        except Exception as exc:
            mapping["explain_validation"] = {
                **(mapping.get("explain_validation") or {}),
                "required_before_publish": True,
                "status": "failed",
                "error": str(exc),
            }
            result.setdefault("validation", {}).setdefault("errors", []).append(
                {"code": "SQL_EXPLAIN_FAILED", "message": f"PostgreSQL EXPLAIN failed: {exc}"}
            )
            result["validation"]["status"] = "failed"
            result["review"] = {
                "status": "rejected",
                "required": True,
                "reviewer_role": "admin",
                "reason": "PostgreSQL EXPLAIN validation failed.",
                "required_changes": ["Fix the generated SQL or rescan the database schema before review."],
            }
            result["tool"]["review"] = result["review"]
            result["tool"]["status"] = "rejected"
            result["tool"]["version"]["status"] = "rejected"
        result["publish_gate"] = PublishGate.evaluate(result, admin_approval_exists=False)
        return result

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
        actor_id: str = "system",
        actor_role: str = "admin",
    ) -> dict[str, Any]:
        capability = ProviderCapabilityRegistry.require(integration_type, provider)
        provider_status = dict(tenant_res.provider_status or {})
        snapshot_context = ToolkitGeneratorService._snapshot_context(provider_status, integration_type, provider)
        selected_context_snapshot_id = (
            provider_status.get(f"{ToolkitGeneratorService._status_prefix(integration_type)}_context_snapshot_id")
            or EmbeddingRetriever.snapshot_id(snapshot_context)
        )
        tenant_integration_id = (
            provider_status.get(f"{ToolkitGeneratorService._status_prefix(integration_type)}_tenant_integration_id")
            or f"{tenant_res.tenant_id}:{integration_type}:{provider}"
        )
        provider_connection_id = (
            provider_status.get(f"{ToolkitGeneratorService._status_prefix(integration_type)}_provider_connection_id")
            or ToolkitGeneratorService._provider_connection_id(tenant_res, integration_type, provider)
        )
        retrieval_request = EmbeddingRetrievalRequest(
            organization_id=str(tenant_res.organization_id),
            tenant_integration_id=str(tenant_integration_id),
            provider_connection_id=str(provider_connection_id),
            selected_context_snapshot_id=str(selected_context_snapshot_id),
            integration_type=integration_type,
            provider=provider,
            user_prompt=prompt,
            normalized_intent=None,
            operation_type=None,
            candidate_entities=EmbeddingRetriever.extract_candidate_entities(prompt),
            candidate_inputs=EmbeddingRetriever.extract_candidate_inputs(prompt),
            top_k=20,
        )
        retrieval = await EmbeddingRetriever.retrieve(tenant_res, retrieval_request, snapshot_context)
        selected_context = retrieval.get("selected_context") or {}
        return {
            "tenant_id": tenant_res.tenant_id,
            "organization_id": str(tenant_res.organization_id),
            "tenant_integration_id": str(tenant_integration_id),
            "provider_connection_id": str(provider_connection_id),
            "selected_context_snapshot_id": str(selected_context_snapshot_id),
            "actor_id": actor_id,
            "actor_role": actor_role,
            "integration_type": integration_type,
            "provider": provider,
            "builder_key": capability.builder_key,
            "provider_capability": {
                "execution_backend": capability.execution_backend,
                "resource_model": capability.resource_model,
                "supported_operations": list(capability.supported_operations),
                "allowed_source_kinds": list(capability.allowed_source_kinds),
            },
            "embedding_context": selected_context.get("embedding_context", []),
            "snapshot_context": selected_context.get("snapshot_context", []),
            "source_context": selected_context.get("source_context", []),
            "retrieval": retrieval,
        }

    @staticmethod
    def _status_prefix(integration_type: str) -> str:
        if integration_type == "database":
            return "db"
        return integration_type

    @staticmethod
    def _provider_connection_id(tenant_res: TenantResources, integration_type: str, provider: str) -> str:
        secret_refs = tenant_res.secret_refs or {}
        key = {
            "database": "db_connection_string",
            "crm": "crm_connection",
            "erp": "erp_connection",
            "shipping": "shipping_connection",
            "zoho_desk": "crm_connection",
        }.get(integration_type)
        secret_name = ((secret_refs.get(key) or {}).get("secret_name") if key else None) or ""
        return secret_name or f"{tenant_res.tenant_id}:{integration_type}:{provider}:connection"

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
        name_seed = nlp_prompt
        normalized_name = ToolkitGeneratorService._normalize_tool_name(name_seed)
        return {
            "name": normalized_name,
            "title": normalized_name.replace("_", " ").title(),
            "description": f"Generated draft for requested {integration_type}/{provider} operation: {nlp_prompt}",
            "integration_type": integration_type,
            "provider": provider,
            "mcp": {
                "server": "tenant-integration-mcp",
                "tool_name": normalized_name,
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
        actor_id: str = "system",
        actor_role: str = "admin",
        builder_key: str | None = None,
    ) -> dict[str, Any]:
        integration_type = integration_type.strip().lower()
        provider = provider.strip().lower()
        capability = ProviderCapabilityRegistry.require(integration_type, provider)
        if builder_key and builder_key != capability.builder_key:
            raise RuntimeError(f"Builder key {builder_key} does not match selected integration capability {capability.builder_key}")
        context = await ToolkitGeneratorService.build_context(tenant_res, integration_type, provider, nlp_prompt, actor_id=actor_id, actor_role=actor_role)
        user_payload = {
            "task": nlp_prompt,
            "selected_integration": {"integration_type": integration_type, "provider": provider},
            "selected_builder": {"builder_key": capability.builder_key, "execution_backend": capability.execution_backend, "resource_model": capability.resource_model},
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
        tool = await ToolkitGeneratorService._run_database_explain_validation(tenant_res, tool)
        return {
            "id": str(uuid.uuid4()),
            "status": "draft",
            "integration_type": integration_type,
            "provider": provider,
            "builder_key": capability.builder_key,
            "nlp_prompt": nlp_prompt,
            "tool": tool,
            "context_summary": {
                "embedding_items": len(context.get("embedding_context", [])),
                "snapshot_items": len(context.get("snapshot_context", [])),
            },
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
