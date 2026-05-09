from __future__ import annotations

from app.services.toolkit_generator_service import ToolkitGeneratorService


def db_context(*tables: dict, **extra: dict) -> dict:
    base = {
        "organization_id": "org_1",
        "tenant_id": "tenant_1",
        "tenant_integration_id": "tenant_1:database:postgresql",
        "provider_connection_id": "provider_conn_1",
        "selected_context_snapshot_id": "snapshot_A",
        "actor_id": "admin_1",
        "actor_role": "admin",
        "snapshot_context": [
            {"source": "provider_status.db_schema_snapshot", "payload": table}
            for table in tables
        ],
        "embedding_context": [],
    }
    base.update(extra)
    return base


CUSTOMERS = {
    "schema": "public",
    "name": "customers",
    "primary_key": ["customer_id"],
    "columns": [
        {"name": "customer_id", "type": "integer", "nullable": False},
        {"name": "full_name", "type": "text", "nullable": True},
        {"name": "phone", "type": "text", "nullable": True},
        {"name": "email", "type": "text", "nullable": True},
    ],
}

ORDERS = {
    "schema": "public",
    "name": "orders",
    "primary_key": ["order_id"],
    "columns": [
        {"name": "order_id", "type": "integer", "nullable": False},
        {"name": "customer_id", "type": "integer", "nullable": True},
        {"name": "status", "type": "text", "nullable": True},
    ],
}

ECOMMERCE_ORDERS = {
    "schema": "ecommerce_callcenter",
    "name": "orders",
    "primary_key": ["order_id"],
    "columns": [
        {"name": "order_id", "type": "integer", "nullable": False},
        {"name": "order_number", "type": "text", "nullable": True},
        {"name": "customer_id", "type": "integer", "nullable": True},
        {"name": "order_status", "type": "text", "nullable": True},
        {"name": "created_at", "type": "timestamp", "nullable": True},
    ],
}

SUPPORT_TICKETS = {
    "schema": "public",
    "name": "support_tickets",
    "primary_key": ["ticket_id"],
    "columns": [
        {"name": "ticket_id", "type": "integer", "nullable": False},
        {"name": "customer_id", "type": "integer", "nullable": True},
        {"name": "subject", "type": "text", "nullable": False},
        {"name": "status", "type": "text", "nullable": True},
    ],
}

CALL_LOGS = {
    "schema": "public",
    "name": "call_logs",
    "primary_key": ["call_log_id"],
    "columns": [
        {"name": "call_log_id", "type": "integer", "nullable": False},
        {"name": "ticket_id", "type": "integer", "nullable": True},
    ],
}

CUSTOMERS_WITH_USERNAME = {
    "schema": "ecommerce_callcenter",
    "name": "customers",
    "primary_key": ["customer_id"],
    "columns": [
        {"name": "customer_id", "type": "integer", "nullable": False},
        {"name": "customer_code", "type": "text", "nullable": True},
        {"name": "full_name", "type": "text", "nullable": True},
        {"name": "phone", "type": "text", "nullable": True},
        {"name": "username", "type": "text", "nullable": True},
    ],
}


