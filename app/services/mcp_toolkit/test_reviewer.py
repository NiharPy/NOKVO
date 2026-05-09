from __future__ import annotations

from copy import deepcopy

from app.services.mcp_toolkit.reviewer import Reviewer
from app.services.mcp_toolkit.sql_validator import SQLValidator


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
                {"name": "order_number", "type": "text", "nullable": True, "fqn": "public.orders.order_number"},
                {"name": "customer_id", "type": "integer", "nullable": False, "fqn": "public.orders.customer_id"},
                {"name": "order_status", "type": "text", "nullable": True, "fqn": "public.orders.order_status"},
            ],
        },
    ],
    "allowed_tables": ["public.customers", "public.call_logs", "public.orders"],
    "allowed_columns": {
        "public.customers": ["customer_id", "full_name", "phone"],
        "public.call_logs": ["call_log_id", "customer_id"],
        "public.orders": ["order_id", "order_number", "customer_id", "order_status"],
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
                    "parameter_binding": {"style": "named", "sources": ["input.full_name", "input.phone", "input.idempotency_key"]},
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
            "status_reason": None,
            "confidence": 0.91,
            "fallback_used": False,
            "publish_blockers": [],
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
            "errors": [],
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

    assert_rejected_with(generation, "SQL_BLOCKED_STATEMENT_USED")


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

    assert_rejected_with(generation, "UNUSED_INPUT_FIELD")


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

    result = Reviewer.validate(generation, VERIFIED_CONTEXT)
    assert result["validation"]["status"] == "failed"
    assert "SQL_EXPLAIN_FAILED" in validation_codes(result)
    assert result["validation"]["checks"]["sql_explain"] == "failed"


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
                    "parameter_binding": {"style": "named", "sources": ["input.phone_number", "input.limit"]},
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
            "status_reason": None,
            "confidence": 0.91,
            "fallback_used": False,
            "publish_blockers": [],
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
            "errors": [],
            "rejected_chunks": [],
        },
    }

    assert_rejected_with(generation, "PII_OUTPUT_SCHEMA_MISMATCH")


def test_reviewer_rejects_fallback_title_description():
    generation = valid_insert_generation()
    generation["tool"]["title"] = "Postgresql generated tool"
    generation["tool"]["description"] = "Draft generated from indexed integration context. Configure Azure OpenAI for richer tool synthesis."

    assert_rejected_with(generation, "FALLBACK_TITLE_USED")


def test_reviewer_rejects_bare_table_name_tool_name():
    generation = valid_insert_generation()
    generation["tool"]["name"] = "customers"
    generation["tool"]["mcp"]["tool_name"] = "customers"
    generation["tool_plan"]["name"] = "customers"

    result = Reviewer.validate(generation, VERIFIED_CONTEXT)
    assert result["validation"]["status"] == "failed"
    codes = validation_codes(result)
    assert "BAD_OPERATION_NAME_PREFIX" in codes
    assert "GENERIC_TABLE_NAME_USED" in codes


def test_reviewer_accepts_semantically_consistent_insert_tool():
    generation = deepcopy(valid_insert_generation())

    result = Reviewer.validate(generation, VERIFIED_CONTEXT)

    assert result["validation"]["status"] == "passed"
    assert result["review"]["status"] == "draft"


