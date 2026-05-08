from __future__ import annotations

from copy import deepcopy

from app.services.mcp_toolkit.reviewer import Reviewer


VERIFIED_CONTEXT = {
    "tables": [
        {
            "schema": "public",
            "table": "customers",
            "fqn": "public.customers",
            "primary_key": ["customer_id"],
            "columns": [
                {"name": "customer_id", "type": "integer", "nullable": False, "fqn": "public.customers.customer_id"},
                {"name": "full_name", "type": "text", "nullable": False, "fqn": "public.customers.full_name"},
                {"name": "phone", "type": "text", "nullable": False, "fqn": "public.customers.phone"},
            ],
        },
        {
            "schema": "public",
            "table": "call_logs",
            "fqn": "public.call_logs",
            "primary_key": ["call_log_id"],
            "columns": [
                {"name": "call_log_id", "type": "integer", "nullable": False, "fqn": "public.call_logs.call_log_id"},
                {"name": "customer_id", "type": "integer", "nullable": False, "fqn": "public.call_logs.customer_id"},
            ],
        },
        {
            "schema": "public",
            "table": "orders",
            "fqn": "public.orders",
            "primary_key": ["order_id"],
            "columns": [
                {"name": "order_id", "type": "integer", "nullable": False, "fqn": "public.orders.order_id"},
                {"name": "customer_id", "type": "integer", "nullable": False, "fqn": "public.orders.customer_id"},
            ],
        },
    ],
    "allowed_tables": ["public.customers", "public.call_logs", "public.orders"],
    "allowed_columns": {
        "public.customers": ["customer_id", "full_name", "phone"],
        "public.call_logs": ["call_log_id", "customer_id"],
        "public.orders": ["order_id", "customer_id"],
    },
}


def valid_insert_generation() -> dict:
    return {
        "tool": {
            "name": "create_customers",
            "title": "Create Customers",
            "description": "Create a customer.",
            "integration_type": "database",
            "provider": "postgresql",
            "mcp": {"tool_name": "create_customers"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "full_name": {"type": "string", "minLength": 1, "maxLength": 120},
                    "phone": {"type": "string", "minLength": 7, "maxLength": 20},
                    "idempotency_key": {"type": "string", "minLength": 8, "maxLength": 120},
                },
                "required": ["full_name", "phone", "idempotency_key"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "operation": {"type": "string", "enum": ["create"]},
                            "provider_reference": {"type": "string", "nullable": True},
                            "affected_count": {"type": "integer"},
                            "idempotency_key": {"type": "string", "nullable": True},
                        },
                        "required": ["operation", "affected_count"],
                        "additionalProperties": False,
                    },
                    "message": {"type": "string"},
                    "error_code": {"type": "string", "nullable": True},
                    "trace_id": {"type": "string"},
                },
                "required": ["success", "data", "message", "trace_id"],
                "additionalProperties": False,
            },
            "execution": {
                "type": "database_sql",
                "mode": "write_requires_human_approval",
                "mapping": {
                    "sql_template": "INSERT INTO public.customers (full_name, phone) VALUES (:full_name, :phone)",
                    "allowed_statements": ["INSERT"],
                    "blocked_statements": ["DROP", "ALTER", "CREATE", "TRUNCATE", "UPDATE", "DELETE", "SELECT"],
                    "allowed_tables": ["public.customers"],
                    "allowed_columns": {"public.customers": ["full_name", "phone"]},
                    "idempotency": {"required": True, "source": "input.idempotency_key"},
                    "explain_validation": {"required_before_publish": True},
                },
            },
            "review": {"required": True},
            "tests": {"test_run_required": True},
            "safety": {"pii_policy": {"enabled": True}, "audit_policy": {"enabled": True}},
            "source_context": [
                {
                    "context_id": "ctx_customers",
                    "source": "provider_status.db_schema_snapshot",
                    "source_kind": "db_table",
                    "organization_id": "org_1",
                    "tenant_integration_id": "tenant_1:database:postgresql",
                    "provider_connection_id": "provider_conn_1",
                    "context_snapshot_id": "snapshot_A",
                    "integration_type": "database",
                    "provider": "postgresql",
                    "resource": "public.customers",
                    "status": "active",
                    "retrieval_score": 0.91,
                }
            ],
        },
        "tool_plan": {
            "name": "create_customers",
            "operation_type": "create",
            "exact_resources_used": ["public.customers"],
            "output_fields": [
                {"name": "affected_count", "type": "integer"},
                {"name": "idempotency_key", "type": "string"},
            ],
            "approval_policy": {"human_approval_required": True},
        },
        "retrieval": {
            "status": "passed",
            "confidence": 0.91,
            "query_variants": ["create customers"],
            "scope_filter": {
                "organization_id": "org_1",
                "tenant_integration_id": "tenant_1:database:postgresql",
                "provider_connection_id": "provider_conn_1",
                "selected_context_snapshot_id": "snapshot_A",
                "integration_type": "database",
                "provider": "postgresql",
            },
            "verified_resources": [{"resource_type": "table", "name": "public.customers", "source_context_ids": ["ctx_customers"]}],
            "retrieved_context_ids": ["ctx_customers"],
            "rejected_context_ids": [],
            "warnings": [],
            "rejected_chunks": [],
        },
    }


