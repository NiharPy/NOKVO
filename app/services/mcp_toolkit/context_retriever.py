from __future__ import annotations

from typing import Any


class ContextRetriever:
    """Normalizes trusted integration context already retrieved by the platform."""

    @staticmethod
    def database_schema(context: dict[str, Any]) -> dict[str, Any]:
        tables: dict[str, dict[str, Any]] = {}
        for item in context.get("snapshot_context", []):
            payload = item.get("payload", {})
            if not isinstance(payload, dict):
                continue
            if item.get("source") not in {"provider_status.db_schema_snapshot", "provider_status.db_selected_sources"}:
                continue
            schema = str(payload.get("schema") or payload.get("schema_name") or "public")
            table_name = payload.get("name") or payload.get("table")
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
                    "primary_key": payload.get("primary_key") or payload.get("pk") or [],
                    "foreign_keys": payload.get("foreign_keys") or [],
                    "indexes": payload.get("indexes") or [],
                },
            )
            for column in payload.get("columns") or []:
                if isinstance(column, dict):
                    name = column.get("name")
                    column_type = column.get("type") or column.get("data_type") or "unknown"
                    nullable = column.get("nullable")
                else:
                    name = column
                    column_type = "unknown"
                    nullable = None
                if not name:
                    continue
                table["columns"][str(name)] = {
                    "name": str(name),
                    "type": str(column_type),
                    "nullable": bool(nullable) if nullable is not None else True,
                    "fqn": f"{fqn}.{name}",
                }

        table_list = []
        for table in tables.values():
            table_list.append({**table, "columns": list(table["columns"].values())})
        table_list.sort(key=lambda item: item["fqn"])
        return {
            "tables": table_list,
            "allowed_tables": [table["fqn"] for table in table_list],
            "allowed_columns": {table["fqn"]: [column["name"] for column in table["columns"]] for table in table_list},
        }

    @staticmethod
    def connector_actions(context: dict[str, Any]) -> list[dict[str, Any]]:
        actions: list[dict[str, Any]] = []
        for item in context.get("snapshot_context", []) + context.get("embedding_context", []):
            payload = item.get("payload", {})
            if not isinstance(payload, dict):
                continue
            name = payload.get("name") or payload.get("action")
            endpoint = payload.get("endpoint")
            method = payload.get("method")
            if name or endpoint or method:
                actions.append(
                    {
                        "name": name or "connector_action",
                        "method": method,
                        "endpoint": endpoint,
                        "module": payload.get("module"),
                        "description": payload.get("description") or payload.get("text"),
                        "source": item.get("source"),
                    }
                )
        return actions

    @staticmethod
    def source_refs(context: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
        retrieval = context.get("retrieval") or {}
        structured = retrieval.get("source_context") or context.get("source_context")
        if isinstance(structured, list) and structured and all(isinstance(item, dict) for item in structured):
            return structured[:limit]
        refs = []
        for item in context.get("snapshot_context", [])[:limit]:
            payload = item.get("payload", {})
            resource = None
            if isinstance(payload, dict):
                label = payload.get("name") or payload.get("table") or payload.get("action") or payload.get("module")
                if payload.get("table") or payload.get("name"):
                    schema = payload.get("schema") or payload.get("schema_name") or "public"
                    table = payload.get("table") or payload.get("name")
                    resource = f"{schema}.{table}" if table else None
            else:
                label = None
            refs.append(
                {
                    "context_id": item.get("context_id") or str(label or item.get("source") or "context"),
                    "source": item.get("source"),
                    "source_kind": item.get("source_kind") or ("db_table" if resource else "connector_template"),
                    "organization_id": context.get("organization_id"),
                    "tenant_integration_id": context.get("tenant_integration_id"),
                    "provider_connection_id": context.get("provider_connection_id"),
                    "context_snapshot_id": context.get("selected_context_snapshot_id"),
                    "integration_type": context.get("integration_type"),
                    "provider": context.get("provider"),
                    "resource": resource or label or "context",
                    "status": "active",
                    "retrieval_score": item.get("retrieval_score"),
                }
            )
        return refs