def test_reviewer_rejects_unbound_limit_parameter():
    generation = deepcopy(valid_insert_generation())
    generation["tool"]["name"] = "lookup_customer_by_phone"
    generation["tool"]["title"] = "Lookup Customer By Phone"
    generation["tool"]["description"] = "Lookup customer by phone."
    generation["tool"]["mcp"]["tool_name"] = "lookup_customer_by_phone"
    generation["tool"]["execution"]["mode"] = "read_only"
    generation["tool"]["execution"]["mapping"] = {
        "sql_template": "SELECT public.customers.customer_id FROM public.customers WHERE public.customers.phone = :phone LIMIT :limit",
        "allowed_statements": ["SELECT"],
        "blocked_statements": ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"],
        "allowed_tables": ["public.customers"],
        "allowed_columns": {"public.customers": ["customer_id", "phone"]},
        "parameter_binding": {"style": "named", "sources": ["input.phone"]},
        "explain_validation": {"required_before_publish": True},
    }
    generation["tool"]["input_schema"]["properties"] = {
        "phone": {"type": "string", "minLength": 7, "maxLength": 20},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }
    generation["tool"]["input_schema"]["required"] = ["phone"]
    generation["tool"]["output_schema"]["properties"]["data"] = {
        "type": "object",
        "properties": {
            "records": {"type": "array", "items": {"type": "object", "properties": {"customer_id": {"type": "integer"}}, "additionalProperties": False}},
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

    assert_rejected_with(generation, "UNBOUND_SQL_PARAMETER")


def test_reviewer_rejects_read_output_sql_mismatch_for_call_logs_count():
    generation = deepcopy(valid_insert_generation())
    generation["tool"]["name"] = "lookup_customer_details_by_phone"
    generation["tool"]["title"] = "Lookup Customer Details By Phone"
    generation["tool"]["description"] = "Lookup customer details by phone."
    generation["tool"]["mcp"]["tool_name"] = "lookup_customer_details_by_phone"
    generation["tool"]["execution"]["mode"] = "read_only"
    generation["tool"]["execution"]["mapping"] = {
        "sql_template": "SELECT public.customers.customer_id, RIGHT(CAST(public.customers.phone AS TEXT), 4) AS phone_last4 FROM public.customers WHERE public.customers.phone = :phone LIMIT :limit",
        "allowed_statements": ["SELECT"],
        "blocked_statements": ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"],
        "allowed_tables": ["public.customers"],
        "allowed_columns": {"public.customers": ["customer_id", "phone"]},
        "parameter_binding": {"style": "named", "sources": ["input.phone", "input.limit"]},
        "explain_validation": {"required_before_publish": True},
    }
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
                    "properties": {
                        "customer_id": {"type": "integer"},
                        "phone_last4": {"type": "string"},
                        "call_logs_count": {"type": "integer"},
                    },
                    "additionalProperties": False,
                },
            },
            "count": {"type": "integer"},
        },
        "required": ["records", "count"],
        "additionalProperties": False,
    }
    generation["tool_plan"] = {
        "name": "lookup_customer_details_by_phone",
        "operation_type": "read",
        "exact_resources_used": ["public.customers"],
        "output_fields": [{"name": "customer_id", "type": "integer"}, {"name": "phone_last4", "type": "string"}, {"name": "call_logs_count", "type": "integer"}],
        "approval_policy": {"human_approval_required": False},
    }

    assert_rejected_with(generation, "READ_OUTPUT_SQL_MISMATCH")


def test_reviewer_rejects_allowed_tables_with_unused_call_logs():
    generation = deepcopy(valid_insert_generation())
    generation["tool"]["execution"]["mode"] = "read_only"
    generation["tool"]["execution"]["mapping"] = {
        "sql_template": "SELECT public.customers.customer_id FROM public.customers WHERE public.customers.phone = :phone LIMIT :limit",
        "allowed_statements": ["SELECT"],
        "blocked_statements": ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"],
        "allowed_tables": ["public.customers", "public.call_logs"],
        "allowed_columns": {"public.customers": ["customer_id", "phone"], "public.call_logs": ["call_log_id"]},
        "parameter_binding": {"style": "named", "sources": ["input.phone", "input.limit"]},
        "explain_validation": {"required_before_publish": True},
    }
    generation["tool"]["input_schema"]["properties"] = {
        "phone": {"type": "string", "minLength": 7, "maxLength": 20},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }
    generation["tool"]["input_schema"]["required"] = ["phone"]
    generation["tool"]["output_schema"]["properties"]["data"] = {
        "type": "object",
        "properties": {
            "records": {"type": "array", "items": {"type": "object", "properties": {"customer_id": {"type": "integer"}}, "additionalProperties": False}},
            "count": {"type": "integer"},
        },
        "required": ["records", "count"],
        "additionalProperties": False,
    }
    generation["tool_plan"] = {
        "name": "lookup_customer_by_phone",
        "operation_type": "read",
        "exact_resources_used": ["public.customers", "public.call_logs"],
        "output_fields": [{"name": "customer_id", "type": "integer"}],
        "approval_policy": {"human_approval_required": False},
    }

    assert_rejected_with(generation, "SQL_ALLOWED_TABLE_MISMATCH")


