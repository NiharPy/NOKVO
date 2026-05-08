from __future__ import annotations

from copy import deepcopy

from app.services.mcp_toolkit.embedding_retriever import EmbeddingRetrievalRequest, EmbeddingRetriever
from app.services.mcp_toolkit.reviewer import Reviewer
from app.services.mcp_toolkit.sql_validator import SQLValidator
from app.services.mcp_toolkit.test_intent_pipeline import CALL_LOGS, CUSTOMERS, SUPPORT_TICKETS, db_context
from app.services.toolkit_generator_service import ToolkitGeneratorService


ORDERS_STATUS = {
    "schema": "public",
    "name": "orders",
    "primary_key": ["order_id"],
    "columns": [
        {"name": "order_id", "type": "integer", "nullable": False},
        {"name": "customer_id", "type": "integer", "nullable": True},
        {"name": "order_status", "type": "text", "nullable": True},
        {"name": "payment_status", "type": "text", "nullable": True},
        {"name": "shipment_partner", "type": "text", "nullable": True},
        {"name": "created_at", "type": "timestamp", "nullable": True},
    ],
}


def test_retrieval_customer_lookup_uses_scoped_postgresql_context():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Create a tool to find customer details by phone",
        db_context(CUSTOMERS, ORDERS_STATUS, SUPPORT_TICKETS),
    )

    assert result["retrieval"]["status"] == "passed", result["retrieval"]
    assert result["retrieval"]["confidence"] >= 0.80
    assert any("customers phone customer_id" in query for query in result["retrieval"]["query_variants"])
    assert {item["name"] for item in result["retrieval"]["verified_resources"]} >= {"public.customers"}
    assert result["tool_plan"]["operation_type"] == "read"
    assert "c.phone = :phone_number" in result["tool"]["execution"]["mapping"]["sql_template"]
    assert {item["provider"] for item in result["tool"]["source_context"]} == {"postgresql"}


def test_retrieval_order_status_by_phone_joins_customers_orders():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Create a tool to check latest order status by phone",
        db_context(CUSTOMERS, ORDERS_STATUS),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    record_props = tool["output_schema"]["properties"]["data"]["properties"]["records"]["items"]["properties"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert result["tool_plan"]["exact_resources_used"] == ["public.customers", "public.orders"]
    assert tool["name"] == "check_latest_order_status_by_phone"
    assert "LEFT JOIN public.orders" in sql
    assert "public.customers.phone = :phone_number" in sql
    assert "order_status" in record_props
    assert "payment_status" in record_props
    assert "shipment_partner" in record_props


def test_retrieval_call_history_by_phone_uses_call_logs():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Create a tool to show customer call history by phone",
        db_context(CUSTOMERS, SUPPORT_TICKETS, CALL_LOGS),
    )

    tool = result["tool"]
    sql = tool["execution"]["mapping"]["sql_template"]
    assert result["validation"]["status"] == "passed", result["validation"]
    assert tool["name"] == "lookup_customer_call_history_by_phone"
    assert result["tool_plan"]["exact_resources_used"] == ["public.customers", "public.support_tickets", "public.call_logs"]
    assert "LEFT JOIN public.call_logs" in sql


def test_retrieval_rejects_cross_provider_contamination_without_leaking_context():
    context = db_context(CUSTOMERS)
    context["embedding_context"] = [
        {
            "source": "qdrant",
            "score": 0.4,
            "payload": {
                "source_type": "crm_schema",
                "integration_type": "crm",
                "provider": "zoho",
                "module": "Contacts",
                "text": "Zoho Contacts phone email",
            },
        },
        {
            "source": "qdrant",
            "score": 0.4,
            "payload": {
                "source_type": "erp_schema",
                "integration_type": "erp",
                "provider": "tally",
                "module": "Ledgers",
                "text": "Tally ledgers customer phone",
            },
        },
    ]

    result = ToolkitGeneratorService._sanitize_tool({}, "database", "postgresql", "Create a tool to find customer by phone", context)

    assert result["validation"]["status"] == "passed", result["validation"]
    rejected_reasons = {chunk["reason"] for chunk in result["retrieval"]["rejected_chunks"]}
    assert "PROVIDER_MISMATCH" in rejected_reasons or "SOURCE_KIND_NOT_ALLOWED" in rejected_reasons
    assert {item["provider"] for item in result["tool"]["source_context"]} == {"postgresql"}


def test_reviewer_rejects_wrong_snapshot_context():
    result = ToolkitGeneratorService._sanitize_tool({}, "database", "postgresql", "Create a tool to find customer by phone", db_context(CUSTOMERS))
    mutated = deepcopy(result)
    mutated["retrieval"]["rejected_chunks"] = [
        {
            "context_id": "wrong_snapshot",
            "reason": "CONTEXT_SNAPSHOT_MISMATCH",
            "retrieval_score": 0.92,
        }
    ]

    reviewed = Reviewer.validate(mutated, {"allowed_tables": ["public.customers"], "tables": []})

    assert reviewed["validation"]["status"] == "failed"
    assert "CONTEXT_SNAPSHOT_MISMATCH" in {error["code"] for error in reviewed["validation"]["errors"]}


