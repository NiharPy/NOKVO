from __future__ import annotations

from app.services.mcp_toolkit.capability_registry import ProviderCapabilityRegistry
from app.services.toolkit_generator_service import ToolkitGeneratorService


def scoped_context(integration_type: str, provider: str, snapshot_context: list[dict]) -> dict:
    return {
        "organization_id": "org_1",
        "tenant_id": "tenant_1",
        "tenant_integration_id": f"tenant_1:{integration_type}:{provider}",
        "provider_connection_id": f"provider_conn_{provider}",
        "selected_context_snapshot_id": "snapshot_A",
        "actor_id": "admin_1",
        "actor_role": "admin",
        "integration_type": integration_type,
        "provider": provider,
        "snapshot_context": snapshot_context,
        "embedding_context": [],
    }


def test_provider_capability_registry_resolves_multiple_backends():
    assert ProviderCapabilityRegistry.require("database", "postgresql").execution_backend == "database_sql"
    assert ProviderCapabilityRegistry.require("database", "mongodb").execution_backend == "database_nosql"
    assert ProviderCapabilityRegistry.require("crm", "zoho").execution_backend == "connector_action"
    assert ProviderCapabilityRegistry.require("erp", "tally").execution_backend == "erp_xml"
    assert ProviderCapabilityRegistry.require("custom_api", "openapi").execution_backend == "api_request"


def test_connected_toolkit_builders_returns_connected_builders_only():
    provider_status = {
        "db_status": "indexed",
        "db_provider": "postgresql",
        "db_database_name": "commerce",
        "db_selected_sources": [{"table": "customers"}],
        "crm_status": "indexed",
        "crm_provider": "zoho",
        "crm_account_name": "Zoho CRM",
        "zoho_desk_status": "indexed",
        "zoho_desk_account_name": "Desk Account",
        "shipping_status": "indexed",
        "shipping_provider": "shiprocket",
        "shipping_account_name": "Shiprocket",
    }

    builders = ToolkitGeneratorService.connected_toolkit_builders(provider_status)
    keys = {builder["builder_key"] for builder in builders}

    assert "database:postgresql:database_sql" in keys
    assert "crm:zoho:connector_action" in keys
    assert "zoho_desk:zoho:connector_action" in keys
    assert "shipping:shiprocket:connector_action" in keys
    assert "erp:tally:erp_xml" not in keys


def test_mongodb_generation_uses_nosql_backend():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "database",
        "mongodb",
        "Create a tool to find customer by phone",
        scoped_context(
            "database",
            "mongodb",
            [
                {
                    "source": "provider_status.db_schema_snapshot",
                    "payload": {
                        "schema": "app",
                        "name": "customers",
                        "columns": [
                            {"name": "customer_id", "type": "objectId", "nullable": False},
                            {"name": "phone", "type": "string", "nullable": True},
                            {"name": "full_name", "type": "string", "nullable": True},
                        ],
                    },
                }
            ],
        ),
    )

    assert result["tool"]["execution"]["type"] == "database_nosql"
    assert result["tool"]["builder"]["execution_backend"] == "database_nosql"


def test_crm_generation_uses_connector_action_backend():
    result = ToolkitGeneratorService._sanitize_tool(
        {},
        "crm",
        "zoho",
        "Create a tool to update customer email",
        scoped_context(
            "crm",
            "zoho",
            [
                {
                    "source": "provider_status.crm_action_snapshot",
                    "payload": {
                        "name": "updateContact",
                        "action": "updateContact",
                        "method": "PATCH",
                        "endpoint": "/crm/v2/Contacts/{contact_id}",
                        "module": "Contacts",
                        "description": "Update a CRM contact.",
                    },
                }
            ],
        ),
    )

    assert result["tool"]["execution"]["type"] == "connector_action"
    assert result["tool"]["builder"]["execution_backend"] == "connector_action"