def test_reviewer_rejects_mutation_intent_downgraded_to_read():
    generation = deepcopy(valid_insert_generation())
    generation["tool"]["name"] = "lookup_customer_by_phone"
    generation["tool"]["title"] = "Lookup Customer By Phone"
    generation["tool"]["description"] = "Lookup customer by phone."
    generation["tool"]["mcp"]["tool_name"] = "lookup_customer_by_phone"
    generation["tool"]["execution"]["mode"] = "read_only"
    generation["tool"]["execution"]["mapping"] = {
        "sql_template": "SELECT public.customers.customer_id FROM public.customers WHERE public.customers.phone = :phone_number LIMIT :limit",
        "allowed_statements": ["SELECT"],
        "blocked_statements": ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"],
        "allowed_tables": ["public.customers"],
        "allowed_columns": {"public.customers": ["customer_id", "phone"]},
        "parameter_binding": {"style": "named", "sources": ["input.phone_number", "input.limit"]},
        "explain_validation": {"required_before_publish": True},
    }
    generation["tool"]["input_schema"]["properties"] = {
        "phone_number": {"type": "string", "minLength": 7, "maxLength": 20},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }
    generation["tool"]["input_schema"]["required"] = ["phone_number"]
    generation["tool"]["output_schema"]["properties"]["data"] = {
        "type": "object",
        "properties": {
            "records": {"type": "array", "items": {"type": "object", "properties": {"customer_id": {"type": "integer"}}, "additionalProperties": False}},
            "count": {"type": "integer"},
        },
        "required": ["records", "count"],
        "additionalProperties": False,
    }
    generation["tool_plan"] = {
        "name": "lookup_customer_by_phone",
        "operation_type": "read",
        "workflow_type": None,
        "exact_resources_used": ["public.customers"],
        "output_fields": [{"name": "customer_id", "type": "integer"}],
        "approval_policy": {"human_approval_required": False},
        "intent_signals": {
            "normalized_prompt": "retrieve customer details using phone number and change username",
            "read_detected": True,
            "create_detected": False,
            "update_detected": True,
            "delete_detected": False,
            "workflow_detected": True,
            "operation_type": "workflow",
            "workflow_type": "read_then_update",
            "target_field": "username",
        },
    }

    assert_rejected_with(generation, "MUTATION_INTENT_DOWNGRADED_TO_READ")


def test_reviewer_rejects_order_status_mutation_prompt_generated_as_read_only():
    generation = deepcopy(valid_insert_generation())
    generation["tool"]["name"] = "check_order_status_by_phone"
    generation["tool"]["title"] = "Check Order Status By Phone"
    generation["tool"]["description"] = "Check order status by phone."
    generation["tool"]["mcp"]["tool_name"] = "check_order_status_by_phone"
    generation["tool"]["execution"]["mode"] = "read_only"
    generation["tool"]["execution"]["mapping"] = {
        "sql_template": "SELECT public.orders.order_id FROM public.orders LIMIT :limit",
        "allowed_statements": ["SELECT"],
        "blocked_statements": ["INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "CREATE"],
        "allowed_tables": ["public.orders"],
        "allowed_columns": {"public.orders": ["order_id", "customer_id"]},
        "parameter_binding": {"style": "named", "sources": ["input.limit"]},
        "explain_validation": {"required_before_publish": True},
    }
    generation["tool"]["input_schema"]["properties"] = {
        "phone_number": {"type": "string", "minLength": 7, "maxLength": 20},
        "limit": {"type": "integer", "minimum": 1, "maximum": 100},
    }
    generation["tool"]["input_schema"]["required"] = ["phone_number"]
    generation["tool"]["output_schema"]["properties"]["data"] = {
        "type": "object",
        "properties": {
            "records": {"type": "array", "items": {"type": "object", "properties": {"order_id": {"type": "integer"}}, "additionalProperties": False}},
            "count": {"type": "integer"},
        },
        "required": ["records", "count"],
        "additionalProperties": False,
    }
    generation["tool_plan"] = {
        "name": "check_order_status_by_phone",
        "operation_type": "read",
        "workflow_type": None,
        "target_entity": "order",
        "target_field": "order_status",
        "exact_resources_used": ["public.orders"],
        "output_fields": [{"name": "order_id", "type": "integer"}],
        "approval_policy": {"human_approval_required": False},
        "intent_signals": {
            "normalized_prompt": "retrieve orders of a user using their name and phone number and the status of the order must be changed",
            "read_detected": True,
            "create_detected": False,
            "update_detected": False,
            "delete_detected": False,
            "operation_type": "read",
            "target_entity": "order",
            "target_field": "order_status",
        },
    }
    generation["retrieval"]["verified_resources"] = [{"resource_type": "table", "name": "public.orders", "source_context_ids": ["ctx_orders"]}]

    result = Reviewer.validate(generation, VERIFIED_CONTEXT)
    codes = validation_codes(result)
    assert result["validation"]["status"] == "failed"
    assert "MUTATION_INTENT_DOWNGRADED_TO_READ" in codes
    assert "PROMPT_TOOL_INTENT_MISMATCH" in codes
    assert "MISSING_REQUIRED_INPUT_FIELD" in codes
    assert "MISSING_MUTATION_TARGET_VALUE" in codes
    assert "WORKFLOW_REQUIRES_HUMAN_APPROVAL" in codes


def test_sql_explain_bindings_follow_input_schema_for_optional_order_strings():
    sql = (
        "WITH selected_order AS (SELECT public.orders.order_id FROM public.orders "
        "WHERE (:order_id IS NULL OR CAST(public.orders.order_id AS TEXT) = :order_id) "
        "AND (:order_number IS NULL OR public.orders.order_number = :order_number)) "
        "UPDATE public.orders AS o SET order_status = :new_order_status "
        "FROM selected_order so WHERE o.order_id = so.order_id "
        "RETURNING o.order_id, o.order_number, o.order_status AS updated_order_status"
    )
    mapping = {
        "allowed_columns": {"public.orders": ["order_id", "order_number", "order_status"]},
        "input_schema": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "required": False},
                "order_number": {"type": "string", "required": False},
                "new_order_status": {"type": "string"},
            },
        },
    }

    bindings = SQLValidator.safe_sample_bindings(sql, mapping)
    variants = SQLValidator.safe_sample_binding_variants(sql, mapping)

    assert bindings["order_id"] == "1"
    assert isinstance(bindings["order_id"], str)
    assert bindings["order_number"] == "sample_number"
    assert variants[0]["order_id"] is None
    assert variants[0]["order_number"] is None
    assert variants[1]["order_id"] == "1"
    assert variants[1]["order_number"] == "sample_number"