def test_create_tool_to_find_customer_by_phone_is_read_select():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "create a tool to find customer by phone",
        db_context(CUSTOMERS),
    )

    tool = result["tool"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert result["tool_plan"]["operation_type"] == "read"
    assert tool["execution"]["mode"] == "read_only"
    assert tool["execution"]["mapping"]["allowed_statements"] == ["SELECT"]
    assert tool["safety"]["risk_level"] == "low"
    assert tool["safety"]["approval_policy"]["human_approval_required"] is False
    assert tool["execution"]["mapping"]["sql_template"].startswith("SELECT ")
    assert "INSERT INTO" not in tool["execution"]["mapping"]["sql_template"]
    assert "idempotency_key" not in tool["input_schema"]["properties"]
    record_props = tool["output_schema"]["properties"]["data"]["properties"]["records"]["items"]["properties"]
    assert "phone_last4" in record_props
    assert "email_masked" in record_props
    assert "phone" not in record_props
    assert "email" not in record_props
    assert "match_confidence" not in tool["output_schema"]["properties"]["data"]["properties"]


def test_build_tool_to_check_order_status_is_read_select():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "build a tool to check order status",
        db_context(ORDERS),
    )

    tool = result["tool"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert result["tool_plan"]["operation_type"] == "read"
    assert tool["name"] == "check_order_status"
    assert tool["execution"]["mapping"]["allowed_statements"] == ["SELECT"]
    assert tool["execution"]["mapping"]["sql_template"].startswith("SELECT ")


def test_create_support_ticket_is_create_insert():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "create a support ticket",
        db_context(SUPPORT_TICKETS),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert result["tool_plan"]["operation_type"] == "create"
    assert tool["name"] == "create_support_ticket"
    assert tool["execution"]["mode"] == "write_requires_human_approval"
    assert tool["execution"]["mapping"]["allowed_statements"] == ["INSERT"]
    assert sql.startswith("INSERT INTO public.support_tickets")
    assert "idempotency_key" in tool["input_schema"]["properties"]


def test_related_customer_phone_lookup_generates_expected_read_tool_and_joins():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Find customer details and related customer records by phone",
        db_context(CUSTOMERS, ORDERS, SUPPORT_TICKETS, CALL_LOGS),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert tool["name"] == "lookup_customer_details_by_phone"
    assert result["tool_plan"]["operation_type"] == "read"
    assert tool["execution"]["mapping"]["allowed_statements"] == ["SELECT"]
    assert tool["execution"]["mode"] == "read_only"
    assert "FROM public.customers" in sql
    assert "LEFT JOIN public.orders" in sql
    assert "LEFT JOIN public.support_tickets" in sql
    assert "LEFT JOIN public.call_logs" in sql
    assert "c.phone = :phone_number" in sql
    assert "RIGHT(CAST(c.phone AS TEXT), 4) AS phone_last4" in sql
    assert sql.startswith("WITH customer_base AS")
    assert "GROUP BY cb.customer_id" in sql
    assert "AS email_masked" in sql
    assert "customer_name" not in tool["input_schema"]["properties"]
    assert "full_name = :customer_name" not in sql
    assert "INSERT INTO" not in sql
    record_props = tool["output_schema"]["properties"]["data"]["properties"]["records"]["items"]["properties"]
    assert "phone_last4" in record_props
    assert "email_masked" in record_props
    assert "phone" not in record_props
    assert "email" not in record_props
    assert record_props["orders_count"]["type"] == "integer"
    assert record_props["support_tickets_count"]["type"] == "integer"
    assert record_props["call_logs_count"]["type"] == "integer"
    assert "match_confidence" not in tool["output_schema"]["properties"]["data"]["properties"]
    assert tool["execution"]["mapping"]["search_strategy"]["searchable_columns"] == ["public.customers.phone"]


def test_model_table_name_is_not_used_as_tool_name():
    result = ToolkitGeneratorService._sanitize_tool(
        {"name": "call_logs", "title": "Postgresql generated tool"},
        "database",
        "postgresql",
        "Find customer details and related customer records by phone",
        db_context(CUSTOMERS, ORDERS, SUPPORT_TICKETS, CALL_LOGS),
    )

    tool = result["tool"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert tool["name"] == "lookup_customer_details_by_phone"
    assert tool["title"] == "Lookup Customer Details By Phone"
    assert "Postgresql generated tool" not in tool["title"]
    assert "Draft generated from indexed integration context" not in tool["description"]


def test_name_generation_failure_blocks_uninferable_database_prompt():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "show information",
        db_context(CUSTOMERS),
    )

    assert result["validation"]["status"] == "failed"
    assert result["review"]["status"] == "rejected"
    assert result["tool"]["name"] == "name_generation_failed"
    assert "No verified database table/column context matched the request." in result["tool"]["missing_context"]


def test_read_plus_update_prompt_generates_workflow_not_read_only():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "build a tool to retrieve customer details using their phone number, and accordingly change their username to the one requested by the customer",
        db_context(CUSTOMERS_WITH_USERNAME, ORDERS, SUPPORT_TICKETS, CALL_LOGS),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert result["tool_plan"]["operation_type"] == "workflow"
    assert result["tool_plan"]["workflow_type"] == "read_then_update"
    assert set(result["retrieval"]["candidate_resources"]) >= {
        "ecommerce_callcenter.customers",
        "public.orders",
        "public.support_tickets",
        "public.call_logs",
    }
    assert [item["name"] for item in result["retrieval"]["verified_resources_used"]] == ["ecommerce_callcenter.customers"]
    assert result["tool_plan"]["exact_resources_used"] == ["ecommerce_callcenter.customers"]
    assert result["tool_plan"]["trusted_resources_used"] == ["ecommerce_callcenter.customers"]
    assert tool["execution"]["mapping"]["allowed_tables"] == ["ecommerce_callcenter.customers"]
    assert {item["resource"] for item in tool["source_context"]} == {"ecommerce_callcenter.customers"}
    assert tool["execution"]["mode"] == "write_requires_human_approval"
    assert tool["safety"]["approval_policy"]["human_approval_required"] is True
    assert tool["name"] == "update_customer_username_by_phone"
    assert result["tool_plan"]["resolved_target_column"] == "ecommerce_callcenter.customers.username"
    assert sql.startswith("WITH matched_customer AS")
    assert "UPDATE ecommerce_callcenter.customers" in sql
    assert "SET username = :new_username" in sql
    assert "RETURNING c.customer_id" in sql
    assert "SELECT " not in sql.split("UPDATE ecommerce_callcenter.customers", 1)[1]
    mutation_props = tool["output_schema"]["properties"]["data"]["properties"]
    assert "updated_username" in mutation_props
    field_policy = tool["safety"]["pii_policy"]["field_policy"]
    assert "ecommerce_callcenter.customers.phone" in field_policy
    assert "ecommerce_callcenter.customers.username" in field_policy
    assert "phone_last4" in field_policy
    assert not any(key.startswith("public.orders") for key in field_policy)
    assert not any(key.startswith("public.support_tickets") for key in field_policy)
    assert not any(key.startswith("public.call_logs") for key in field_policy)
    assert "ecommerce_callcenter.customers.email" not in field_policy


def test_username_missing_requires_confirmation_instead_of_silent_full_name_update():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "build a tool to retrieve customer details using their phone number, and accordingly change their username to the one requested by the customer",
        db_context({**CUSTOMERS, "schema": "ecommerce_callcenter"}, ORDERS, SUPPORT_TICKETS, CALL_LOGS),
    )

    assert result["validation"]["status"] == "failed"
    assert result["review"]["status"] == "needs_context_confirmation"
    assert result["tool_plan"]["exact_resources_used"] == ["ecommerce_callcenter.customers"]
    assert "No verified username column found. Confirm whether full_name should be used as the username field." in result["tool"]["missing_context"]
    assert "SET full_name" not in result["tool"]["execution"]["mapping"]["sql_template"]
    assert {error["code"] for error in result["validation"]["errors"]} >= {"MUTATION_SQL_BLOCKED_DUE_TO_MISSING_TARGET_FIELD", "MISSING_TARGET_FIELD_CONFIRMATION"}