def validation_codes(result: dict) -> set[str]:
    return {error["code"] for error in result["validation"]["errors"]}


def assert_rejected_with(generation: dict, code: str) -> None:
    result = Reviewer.validate(generation, VERIFIED_CONTEXT)
    assert result["validation"]["status"] == "failed"
    assert result["review"]["status"] == "rejected"
    assert result["review"]["required_changes"]
    assert code in validation_codes(result)


def test_reviewer_rejects_sql_statement_allowed_statements_mismatch():
    generation = valid_insert_generation()
    generation["tool"]["execution"]["mapping"]["allowed_statements"] = ["SELECT"]

    assert_rejected_with(generation, "SQL_ALLOWED_STATEMENT_MISMATCH")


def test_reviewer_rejects_sql_statement_inside_blocked_statements():
    generation = valid_insert_generation()
    generation["tool"]["execution"]["mapping"]["blocked_statements"].append("INSERT")

    assert_rejected_with(generation, "SQL_STATEMENT_BLOCKED")


def test_reviewer_rejects_tool_name_table_mismatch():
    generation = valid_insert_generation()
    generation["tool"]["name"] = "create_call_logs"
    generation["tool"]["mcp"]["tool_name"] = "create_call_logs"
    generation["tool_plan"]["name"] = "create_call_logs"

    assert_rejected_with(generation, "TOOL_NAME_TABLE_MISMATCH")


def test_reviewer_rejects_operation_type_sql_mismatch():
    generation = valid_insert_generation()
    generation["tool_plan"]["operation_type"] = "read"

    assert_rejected_with(generation, "OPERATION_SQL_MISMATCH")


def test_reviewer_rejects_unused_input_fields():
    generation = valid_insert_generation()
    generation["tool"]["input_schema"]["properties"]["limit"] = {"type": "integer", "minimum": 1, "maximum": 100}

    assert_rejected_with(generation, "UNUSED_INPUT_FIELDS")


def test_reviewer_rejects_output_fields_not_matching_execution():
    generation = valid_insert_generation()
    generation["tool_plan"]["output_fields"] = [{"name": "order_id", "type": "integer"}]

    assert_rejected_with(generation, "MUTATION_PLAN_OUTPUT_MISMATCH")


def test_reviewer_rejects_partial_insert_missing_required_columns():
    generation = valid_insert_generation()
    generation["tool"]["execution"]["mapping"]["sql_template"] = "INSERT INTO public.customers (full_name) VALUES (:full_name)"
    generation["tool"]["input_schema"]["properties"].pop("phone")
    generation["tool"]["input_schema"]["required"].remove("phone")

    assert_rejected_with(generation, "INSERT_MISSING_REQUIRED_COLUMNS")


def test_reviewer_rejects_malformed_sql_syntax():
    generation = valid_insert_generation()
    generation["tool"]["name"] = "lookup_customer_by_phone"
    generation["tool"]["title"] = "Lookup Customer By Phone"
    generation["tool"]["description"] = "Lookup customer by phone."
    generation["tool"]["mcp"]["tool_name"] = "lookup_customer_by_phone"
    generation["tool"]["execution"]["mode"] = "read_only"
    generation["tool"]["execution"]["mapping"]["sql_template"] = "SELECT public.customers.customer_id, FROM public.customers WHERE public.customers.phone = :phone LIMIT :limit"
    generation["tool"]["execution"]["mapping"]["allowed_statements"] = ["SELECT"]
    generation["tool"]["execution"]["mapping"]["blocked_statements"] = ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"]
    generation["tool"]["input_schema"]["properties"] = {
        "phone": {"type": "string", "minLength": 7, "maxLength": 20},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }
    generation["tool"]["input_schema"]["required"] = ["phone"]
    generation["tool"]["output_schema"]["properties"]["data"] = {
        "type": "object",
        "properties": {
            "records": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {"customer_id": {"type": "integer", "nullable": True}},
                    "additionalProperties": False,
                },
            },
            "count": {"type": "integer"},
        },
        "required": ["records", "count"],
        "additionalProperties": False,
    }
    generation["tool_plan"] = {
        "name": "lookup_customer_by_phone",
        "operation_type": "read",
        "exact_resources_used": ["public.customers"],
        "output_fields": [{"name": "customer_id", "type": "integer"}],
        "approval_policy": {"human_approval_required": False},
    }

    assert_rejected_with(generation, "SQL_SYNTAX_INVALID")


def test_reviewer_rejects_explain_failure():
    generation = valid_insert_generation()
    generation["tool"]["execution"]["mapping"]["explain_validation"] = {
        "required_before_publish": True,
        "status": "failed",
        "error": "syntax error at or near INSERT",
    }

    assert_rejected_with(generation, "SQL_EXPLAIN_FAILED")