def test_missing_integration_selection_blocks_generation():
    result = ToolkitGeneratorService._sanitize_tool({}, "database", "postgresql", "Create a tool to find customer by phone", {"snapshot_context": [], "embedding_context": []})

    assert result["retrieval"]["status"] == "failed"
    assert result["validation"]["status"] == "failed"
    assert result["review"]["status"] == "rejected"
    assert not result["tool"]["execution"]["mapping"]["sql_template"]


def test_low_confidence_retrieval_blocks_publish():
    result = ToolkitGeneratorService._sanitize_tool({}, "database", "postgresql", "make a tool for logs", db_context(CALL_LOGS))

    assert result["retrieval"]["status"] in {"low_confidence", "failed"}
    assert result["review"]["status"] == "rejected"
    assert result["publish_gate"]["can_publish"] is False


def test_reviewer_rejects_unverified_table_usage():
    result = ToolkitGeneratorService._sanitize_tool({}, "database", "postgresql", "Create a tool to find customer by phone", db_context(CUSTOMERS))
    mutated = deepcopy(result)
    mutated["tool"]["execution"]["mapping"]["sql_template"] = mutated["tool"]["execution"]["mapping"]["sql_template"].replace("public.customers", "public.orders")
    mutated["tool"]["execution"]["mapping"]["allowed_tables"] = ["public.orders"]
    mutated["tool_plan"]["exact_resources_used"] = ["public.orders"]

    reviewed = Reviewer.validate(mutated, {"allowed_tables": ["public.customers"], "tables": []})

    assert reviewed["validation"]["status"] == "failed"
    assert "UNVERIFIED_RESOURCE_USED" in {error["code"] for error in reviewed["validation"]["errors"]}


def test_qdrant_missing_index_creates_low_confidence_warning_and_validation_failure():
    request = EmbeddingRetrievalRequest(
        organization_id="org_1",
        tenant_integration_id="tenant_1:database:postgresql",
        provider_connection_id="provider_conn_1",
        selected_context_snapshot_id="snapshot_A",
        integration_type="database",
        provider="postgresql",
        user_prompt="Create a tool to find customer by phone",
        operation_type="read",
        candidate_entities=["customer", "customers", "phone", "customer_id"],
        candidate_inputs=["phone_number", "phone"],
    )
    scope_filter = EmbeddingRetriever.scope_filter(request)
    chunks = EmbeddingRetriever.snapshot_chunks(request, db_context(CUSTOMERS)["snapshot_context"], scope_filter)
    chunks.append(
        {
            "context_id": "qdrant_error_missing_index",
            "source": "qdrant_search_error",
            "source_kind": "retrieval_error",
            "status": "rejected",
            "payload": {"error": "payload index required for organization_id", "error_code": "QDRANT_INDEX_MISSING"},
            "text": "payload index required for organization_id",
            "vector_score": 0.0,
            **{key: value for key, value in scope_filter.items() if key != "status"},
        }
    )

    retrieval = EmbeddingRetriever.rank_filter_verify(request, chunks, ["customer by phone"], scope_filter)

    assert retrieval["status"] == "low_confidence"
    assert any("QDRANT_INDEX_MISSING_FALLBACK_USED" in warning for warning in retrieval["warnings"])

    result = ToolkitGeneratorService._sanitize_tool({}, "database", "postgresql", "Create a tool to find customer by phone", db_context(CUSTOMERS))
    result["retrieval"] = retrieval
    reviewed = Reviewer.validate(result, {"allowed_tables": ["public.customers"], "tables": []})
    codes = {error["code"] for error in reviewed["validation"]["errors"]}
    assert reviewed["validation"]["status"] == "failed"
    assert "QDRANT_INDEX_MISSING" in codes
    assert "FALLBACK_RETRIEVAL_USED" in codes


def test_generated_group_by_with_masked_fields_uses_valid_cte_sql():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "postgresql",
        "Find customer details and related customer records by phone",
        db_context(CUSTOMERS, ORDERS_STATUS, SUPPORT_TICKETS, CALL_LOGS),
    )

    sql = result["tool"]["execution"]["mapping"]["sql_template"]
    sql_validation = SQLValidator.validate(sql, "postgresql", result["tool"]["execution"]["mapping"])
    assert result["validation"]["status"] == "passed", result["validation"]
    assert sql_validation["status"] == "passed", sql_validation
    assert sql.startswith("WITH customer_base AS")
    assert "GROUP BY cb.customer_id" in sql
    assert "GROUP BY CASE" not in sql
    assert "GROUP BY RIGHT" not in sql
    assert "GROUP BY LEFT(CAST" not in sql