def test_reviewer_rejects_order_workflow_limit_one_target_selection():
    generation = deepcopy(valid_insert_generation())
    generation["tool"]["name"] = "update_order_status_by_name_and_phone"
    generation["tool"]["title"] = "Update Order Status By Name And Phone"
    generation["tool"]["description"] = "Update order status by name and phone."
    generation["tool"]["mcp"]["tool_name"] = "update_order_status_by_name_and_phone"
    generation["tool"]["execution"]["mode"] = "write_requires_human_approval"
    generation["tool"]["execution"]["mapping"] = {
        "sql_template": "WITH selected_order AS (SELECT public.orders.order_id FROM public.orders JOIN public.customers ON public.orders.customer_id = public.customers.customer_id WHERE public.customers.full_name = :customer_name AND public.customers.phone = :phone_number LIMIT 1) UPDATE public.orders AS o SET order_status = :new_order_status FROM selected_order so WHERE o.order_id = so.order_id RETURNING o.order_id, o.order_status AS updated_order_status",
        "allowed_statements": ["UPDATE"],
        "blocked_statements": ["INSERT", "DELETE", "DROP", "ALTER", "CREATE"],
        "allowed_tables": ["public.orders", "public.customers"],
        "allowed_columns": {"public.orders": ["order_id", "customer_id", "order_status"], "public.customers": ["customer_id", "full_name", "phone"]},
        "parameter_binding": {"style": "named", "sources": ["input.customer_name", "input.phone_number", "input.new_order_status", "input.idempotency_key"]},
        "idempotency": {"required": True, "source": "input.idempotency_key"},
        "explain_validation": {"required_before_publish": True},
    }
    generation["tool"]["input_schema"]["properties"] = {
        "customer_name": {"type": "string"},
        "phone_number": {"type": "string"},
        "new_order_status": {"type": "string"},
        "idempotency_key": {"type": "string"},
    }
    generation["tool"]["input_schema"]["required"] = ["customer_name", "phone_number", "new_order_status", "idempotency_key"]
    generation["tool"]["output_schema"]["properties"]["data"]["properties"] = {
        "operation": {"type": "string", "enum": ["workflow"]},
        "affected_count": {"type": "integer"},
        "idempotency_key": {"type": "string"},
        "order_id": {"type": "integer"},
        "updated_order_status": {"type": "string"},
    }
    generation["tool_plan"] = {
        "name": "update_order_status_by_name_and_phone",
        "operation_type": "workflow",
        "workflow_type": "read_then_update",
        "target_entity": "order",
        "target_field": "order_status",
        "exact_resources_used": ["public.orders", "public.customers"],
        "output_fields": [{"name": "order_id", "type": "integer"}, {"name": "updated_order_status", "type": "string"}],
        "approval_policy": {"human_approval_required": True},
        "intent_signals": {"normalized_prompt": "retrieve orders by name and phone and status must be changed", "read_detected": True, "update_detected": True, "target_field": "order_status"},
    }
    generation["retrieval"]["verified_resources"] = [
        {"resource_type": "table", "name": "public.orders", "source_context_ids": ["ctx_orders"]},
        {"resource_type": "table", "name": "public.customers", "source_context_ids": ["ctx_customers"]},
    ]
    generation["tool"]["source_context"] = [
        {**generation["tool"]["source_context"][0], "resource": "public.orders", "context_id": "ctx_orders"},
        {**generation["tool"]["source_context"][0], "resource": "public.customers", "context_id": "ctx_customers"},
    ]

    result = Reviewer.validate(generation, VERIFIED_CONTEXT)
    codes = validation_codes(result)
    assert "ORDER_TARGET_AMBIGUOUS" in codes
    assert "UNSAFE_MUTATION_TARGET_SELECTION" in codes


