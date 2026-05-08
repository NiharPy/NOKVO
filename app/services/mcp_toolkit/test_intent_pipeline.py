from __future__ import annotations

from app.services.toolkit_generator_service import ToolkitGeneratorService


def db_context(*tables: dict) -> dict:
    return {
        "snapshot_context": [
            {"source": "provider_status.db_schema_snapshot", "payload": table}
            for table in tables
        ],
        "embedding_context": [],
    }


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