def test_reviewer_rejects_raw_pii_fields_in_read_output_schema():
    generation = {
        "tool": {
            "name": "lookup_customers",
            "title": "Lookup Customers",
            "description": "Lookup customers by phone.",
            "integration_type": "database",
            "provider": "postgresql",
            "mcp": {"tool_name": "lookup_customers"},
            "input_schema": {
                "type": "object",
                "properties": {
                    "phone_number": {"type": "string", "minLength": 7, "maxLength": 20},
                    "limit": {"type": "integer", "minimum": 1, "maximum": 100},
                },
                "required": ["phone_number"],
                "additionalProperties": False,
            },
            "output_schema": {
                "type": "object",
                "properties": {
                    "success": {"type": "boolean"},
                    "data": {
                        "type": "object",
                        "properties": {
                            "records": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "customer_id": {"type": "number"},
                                        "phone": {"type": "string"},
                                    },
                                    "additionalProperties": False,
                                },
                            },
                            "count": {"type": "integer"},
                        },
                        "additionalProperties": False,
                    },
                    "message": {"type": "string"},
                    "error_code": {"type": "string", "nullable": True},
                    "trace_id": {"type": "string"},
                },
                "additionalProperties": False,
            },
            "execution": {
                "type": "database_sql",
                "mode": "read_only",
                "mapping": {
                    "sql_template": "SELECT public.customers.customer_id, RIGHT(CAST(public.customers.phone AS TEXT), 4) AS phone_last4 FROM public.customers WHERE public.customers.phone = :phone_number LIMIT :limit",
                    "allowed_statements": ["SELECT"],
                    "blocked_statements": ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"],
                    "allowed_tables": ["public.customers"],
                    "allowed_columns": {"public.customers": ["customer_id", "phone"]},
                    "explain_validation": {"required_before_publish": True},
                },
            },
            "review": {"required": True},
            "tests": {"test_run_required": True},
            "safety": {
                "pii_policy": {
                    "field_policy": {
                        "public.customers.phone": {"classification": "phone", "redaction": "phone_last4", "output_name": "phone_last4"}
                    }
                },
                "audit_policy": {"enabled": True},
            },
            "source_context": [
                {
                    "context_id": "ctx_customers",
                    "source": "provider_status.db_schema_snapshot",
                    "source_kind": "db_table",
                    "organization_id": "org_1",
                    "tenant_integration_id": "tenant_1:database:postgresql",
                    "provider_connection_id": "provider_conn_1",
                    "context_snapshot_id": "snapshot_A",
                    "integration_type": "database",
                    "provider": "postgresql",
                    "resource": "public.customers",
                    "status": "active",
                    "retrieval_score": 0.91,
                }
            ],
        },
        "tool_plan": {
            "name": "lookup_customers",
            "operation_type": "read",
            "exact_resources_used": ["public.customers"],
            "output_fields": [{"name": "phone_last4", "type": "string"}],
            "approval_policy": {"human_approval_required": False},
        },
        "retrieval": {
            "status": "passed",
            "confidence": 0.91,
            "query_variants": ["lookup customers by phone"],
            "scope_filter": {
                "organization_id": "org_1",
                "tenant_integration_id": "tenant_1:database:postgresql",
                "provider_connection_id": "provider_conn_1",
                "selected_context_snapshot_id": "snapshot_A",
                "integration_type": "database",
                "provider": "postgresql",
            },
            "verified_resources": [{"resource_type": "table", "name": "public.customers", "source_context_ids": ["ctx_customers"]}],
            "retrieved_context_ids": ["ctx_customers"],
            "rejected_context_ids": [],
            "warnings": [],
            "rejected_chunks": [],
        },
    }

    assert_rejected_with(generation, "PII_OUTPUT_SCHEMA_MISMATCH")


def test_reviewer_rejects_fallback_title_description():
    generation = valid_insert_generation()
    generation["tool"]["title"] = "Postgresql generated tool"
    generation["tool"]["description"] = "Draft generated from indexed integration context. Configure Azure OpenAI for richer tool synthesis."

    assert_rejected_with(generation, "FALLBACK_TITLE_DESCRIPTION")


def test_reviewer_rejects_bare_table_name_tool_name():
    generation = valid_insert_generation()
    generation["tool"]["name"] = "customers"
    generation["tool"]["mcp"]["tool_name"] = "customers"
    generation["tool_plan"]["name"] = "customers"

    assert_rejected_with(generation, "BAD_OPERATION_NAME_PREFIX")


def test_reviewer_accepts_semantically_consistent_insert_tool():
    generation = deepcopy(valid_insert_generation())

    result = Reviewer.validate(generation, VERIFIED_CONTEXT)

    assert result["validation"]["status"] == "passed"
    assert result["review"]["status"] == "draft"