def test_username_confirmation_allows_full_name_update_sql():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "build a tool to retrieve customer details using their phone number, and accordingly change their username to the one requested by the customer",
        db_context(
            {**CUSTOMERS, "schema": "ecommerce_callcenter", "columns": [{"name": "customer_id", "type": "integer", "nullable": False}, {"name": "customer_code", "type": "text", "nullable": True}, {"name": "full_name", "type": "text", "nullable": True}, {"name": "phone", "type": "text", "nullable": True}]},
            admin_field_confirmations={"username_field": "full_name"},
        ),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert result["review"]["status"] == "draft"
    assert result["validation"]["checks"]["sql_parser"] == "passed"
    assert result["validation"]["checks"]["sql_explain"] == "passed"
    assert result["tool_plan"]["exact_resources_used"] == ["ecommerce_callcenter.customers"]
    assert result["tool_plan"]["trusted_resources_used"] == ["ecommerce_callcenter.customers"]
    assert result["tool_plan"]["resolved_target_column"] == "ecommerce_callcenter.customers.full_name"
    assert [item["name"] for item in result["retrieval"]["verified_resources_used"]] == ["ecommerce_callcenter.customers"]
    assert tool["execution"]["mapping"]["allowed_tables"] == ["ecommerce_callcenter.customers"]
    assert {item["resource"] for item in tool["source_context"]} == {"ecommerce_callcenter.customers"}
    assert "UPDATE ecommerce_callcenter.customers c" in sql
    assert "SET full_name = :new_username" in sql
    assert "WHERE c.phone = :phone_number" in sql
    assert "c.full_name AS updated_username" in sql
    assert "c.customer_code" in sql
    mutation_props = tool["output_schema"]["properties"]["data"]["properties"]
    assert "updated_username" in mutation_props
    assert result["publish_gate"]["can_publish"] is False
    assert "tests.status must be passed" in result["publish_gate"]["missing_requirements"]
    assert "review.status must be approved" in result["publish_gate"]["missing_requirements"]
    assert "admin approval record is required" in result["publish_gate"]["missing_requirements"]
    field_policy = tool["safety"]["pii_policy"]["field_policy"]
    assert set(field_policy) == {
        "ecommerce_callcenter.customers.customer_id",
        "ecommerce_callcenter.customers.customer_code",
        "ecommerce_callcenter.customers.full_name",
        "ecommerce_callcenter.customers.phone",
        "affected_count",
        "idempotency_key",
        "customer_id",
        "customer_code",
        "phone_last4",
    }
    assert not any(key.startswith("public.orders") for key in field_policy)
    assert not any(key.startswith("public.support_tickets") for key in field_policy)
    assert not any(key.startswith("public.call_logs") for key in field_policy)
    assert "ecommerce_callcenter.customers.email" not in field_policy


