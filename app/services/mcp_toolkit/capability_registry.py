from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class ProviderCapability:
    integration_type: str
    provider: str
    execution_backend: str
    resource_model: str
    allowed_source_kinds: tuple[str, ...]
    supported_operations: tuple[str, ...]
    auth_requirements: dict[str, Any] = field(default_factory=dict)
    idempotency_requirements: dict[str, Any] = field(default_factory=dict)
    approval_requirements: dict[str, Any] = field(default_factory=dict)
    validation_requirements: dict[str, Any] = field(default_factory=dict)
    execution_compiler: str = ""
    builder_label: str = ""
    builder_description: str = ""
    cross_integration_supported: bool = False

    @property
    def builder_key(self) -> str:
        return f"{self.integration_type}:{self.provider}:{self.execution_backend}"


class ProviderCapabilityRegistry:
    _CAPABILITIES: tuple[ProviderCapability, ...] = (
        ProviderCapability(
            integration_type="database",
            provider="postgresql",
            execution_backend="database_sql",
            resource_model="sql_table",
            allowed_source_kinds=("db_schema_snapshot", "db_table", "db_column", "db_relationship", "db_constraint", "db_index", "approved_query_template"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "connection_string"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"workflow_mutation_required": True},
            validation_requirements={"sql_parser": True, "sql_explain": True},
            execution_compiler="SQLCompiler",
            builder_label="PostgreSQL Toolkit Builder",
            builder_description="Generate SQL-backed MCP tools from verified PostgreSQL schema and query context.",
        ),
        ProviderCapability(
            integration_type="database",
            provider="mysql",
            execution_backend="database_sql",
            resource_model="sql_table",
            allowed_source_kinds=("db_schema_snapshot", "db_table", "db_column", "db_relationship", "db_constraint", "db_index", "approved_query_template"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "connection_string"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"workflow_mutation_required": True},
            validation_requirements={"sql_parser": True},
            execution_compiler="SQLCompiler",
            builder_label="MySQL Toolkit Builder",
            builder_description="Generate SQL-backed MCP tools from verified MySQL schema context.",
        ),
        ProviderCapability(
            integration_type="database",
            provider="sqlserver",
            execution_backend="database_sql",
            resource_model="sql_table",
            allowed_source_kinds=("db_schema_snapshot", "db_table", "db_column", "db_relationship", "db_constraint", "db_index", "approved_query_template"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "connection_string"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"workflow_mutation_required": True},
            validation_requirements={"sql_parser": True},
            execution_compiler="SQLCompiler",
            builder_label="SQL Server Toolkit Builder",
            builder_description="Generate SQL-backed MCP tools from verified SQL Server schema context.",
        ),
        ProviderCapability(
            integration_type="database",
            provider="mongodb",
            execution_backend="database_nosql",
            resource_model="collection_document",
            allowed_source_kinds=("db_schema_snapshot", "db_table", "db_column", "db_index", "approved_query_template"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "connection_string"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"workflow_mutation_required": True},
            validation_requirements={"collection_exists": True, "safe_filters": True},
            execution_compiler="MongoCompiler",
            builder_label="MongoDB Toolkit Builder",
            builder_description="Generate MongoDB-backed MCP tools from verified collection and field metadata.",
        ),
        ProviderCapability(
            integration_type="crm",
            provider="zoho",
            execution_backend="connector_action",
            resource_model="crm_object",
            allowed_source_kinds=("crm_object_schema", "crm_field_schema", "crm_action_template", "connector_template", "oauth_scope_metadata"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "oauth"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True},
            validation_requirements={"oauth_scope_verification": True, "request_schema": True},
            execution_compiler="CRMConnectorCompiler",
            builder_label="Zoho CRM Toolkit Builder",
            builder_description="Generate connector-action MCP tools from verified Zoho CRM objects, fields, and actions.",
        ),
        ProviderCapability(
            integration_type="crm",
            provider="freshworks",
            execution_backend="connector_action",
            resource_model="crm_object",
            allowed_source_kinds=("crm_object_schema", "crm_field_schema", "crm_action_template", "connector_template", "oauth_scope_metadata"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "oauth_or_api_key"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True},
            validation_requirements={"request_schema": True},
            execution_compiler="CRMConnectorCompiler",
            builder_label="Freshworks CRM Toolkit Builder",
            builder_description="Generate connector-action MCP tools from verified Freshworks CRM metadata.",
        ),
        ProviderCapability(
            integration_type="crm",
            provider="salesforce",
            execution_backend="connector_action",
            resource_model="crm_object",
            allowed_source_kinds=("crm_object_schema", "crm_field_schema", "crm_action_template", "connector_template", "oauth_scope_metadata"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "oauth"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True},
            validation_requirements={"oauth_scope_verification": True, "request_schema": True},
            execution_compiler="CRMConnectorCompiler",
            builder_label="Salesforce Toolkit Builder",
            builder_description="Generate connector-action MCP tools from verified Salesforce objects and actions.",
        ),
        ProviderCapability(
            integration_type="crm",
            provider="hubspot",
            execution_backend="connector_action",
            resource_model="crm_object",
            allowed_source_kinds=("crm_object_schema", "crm_field_schema", "crm_action_template", "connector_template", "oauth_scope_metadata"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "oauth"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True},
            validation_requirements={"oauth_scope_verification": True, "request_schema": True},
            execution_compiler="CRMConnectorCompiler",
            builder_label="HubSpot Toolkit Builder",
            builder_description="Generate connector-action MCP tools from verified HubSpot CRM objects and actions.",
        ),
        ProviderCapability(
            integration_type="zoho_desk",
            provider="zoho",
            execution_backend="connector_action",
            resource_model="desk_object",
            allowed_source_kinds=("crm_object_schema", "crm_field_schema", "crm_action_template", "connector_template", "oauth_scope_metadata"),
            supported_operations=("read", "create", "update", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "oauth"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True},
            validation_requirements={"oauth_scope_verification": True, "request_schema": True},
            execution_compiler="CRMConnectorCompiler",
            builder_label="Zoho Desk Toolkit Builder",
            builder_description="Generate Desk ticket and helpdesk MCP tools from verified Zoho Desk actions.",
        ),
        ProviderCapability(
            integration_type="erp",
            provider="tally",
            execution_backend="erp_xml",
            resource_model="erp_business_object",
            allowed_source_kinds=("erp_object_schema", "erp_action_template", "connector_template", "business_operation_metadata"),
            supported_operations=("read", "create", "update", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "http_basic_or_session"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True, "policy_required": True},
            validation_requirements={"action_template": True, "company_context": True},
            execution_compiler="ERPConnectorCompiler",
            builder_label="Tally Toolkit Builder",
            builder_description="Generate ERP XML/action MCP tools from verified Tally objects, vouchers, and ledgers.",
        ),
        ProviderCapability(
            integration_type="erp",
            provider="sap_like",
            execution_backend="connector_action",
            resource_model="erp_business_object",
            allowed_source_kinds=("erp_object_schema", "erp_action_template", "connector_template", "business_operation_metadata"),
            supported_operations=("read", "create", "update", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "oauth_or_api_key"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True, "policy_required": True},
            validation_requirements={"action_template": True},
            execution_compiler="ERPConnectorCompiler",
            builder_label="ERP Toolkit Builder",
            builder_description="Generate ERP connector MCP tools from verified ERP business objects and actions.",
        ),
        ProviderCapability(
            integration_type="shipping",
            provider="shiprocket",
            execution_backend="connector_action",
            resource_model="shipping_action",
            allowed_source_kinds=("ecommerce_object_schema", "ecommerce_action_template", "connector_template", "api_endpoint", "api_operation"),
            supported_operations=("read", "create", "update", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "oauth_or_api_key"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True},
            validation_requirements={"request_schema": True, "rate_limit": True},
            execution_compiler="EcommerceCompiler",
            builder_label="Shiprocket Toolkit Builder",
            builder_description="Generate shipping and fulfillment MCP tools from verified Shiprocket actions.",
        ),
        ProviderCapability(
            integration_type="ecommerce",
            provider="shopify",
            execution_backend="connector_action",
            resource_model="commerce_object",
            allowed_source_kinds=("ecommerce_object_schema", "ecommerce_action_template", "order_metadata", "customer_metadata", "product_metadata", "refund_metadata"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "oauth_or_api_key"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"refund_or_payment_required": True},
            validation_requirements={"request_schema": True, "rate_limit": True},
            execution_compiler="EcommerceCompiler",
            builder_label="Shopify Toolkit Builder",
            builder_description="Generate ecommerce MCP tools from verified Shopify customers, orders, products, and refunds.",
        ),
        ProviderCapability(
            integration_type="ecommerce",
            provider="woocommerce",
            execution_backend="connector_action",
            resource_model="commerce_object",
            allowed_source_kinds=("ecommerce_object_schema", "ecommerce_action_template", "order_metadata", "customer_metadata", "product_metadata", "refund_metadata"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "api_key"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"refund_or_payment_required": True},
            validation_requirements={"request_schema": True, "rate_limit": True},
            execution_compiler="EcommerceCompiler",
            builder_label="WooCommerce Toolkit Builder",
            builder_description="Generate ecommerce MCP tools from verified WooCommerce entities and actions.",
        ),
        ProviderCapability(
            integration_type="his",
            provider="generic_his",
            execution_backend="his_action",
            resource_model="his_object",
            allowed_source_kinds=("his_object_schema", "his_action_template", "patient_safety_policy", "appointment_metadata", "billing_metadata", "lab_metadata", "doctor_metadata"),
            supported_operations=("read", "create", "update", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "oauth_or_api_key"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True, "clinical_policy_required": True},
            validation_requirements={"patient_safety_policy": True},
            execution_compiler="HISCompiler",
            builder_label="HIS Toolkit Builder",
            builder_description="Generate HIS MCP tools from verified patient, appointment, billing, and lab resources with policy checks.",
        ),
        ProviderCapability(
            integration_type="custom_api",
            provider="openapi",
            execution_backend="api_request",
            resource_model="api_endpoint",
            allowed_source_kinds=("openapi_spec", "api_endpoint", "api_operation", "request_schema", "response_schema", "auth_policy"),
            supported_operations=("read", "create", "update", "delete", "workflow"),
            auth_requirements={"mode": "tenant_secret_ref", "kind": "api_key_or_oauth"},
            idempotency_requirements={"mutation_required": True},
            approval_requirements={"mutation_required": True},
            validation_requirements={"request_schema": True, "response_schema": True, "auth_policy": True},
            execution_compiler="OpenAPICompiler",
            builder_label="Custom API Toolkit Builder",
            builder_description="Generate MCP tools from verified OpenAPI endpoints, schemas, and auth policies.",
            cross_integration_supported=True,
        ),
    )

    @classmethod
    def resolve(cls, integration_type: str, provider: str) -> ProviderCapability | None:
        integration_type = integration_type.strip().lower()
        provider = provider.strip().lower()
        for capability in cls._CAPABILITIES:
            if capability.integration_type == integration_type and capability.provider == provider:
                return capability
        for capability in cls._CAPABILITIES:
            if capability.integration_type == integration_type and capability.provider in {"openapi", "sap_like", "generic_his"}:
                return capability
        return None

    @classmethod
    def require(cls, integration_type: str, provider: str) -> ProviderCapability:
        capability = cls.resolve(integration_type, provider)
        if capability is None:
            raise ValueError(f"No provider capability registered for {integration_type}/{provider}")
        return capability

    @classmethod
    def allowed_source_kinds(cls, integration_type: str, provider: str) -> set[str]:
        capability = cls.resolve(integration_type, provider)
        return set(capability.allowed_source_kinds) if capability else set()

    @classmethod
    def list_capabilities(cls) -> list[ProviderCapability]:
        return list(cls._CAPABILITIES)