def test_reviewer_rejects_order_workflow_missing_precheck_mapping():
    generation = deepcopy(valid_insert_generation())
    generation["tool"]["name"] = "update_order_status_by_name_and_phone"
    generation["tool"]["title"] = "Update Order Status By Name And Phone"
    generation["tool"]["description"] = "Update order status by name and phone."
    generation["tool"]["mcp"]["tool_name"] = "update_order_status_by_name_and_phone"
    generation["tool"]["execution"]["mode"] = "write_requires_human_approval"
    generation["tool"]["execution"]["mapping"] = {
        "sql_template": "WITH candidate_count AS (SELECT 1 AS match_count), selected_order AS (SELECT public.orders.order_id FROM public.orders CROSS JOIN candidate_count cc WHERE cc.match_count = 1) UPDATE public.orders AS o SET order_status = :new_order_status FROM selected_order so WHERE o.order_id = so.order_id RETURNING o.order_id, o.order_status AS updated_order_status",
        "allowed_statements": ["UPDATE"],
        "blocked_statements": ["INSERT", "DELETE", "DROP", "ALTER", "CREATE"],
        "allowed_tables": ["public.orders"],
        "allowed_columns": {"public.orders": ["order_id", "order_status"]},
        "parameter_binding": {"style": "named", "sources": ["input.order_id", "input.order_number", "input.new_order_status", "input.idempotency_key"]},
        "idempotency": {"required": True, "source": "input.idempotency_key"},
        "explain_validation": {"required_before_publish": True},
    }
    generation["tool"]["input_schema"]["properties"] = {
        "order_id": {"type": "string", "required": False, "runtime_control": "required_when_multiple_matches"},
        "order_number": {"type": "string", "required": False, "runtime_control": "optional_order_disambiguation"},
        "new_order_status": {"type": "string"},
        "idempotency_key": {"type": "string"},
    }
    generation["tool"]["input_schema"]["required"] = ["new_order_status", "idempotency_key"]
    generation["tool"]["output_schema"]["properties"]["data"]["properties"] = {
        "operation": {"type": "string", "enum": ["workflow"]},
        "affected_count": {"type": "integer"},
        "idempotency_key": {"type": "string"},
        "order_id": {"type": "integer"},
        "updated_order_status": {"type": "string"},
    }
    generation["tool_plan"] = {
        "name": "update_order_status_by_name_and_phone",
        "operation_type": "workflow",
        "workflow_type": "read_then_update",
        "target_entity": "order",
        "target_field": "order_status",
        "exact_resources_used": ["public.orders"],
        "output_fields": [{"name": "order_id", "type": "integer"}, {"name": "updated_order_status", "type": "string"}],
        "approval_policy": {"human_approval_required": True},
        "intent_signals": {"normalized_prompt": "retrieve orders and status must be changed", "read_detected": True, "update_detected": True, "target_field": "order_status"},
    }
    generation["retrieval"]["verified_resources"] = [{"resource_type": "table", "name": "public.orders", "source_context_ids": ["ctx_orders"]}]
    generation["tool"]["source_context"] = [{**generation["tool"]["source_context"][0], "resource": "public.orders", "context_id": "ctx_orders"}]

    result = Reviewer.validate(generation, VERIFIED_CONTEXT)
    codes = validation_codes(result)
    assert result["validation"]["status"] == "failed"
    assert "MUTATION_TARGET_AMBIGUOUS" in codes
    assert "ORDER_TARGET_AMBIGUOUS" in codes