def test_full_name_update_by_phone_generates_fixed_customer_workflow():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Build a tool to find a customer using their phone number and change their full name to the new name provided.",
        db_context(
            {
                **CUSTOMERS,
                "schema": "ecommerce_callcenter",
                "columns": [
                    {"name": "customer_id", "type": "integer", "nullable": False},
                    {"name": "customer_code", "type": "text", "nullable": True},
                    {"name": "full_name", "type": "text", "nullable": True},
                    {"name": "phone", "type": "text", "nullable": True},
                ],
            },
        ),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    input_props = tool["input_schema"]["properties"]
    mutation_props = tool["output_schema"]["properties"]["data"]["properties"]
    field_policy = tool["safety"]["pii_policy"]["field_policy"]

    assert result["validation"]["status"] == "passed", result["validation"]
    assert tool["name"] == "update_customer_full_name_by_phone"
    assert result["tool_plan"]["operation_type"] == "workflow"
    assert result["tool_plan"]["workflow_type"] == "read_then_update"
    assert result["tool_plan"]["target_entity"] == "customer"
    assert result["tool_plan"]["target_field"] == "full_name"
    assert result["tool_plan"]["resolved_target_column"] == "ecommerce_callcenter.customers.full_name"
    assert result["tool_plan"]["exact_resources_used"] == ["ecommerce_callcenter.customers"]
    assert tool["execution"]["mode"] == "write_requires_human_approval"
    assert tool["safety"]["risk_level"] == "medium"
    assert tool["safety"]["approval_policy"]["human_approval_required"] is True
    assert set(input_props) == {"phone_number", "new_full_name", "idempotency_key"}
    assert "customer_name" not in input_props
    assert tool["execution"]["mapping"]["parameter_binding"]["sources"] == [
        "input.phone_number",
        "input.new_full_name",
        "input.idempotency_key",
    ]
    assert sql.startswith("WITH matched_customer AS")
    assert "FROM ecommerce_callcenter.customers c" in sql
    assert "WHERE c.phone = :phone_number" in sql
    assert "UPDATE ecommerce_callcenter.customers c" in sql
    assert "SET full_name = :new_full_name" in sql
    assert "c.full_name AS updated_full_name" in sql
    assert "LIMIT 1" not in sql
    assert set(result["tool_plan"]["output_fields"][index]["name"] for index in range(len(result["tool_plan"]["output_fields"]))) >= {
        "affected_count",
        "idempotency_key",
        "customer_id",
        "customer_code",
        "phone_last4",
        "updated_full_name",
    }
    assert {"affected_count", "idempotency_key", "customer_id", "customer_code", "phone_last4", "updated_full_name"}.issubset(mutation_props)
    assert set(field_policy) == {
        "ecommerce_callcenter.customers.customer_id",
        "ecommerce_callcenter.customers.customer_code",
        "ecommerce_callcenter.customers.full_name",
        "ecommerce_callcenter.customers.phone",
        "affected_count",
        "idempotency_key",
        "customer_id",
        "customer_code",
        "phone_last4",
    }
    assert field_policy["ecommerce_callcenter.customers.full_name"]["output_name"] == "updated_full_name"
    assert field_policy["ecommerce_callcenter.customers.phone"]["output_name"] == "phone_last4"
    assert result["publish_gate"]["can_publish"] is False
    assert "tests.status must be passed" in result["publish_gate"]["missing_requirements"]
    assert "review.status must be approved" in result["publish_gate"]["missing_requirements"]
    assert "admin approval record is required" in result["publish_gate"]["missing_requirements"]


def test_read_plus_update_order_status_prompt_generates_order_workflow():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "build a tool to retrieve orders of a user using their name and phone number and the status of the order must be changed",
        db_context(
            {**CUSTOMERS, "schema": "ecommerce_callcenter", "columns": [{"name": "customer_id", "type": "integer", "nullable": False}, {"name": "customer_code", "type": "text", "nullable": True}, {"name": "full_name", "type": "text", "nullable": True}, {"name": "phone", "type": "text", "nullable": True}]},
            ECOMMERCE_ORDERS,
        ),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    input_props = tool["input_schema"]["properties"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert result["tool_plan"]["operation_type"] == "workflow"
    assert result["tool_plan"]["workflow_type"] == "read_then_update"
    assert result["tool_plan"]["target_entity"] == "order"
    assert result["tool_plan"]["target_field"] == "order_status"
    assert result["tool_plan"]["resolved_target_column"] == "ecommerce_callcenter.orders.order_status"
    assert result["tool_plan"]["exact_resources_used"] == ["ecommerce_callcenter.orders", "ecommerce_callcenter.customers"]
    assert tool["execution"]["mode"] == "write_requires_human_approval"
    assert tool["safety"]["risk_level"] == "medium"
    assert tool["safety"]["approval_policy"]["human_approval_required"] is True
    assert tool["execution"]["mapping"]["allowed_statements"] == ["UPDATE"]
    assert "customer_name" in input_props
    assert "phone_number" in input_props
    assert "new_order_status" in input_props
    assert "idempotency_key" in input_props
    assert "order_id" in input_props
    assert "order_number" in input_props
    assert "candidate_count AS" in sql
    assert "cc.match_count = 1" in sql
    assert "CROSS JOIN candidate_count cc" in sql
    assert "LIMIT 1" not in sql
    assert "UPDATE ecommerce_callcenter.orders AS o" in sql
    assert "SET order_status = :new_order_status" in sql
    assert "LOWER(c.full_name) = LOWER(:customer_name)" in sql
    assert "c.phone = :phone_number" in sql
    assert "co.order_number" in sql
    assert ":order_number IS NOT NULL" in sql
    assert "o.order_status AS updated_order_status" in sql
    mutation_props = tool["output_schema"]["properties"]["data"]["properties"]
    assert "order_number" in mutation_props
    assert "updated_order_status" in mutation_props
    precheck = tool["execution"]["mapping"]["precheck"]
    assert precheck["required"] is True
    assert precheck["must_pass_before_mutation"] is True
    assert "cc.match_count = 0 THEN 'NO_MATCH'" in precheck["sql_template"]
    assert "cc.match_count > 1" in precheck["sql_template"]
    assert set(precheck["result_mapping"]) == {"NO_MATCH", "ORDER_TARGET_AMBIGUOUS", "READY"}
    assert precheck["result_mapping"]["NO_MATCH"]["block_update"] is True
    assert precheck["result_mapping"]["ORDER_TARGET_AMBIGUOUS"]["block_update"] is True
    assert precheck["result_mapping"]["READY"]["block_update"] is False
    assert "records" not in mutation_props


def test_latest_order_status_by_phone_requires_latest_order_policy():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Build a tool to update the latest order status of a customer using only their phone number.",
        db_context(
            {**CUSTOMERS, "schema": "ecommerce_callcenter", "columns": [{"name": "customer_id", "type": "integer", "nullable": False}, {"name": "customer_code", "type": "text", "nullable": True}, {"name": "phone", "type": "text", "nullable": True}]},
            ECOMMERCE_ORDERS,
        ),
    )

    tool = result["tool"]
    input_props = tool["input_schema"]["properties"]
    codes = {error["code"] for error in result["validation"]["errors"]}
    assert tool["name"] == "update_latest_order_status_by_phone"
    assert result["tool_plan"]["operation_type"] == "workflow"
    assert result["tool_plan"]["workflow_type"] == "read_then_update"
    assert result["tool_plan"]["target_entity"] == "order"
    assert result["tool_plan"]["target_field"] == "order_status"
    assert result["tool_plan"]["resolved_target_column"] == "ecommerce_callcenter.orders.order_status"
    assert set(input_props) == {"phone_number", "new_order_status", "idempotency_key"}
    assert "customer_name" not in input_props
    assert tool["execution"]["mapping"]["sql_template"] == ""
    assert result["validation"]["status"] == "failed"
    assert result["review"]["status"] == "needs_context_confirmation"
    assert "LATEST_ORDER_POLICY_REQUIRED" in codes
    assert "Confirm whether latest order means the order with the greatest created_at for the customer." in tool["missing_context"]