def test_reviewer_reports_output_sql_returning_mismatch_for_missing_order_number():
    generation = deepcopy(valid_insert_generation())
    generation["tool_plan"]["operation_type"] = "workflow"
    generation["tool_plan"]["name"] = "update_order_status_by_name_and_phone"
    generation["tool_plan"]["exact_resources_used"] = ["public.orders"]
    generation["tool_plan"]["output_fields"] = [
        {"name": "order_id", "type": "integer"},
        {"name": "order_number", "type": "string"},
        {"name": "updated_order_status", "type": "string"},
    ]
    generation["tool_plan"]["approval_policy"] = {"human_approval_required": True}
    generation["tool"]["name"] = "update_order_status_by_name_and_phone"
    generation["tool"]["mcp"]["tool_name"] = "update_order_status_by_name_and_phone"
    generation["tool"]["execution"]["mode"] = "write_requires_human_approval"
    generation["tool"]["execution"]["mapping"]["sql_template"] = "UPDATE public.orders AS o SET order_status = :new_order_status WHERE o.order_id = :order_id RETURNING o.order_id, o.order_status AS updated_order_status"
    generation["tool"]["execution"]["mapping"]["allowed_statements"] = ["UPDATE"]
    generation["tool"]["execution"]["mapping"]["blocked_statements"] = ["INSERT", "DELETE", "DROP", "ALTER", "CREATE"]
    generation["tool"]["execution"]["mapping"]["allowed_tables"] = ["public.orders"]
    generation["tool"]["execution"]["mapping"]["allowed_columns"] = {"public.orders": ["order_id", "order_number", "order_status"]}
    generation["tool"]["execution"]["mapping"]["parameter_binding"] = {"style": "named", "sources": ["input.order_id", "input.new_order_status", "input.idempotency_key"]}
    generation["tool"]["execution"]["mapping"]["idempotency"] = {"required": True, "source": "input.idempotency_key"}
    generation["tool"]["input_schema"]["properties"] = {"order_id": {"type": "string"}, "new_order_status": {"type": "string"}, "idempotency_key": {"type": "string"}}
    generation["tool"]["input_schema"]["required"] = ["order_id", "new_order_status", "idempotency_key"]
    generation["tool"]["output_schema"]["properties"]["data"]["properties"] = {
        "operation": {"type": "string", "enum": ["workflow"]},
        "affected_count": {"type": "integer"},
        "idempotency_key": {"type": "string"},
        "order_id": {"type": "integer"},
        "order_number": {"type": "string"},
        "updated_order_status": {"type": "string"},
    }
    generation["retrieval"]["verified_resources"] = [{"resource_type": "table", "name": "public.orders", "source_context_ids": ["ctx_orders"]}]
    generation["tool"]["source_context"] = [{**generation["tool"]["source_context"][0], "resource": "public.orders", "context_id": "ctx_orders"}]

    result = Reviewer.validate(generation, VERIFIED_CONTEXT)
    codes = validation_codes(result)
    assert "MUTATION_PLAN_OUTPUT_MISMATCH" in codes
    assert "OUTPUT_SQL_RETURNING_MISMATCH" in codes


def test_reviewer_rejects_pii_policy_for_unused_resource():
    generation = valid_insert_generation()
    generation["tool"]["safety"]["pii_policy"] = {
        "field_policy": {
            "public.customers.phone": {"classification": "phone", "redaction": "phone_last4", "output_name": "phone_last4"},
            "public.call_logs.customer_id": {"classification": "identifier", "redaction": "none", "output_name": "customer_id"},
        }
    }

    assert_rejected_with(generation, "TOOL_PII_POLICY_UNUSED_RESOURCE")