def test_latest_order_status_by_phone_with_policy_generates_fixed_update_sql():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Build a tool to update the latest order status of a customer using only their phone number.",
        db_context(
            {**CUSTOMERS, "schema": "ecommerce_callcenter", "columns": [{"name": "customer_id", "type": "integer", "nullable": False}, {"name": "customer_code", "type": "text", "nullable": True}, {"name": "phone", "type": "text", "nullable": True}]},
            ECOMMERCE_ORDERS,
            admin_policy_confirmations={"latest_order_policy": "created_at_desc"},
        ),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    input_props = tool["input_schema"]["properties"]
    mutation_props = tool["output_schema"]["properties"]["data"]["properties"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert tool["name"] == "update_latest_order_status_by_phone"
    assert result["tool_plan"]["operation_type"] == "workflow"
    assert result["tool_plan"]["workflow_type"] == "read_then_update"
    assert result["tool_plan"]["target_entity"] == "order"
    assert result["tool_plan"]["target_field"] == "order_status"
    assert result["tool_plan"]["resolved_target_column"] == "ecommerce_callcenter.orders.order_status"
    assert result["tool_plan"]["exact_resources_used"] == ["ecommerce_callcenter.orders", "ecommerce_callcenter.customers"]
    assert set(input_props) == {"phone_number", "new_order_status", "idempotency_key"}
    assert "customer_name" not in input_props
    assert "order_id" not in input_props
    assert "order_number" not in input_props
    assert tool["execution"]["mapping"]["parameter_binding"]["sources"] == [
        "input.phone_number",
        "input.new_order_status",
        "input.idempotency_key",
    ]
    assert "WITH matched_customer AS" in sql
    assert "WHERE c.phone = :phone_number" in sql
    assert "latest_order AS" in sql
    assert "ORDER BY o.created_at DESC, o.order_id DESC" in sql
    assert "LIMIT 1" in sql
    assert "UPDATE ecommerce_callcenter.orders AS o" in sql
    assert "SET order_status = :new_order_status" in sql
    assert "SET order_number" not in sql
    assert "o.order_status AS updated_order_status" in sql
    assert "customer_name" not in sql
    assert {"affected_count", "idempotency_key", "order_id", "order_number", "customer_code", "phone_last4", "updated_order_status"}.issubset(mutation_props)
    assert result["publish_gate"]["can_publish"] is False


def test_bulk_mark_pending_orders_as_delivered_requires_bulk_policy_and_preview_only():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Build a tool to mark all pending orders as delivered.",
        db_context(ECOMMERCE_ORDERS),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    input_props = tool["input_schema"]["properties"]
    codes = {error["code"] for error in result["validation"]["errors"]}
    assert tool["name"] == "bulk_mark_pending_orders_as_delivered"
    assert result["tool_plan"]["operation_type"] == "bulk_update"
    assert result["tool_plan"]["target_entity"] == "order"
    assert result["tool_plan"]["target_field"] == "order_status"
    assert result["tool_plan"]["resolved_target_column"] == "ecommerce_callcenter.orders.order_status"
    assert tool["safety"]["risk_level"] == "high"
    assert tool["safety"]["approval_policy"]["human_approval_required"] is True
    assert tool["safety"]["approval_policy"]["admin_approval_required"] is True
    assert set(input_props) == {"idempotency_key", "approval_reason", "dry_run", "max_rows", "effective_before"}
    assert "new_order_status" not in input_props
    assert tool["execution"]["mapping"]["allowed_statements"] == ["SELECT"]
    assert tool["execution"]["mapping"]["bulk_mutation"]["policy_approved"] is False
    assert tool["execution"]["mapping"]["dry_run_required"] is True
    assert tool["execution"]["mapping"]["preview_required"] is True
    assert tool["execution"]["mapping"]["batch_limit_required"] is True
    assert sql == "SELECT COUNT(*)::int AS affected_count_preview FROM ecommerce_callcenter.orders WHERE order_status = 'pending'"
    assert "UPDATE" not in sql
    assert result["validation"]["status"] == "failed"
    assert result["review"]["status"] == "needs_context_confirmation"
    assert "BULK_MUTATION_POLICY_REQUIRED" in codes
    assert "BULK_MUTATION_LIMIT_REQUIRED" not in codes
    assert "PROMPT_VALUE_EXTRACTION_FAILED" not in codes


def test_bulk_mark_pending_orders_as_delivered_with_policy_generates_reviewed_update():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Build a tool to mark all pending orders as delivered.",
        db_context(ECOMMERCE_ORDERS, admin_policy_confirmations={"bulk_mutation_policy": "order_status_pending_to_delivered"}),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    mutation_props = tool["output_schema"]["properties"]["data"]["properties"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert tool["name"] == "bulk_mark_pending_orders_as_delivered"
    assert result["tool_plan"]["operation_type"] == "bulk_update"
    assert result["tool_plan"]["target_field"] == "order_status"
    assert result["tool_plan"]["resolved_target_column"] == "ecommerce_callcenter.orders.order_status"
    assert tool["execution"]["mapping"]["allowed_statements"] == ["UPDATE"]
    assert tool["execution"]["mapping"]["bulk_mutation"]["policy_approved"] is True
    assert "UPDATE ecommerce_callcenter.orders" in sql
    assert "SET order_status = 'delivered'" in sql
    assert "WHERE order_status = 'pending'" in sql
    assert "RETURNING order_id, order_number, order_status AS updated_order_status" in sql
    assert "order_number =" not in sql
    assert {"affected_count", "idempotency_key", "order_id", "order_number", "updated_order_status"}.issubset(mutation_props)
    assert result["publish_gate"]["can_publish"] is False


def test_delete_customer_by_phone_requires_delete_policy_and_blocks_hard_delete():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Build a tool to delete a customer account using their phone number.",
        db_context({**CUSTOMERS, "schema": "ecommerce_callcenter"}),
    )

    tool = result["tool"]
    codes = {error["code"] for error in result["validation"]["errors"]}
    input_props = tool["input_schema"]["properties"]
    assert tool["name"] == "delete_customer_by_phone"
    assert result["tool_plan"]["operation_type"] == "delete"
    assert result["tool_plan"]["target_entity"] == "customer"
    assert tool["safety"]["risk_level"] == "high"
    assert tool["safety"]["approval_policy"]["human_approval_required"] is True
    assert tool["safety"]["approval_policy"]["admin_approval_required"] is True
    assert set(input_props) == {"phone_number", "idempotency_key"}
    assert tool["execution"]["mapping"]["sql_template"] == ""
    assert result["validation"]["status"] == "failed"
    assert result["review"]["status"] == "needs_context_confirmation"
    assert "DELETE_POLICY_REQUIRED" in codes
    assert "SOFT_DELETE_TARGET_FIELD_REQUIRED" in codes
    assert "LINKED_RECORDS_POLICY_REQUIRED" in codes
    assert "UNBOUND_SQL_PARAMETER" not in codes


def test_delete_customer_by_phone_with_soft_delete_policy_uses_phone_lookup_update():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Build a tool to delete a customer account using their phone number.",
        db_context(
            {
                **CUSTOMERS,
                "schema": "ecommerce_callcenter",
                "columns": [
                    {"name": "customer_id", "type": "integer", "nullable": False},
                    {"name": "phone", "type": "text", "nullable": True},
                    {"name": "deleted_at", "type": "timestamp", "nullable": True},
                ],
            },
            admin_policy_confirmations={
                "delete_policy": {
                    "soft_delete_available": True,
                    "retention_policy_confirmed": True,
                    "linked_records_policy_confirmed": True,
                }
            },
        ),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert tool["name"] == "delete_customer_by_phone"
    assert result["tool_plan"]["operation_type"] == "delete"
    assert tool["execution"]["mapping"]["allowed_statements"] == ["UPDATE"]
    assert "WITH matched_customer AS" in sql
    assert "WHERE phone = :phone_number" in sql
    assert "UPDATE ecommerce_callcenter.customers c" in sql
    assert "SET deleted_at = now()" in sql
    assert "DELETE FROM" not in sql
    assert "RETURNING c.customer_id AS deleted_id" in sql
    assert result["publish_gate"]["can_publish"] is False


def test_delete_customer_by_phone_hard_delete_requires_explicit_policy_and_uses_phone_lookup():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Build a tool to delete a customer account using their phone number.",
        db_context(
            {**CUSTOMERS, "schema": "ecommerce_callcenter"},
            admin_policy_confirmations={
                "delete_policy": {
                    "hard_delete_allowed": True,
                    "retention_policy_confirmed": True,
                    "linked_records_policy_confirmed": True,
                }
            },
        ),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert "WITH matched_customer AS" in sql
    assert "WHERE phone = :phone_number" in sql
    assert "DELETE FROM ecommerce_callcenter.customers c" in sql
    assert "USING matched_customer mc" in sql
    assert "RETURNING c.customer_id AS deleted_id" in sql


def test_sensitive_customer_payment_lookup_by_order_id_masks_pii_and_blocks_payment_details():
    customer_table = {
        **CUSTOMERS,
        "schema": "ecommerce_callcenter",
        "columns": [
            {"name": "customer_id", "type": "integer", "nullable": False},
            {"name": "customer_code", "type": "text", "nullable": True},
            {"name": "full_name", "type": "text", "nullable": True},
            {"name": "email", "type": "text", "nullable": True},
            {"name": "phone", "type": "text", "nullable": True},
            {"name": "city", "type": "text", "nullable": True},
        ],
    }
    order_table = {
        **ECOMMERCE_ORDERS,
        "columns": [
            *ECOMMERCE_ORDERS["columns"],
            {"name": "payment_status", "type": "text", "nullable": True},
            {"name": "total_amount", "type": "numeric", "nullable": True},
        ],
    }
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Build a tool to fetch the customer’s full phone number, email, address, and all payment details by order ID.",
        db_context(customer_table, order_table, SUPPORT_TICKETS, CALL_LOGS),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    input_props = tool["input_schema"]["properties"]
    record_props = tool["output_schema"]["properties"]["data"]["properties"]["records"]["items"]["properties"]
    codes = {error["code"] for error in result["validation"]["errors"]}
    assert result["tool_plan"]["operation_type"] == "read"
    assert result["tool_plan"]["target_entity"] == "order"
    assert result["tool_plan"]["risk_level"] == "high"
    assert result["tool_plan"]["intent_signals"]["sensitive_pii_requested"] is True
    assert result["tool_plan"]["intent_signals"]["payment_details_requested"] is True
    assert set(input_props) == {"order_id", "limit"}
    assert "phone_number" not in input_props
    assert "email" not in input_props
    assert result["tool_plan"]["exact_resources_used"] == ["ecommerce_callcenter.orders", "ecommerce_callcenter.customers"]
    assert not any("support_tickets" in resource for resource in result["tool_plan"]["exact_resources_used"])
    assert not any("call_logs" in resource for resource in result["tool_plan"]["exact_resources_used"])
    assert "WHERE CAST(o.order_id AS TEXT) = :order_id" in sql
    assert "RIGHT(CAST(c.phone AS TEXT), 4) AS phone_last4" in sql
    assert "AS email_masked" in sql
    assert "AS city_summary" in sql
    assert "c.phone," not in sql
    assert {"email_masked", "phone_last4", "city_summary", "order_id", "order_number", "order_status", "payment_status"}.issubset(record_props)
    assert "phone" not in record_props
    assert "email" not in record_props
    assert "address" not in record_props
    assert "total_amount" not in record_props
    assert result["validation"]["status"] == "failed"
    assert result["review"]["status"] == "needs_context_confirmation"
    assert "SENSITIVE_PII_REQUEST_BLOCKED" in codes
    assert "PAYMENT_DETAILS_NOT_VERIFIED" in codes
    assert "PAYMENT_POLICY_REQUIRED" in codes
    assert "UNUSED_INPUT_FIELD" not in codes
    assert "UNRELATED_RESOURCE_USED" not in codes
    assert "LOOKUP_FIELD_SQL_MISMATCH" not in codes
