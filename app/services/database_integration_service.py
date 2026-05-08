from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import sqlite3
import ssl as py_ssl
from typing import Any
import uuid
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import asyncpg
import redis
from sqlalchemy import text as sql_text

from app.models.tenant_resources import TenantResources
from app.services.mcp_toolkit.sql_validator import SQLValidator
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.qdrant_service import QdrantService
from app.services.text_embedding_service import TextEmbeddingService


SUPPORTED_PROVIDER_OPTIONS = [
    {"value": "postgresql", "label": "PostgreSQL", "group": "sql", "supported": True},
    {"value": "mysql", "label": "MySQL", "group": "sql", "supported": False},
    {"value": "mariadb", "label": "MariaDB", "group": "sql", "supported": False},
    {"value": "sqlserver", "label": "SQL Server", "group": "sql", "supported": False},
    {"value": "oracle", "label": "Oracle", "group": "sql", "supported": False},
    {"value": "mongodb", "label": "MongoDB", "group": "nosql", "supported": False},
    {"value": "redis", "label": "Redis", "group": "cache", "supported": True},
    {"value": "sqlite", "label": "SQLite", "group": "sql", "supported": True},
    {"value": "cockroachdb", "label": "CockroachDB", "group": "sql", "supported": True},
    {"value": "redshift", "label": "Amazon Redshift", "group": "warehouse", "supported": True},
    {"value": "snowflake", "label": "Snowflake", "group": "warehouse", "supported": False},
    {"value": "bigquery", "label": "BigQuery", "group": "warehouse", "supported": False},
]

POSTGRES_FAMILY = {"postgresql", "cockroachdb", "redshift"}
SQLITE_FAMILY = {"sqlite"}
REDIS_FAMILY = {"redis"}


@dataclass
class DatabaseScanResult:
    provider: str
    schema: list[dict[str, Any]]
    database_name: str | None = None


class DatabaseIntegrationService:
    @staticmethod
    def provider_options() -> list[dict[str, Any]]:
        return SUPPORTED_PROVIDER_OPTIONS

    @staticmethod
    def _build_postgres_connect_kwargs(connection_string: str) -> dict[str, Any]:
        if connection_string.startswith("postgres://"):
            connection_string = "postgresql://" + connection_string[len("postgres://") :]
        elif connection_string.startswith("postgresql+asyncpg://"):
            connection_string = "postgresql://" + connection_string[len("postgresql+asyncpg://") :]

        parsed = urlparse(connection_string)
        query_items = parse_qsl(parsed.query, keep_blank_values=True)
        filtered_query: list[tuple[str, str]] = []
        ssl_mode: str | None = None

        for key, value in query_items:
            if key.lower() in {"ssl", "sslmode"}:
                ssl_mode = value.strip().lower()
                continue
            filtered_query.append((key, value))

        normalized_dsn = urlunparse(parsed._replace(query=urlencode(filtered_query)))
        connect_kwargs: dict[str, Any] = {"dsn": normalized_dsn}
        ssl_value = DatabaseIntegrationService._postgres_ssl_value(ssl_mode)
        if ssl_value is not None:
            connect_kwargs["ssl"] = ssl_value
        return connect_kwargs

    @staticmethod
    def _postgres_ssl_value(ssl_mode: str | None) -> Any | None:
        if not ssl_mode:
            return None
        if ssl_mode in {"disable", "false", "0", "no"}:
            return False
        if ssl_mode in {"require", "prefer", "allow", "true", "1", "yes"}:
            return True
        if ssl_mode in {"verify-ca", "verify-full"}:
            return py_ssl.create_default_context()
        raise ValueError(f"Unsupported PostgreSQL SSL mode: {ssl_mode}")

    @staticmethod
    def _parse_provider(provider: str) -> str:
        normalized = (provider or "").strip().lower()
        if not normalized:
            raise ValueError("Database provider is required")
        known = {item["value"] for item in SUPPORTED_PROVIDER_OPTIONS}
        if normalized not in known:
            raise ValueError("Unsupported database provider")
        return normalized

    @staticmethod
    async def scan_schema(provider: str, connection_string: str) -> DatabaseScanResult:
        provider = DatabaseIntegrationService._parse_provider(provider)
        if provider in POSTGRES_FAMILY:
            return await DatabaseIntegrationService._scan_postgres_family(provider, connection_string)
        if provider in SQLITE_FAMILY:
            return await DatabaseIntegrationService._scan_sqlite(provider, connection_string)
        if provider in REDIS_FAMILY:
            return await DatabaseIntegrationService._scan_redis(provider, connection_string)
        raise RuntimeError(
            f"{provider} is listed in the UI, but live schema scanning is not available in this backend environment yet."
        )

    @staticmethod
    async def store_connection_secret(
        tenant_res: TenantResources,
        provider: str,
        connection_string: str,
    ) -> str:
        secret_refs = dict(tenant_res.secret_refs or {})
        db_secret_ref = (secret_refs.get("db_connection_string") or {}).get("secret_name")
        if not db_secret_ref:
            db_secret_ref = AzureKeyVaultService._secret_name(tenant_res.tenant_id, "db-connection-string")
            secret_refs["db_connection_string"] = {
                "secret_name": db_secret_ref,
                **AzureKeyVaultService._rotation_metadata(),
            }
            tenant_res.secret_refs = secret_refs

        await AzureKeyVaultService.set_secret_value(
            db_secret_ref,
            json.dumps({"provider": provider, "connection_string": connection_string}),
            tenant_res.tenant_id,
            "db_connection_string",
        )
        return db_secret_ref

    @staticmethod
    async def load_connection_secret(tenant_res: TenantResources) -> tuple[str, str]:
        secret_refs = tenant_res.secret_refs or {}
        db_secret_name = (secret_refs.get("db_connection_string") or {}).get("secret_name")
        if not db_secret_name:
            raise RuntimeError("Database connection secret is not configured for this organization")
        raw_secret = await AzureKeyVaultService.get_secret_value(db_secret_name)
        if not raw_secret:
            raise RuntimeError("Database connection secret could not be loaded from Key Vault")
        try:
            payload = json.loads(raw_secret)
        except json.JSONDecodeError:
            return "postgresql", raw_secret
        provider = DatabaseIntegrationService._parse_provider(payload.get("provider") or "postgresql")
        connection_string = payload.get("connection_string")
        if not connection_string:
            raise RuntimeError("Database connection secret is incomplete")
        return provider, connection_string

    @staticmethod
    async def explain_sql(provider: str, connection_string: str, sql: str, mapping: dict[str, Any]) -> dict[str, Any]:
        provider = DatabaseIntegrationService._parse_provider(provider)
        if provider not in POSTGRES_FAMILY:
            return {"status": "skipped", "message": f"EXPLAIN validation is not implemented for {provider}."}
        explain_sql, args, sample_bindings = SQLValidator.postgres_explain_query(sql, mapping)
        connect_kwargs = DatabaseIntegrationService._build_postgres_connect_kwargs(connection_string)
        conn = await asyncpg.connect(**connect_kwargs)
        try:
            rows = await conn.fetch(explain_sql, *args)
        finally:
            await conn.close()
        return {
            "status": "passed",
            "statement": "EXPLAIN <generated parameterized SQL>",
            "sample_bindings": sample_bindings,
            "plan_rows": len(rows),
        }

    @staticmethod
    async def index_selected_columns(
        tenant_res: TenantResources,
        provider: str,
        connection_string: str,
        selections: list[dict[str, Any]],
        row_limit: int = 50,
    ) -> dict[str, Any]:
        provider = DatabaseIntegrationService._parse_provider(provider)
        if provider in POSTGRES_FAMILY:
            records = await DatabaseIntegrationService._fetch_postgres_rows(connection_string, selections, row_limit)
        elif provider in SQLITE_FAMILY:
            records = await DatabaseIntegrationService._fetch_sqlite_rows(connection_string, selections, row_limit)
        elif provider in REDIS_FAMILY:
            records = await DatabaseIntegrationService._fetch_redis_records(connection_string, selections, row_limit)
        else:
            raise RuntimeError(f"{provider} indexing is not available in this backend environment yet.")

        points = []
        for index, record in enumerate(records):
            point_id = str(
                uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"{tenant_res.tenant_id}:{provider}:{record.get('table')}:{index}:{record['text']}",
                )
            )
            vector = TextEmbeddingService.embed_text(record["text"])
            payload = {
                "source_type": "database_schema_selection",
                "integration_type": "database",
                "provider": provider,
                "table": record.get("table"),
                "columns": record.get("columns", []),
                "text": record["text"],
                "row_preview": record.get("row_preview", {}),
            }
            points.append({"id": point_id, "vector": vector, "payload": payload})

        await QdrantService.delete_points_by_filter(
            tenant_res,
            {"integration_type": "database"},
        )
        await QdrantService.delete_points_by_filter(
            tenant_res,
            {"source_type": "database_schema_selection"},
        )
        if points:
            await QdrantService.upsert_points(tenant_res, points)

        tables = sorted({record.get("table") for record in records if record.get("table")})
        columns = sum(len(record.get("columns", [])) for record in records)
        return {
            "indexed_points": len(points),
            "tables": tables,
            "column_value_count": columns,
            "row_limit": row_limit,
        }

    @staticmethod
    async def _scan_postgres_family(provider: str, connection_string: str) -> DatabaseScanResult:
        connect_kwargs = DatabaseIntegrationService._build_postgres_connect_kwargs(connection_string)
        conn = await asyncpg.connect(**connect_kwargs)
        try:
            rows = await conn.fetch(
                """
                SELECT table_schema, table_name, column_name, data_type, is_nullable
                FROM information_schema.columns
                WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
                ORDER BY table_schema, table_name, ordinal_position
                """
            )
            db_name = await conn.fetchval("SELECT current_database()")
        finally:
            await conn.close()

        tables: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (row["table_schema"], row["table_name"])
            table = tables.setdefault(
                key,
                {
                    "schema": row["table_schema"],
                    "name": row["table_name"],
                    "columns": [],
                },
            )
            table["columns"].append(
                {
                    "name": row["column_name"],
                    "type": row["data_type"],
                    "nullable": row["is_nullable"] == "YES",
                }
            )

        return DatabaseScanResult(provider=provider, schema=list(tables.values()), database_name=db_name)

    @staticmethod
    async def _scan_sqlite(provider: str, connection_string: str) -> DatabaseScanResult:
        def _work() -> DatabaseScanResult:
            parsed = urlparse(connection_string)
            path = parsed.path if parsed.scheme == "sqlite" else connection_string.replace("sqlite:///", "", 1)
            conn = sqlite3.connect(path)
            try:
                tables = []
                table_names = [
                    row[0]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
                    ).fetchall()
                ]
                for table_name in table_names:
                    columns = []
                    for col in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall():
                        columns.append(
                            {
                                "name": col[1],
                                "type": col[2] or "text",
                                "nullable": col[3] == 0,
                            }
                        )
                    tables.append({"schema": "main", "name": table_name, "columns": columns})
                return DatabaseScanResult(provider=provider, schema=tables, database_name=path)
            finally:
                conn.close()

        return await asyncio.to_thread(_work)

    @staticmethod
    async def _scan_redis(provider: str, connection_string: str) -> DatabaseScanResult:
        def _work() -> DatabaseScanResult:
            client = redis.Redis.from_url(connection_string, decode_responses=True)
            try:
                schema = []
                cursor = 0
                seen = 0
                while True:
                    cursor, keys = client.scan(cursor=cursor, count=25)
                    for key in keys:
                        key_type = client.type(key)
                        schema.append(
                            {
                                "schema": "default",
                                "name": key,
                                "columns": [
                                    {"name": "key", "type": "string", "nullable": False},
                                    {"name": "type", "type": key_type, "nullable": False},
                                    {"name": "value_preview", "type": "string", "nullable": True},
                                ],
                            }
                        )
                        seen += 1
                        if seen >= 50:
                            return DatabaseScanResult(provider=provider, schema=schema, database_name="redis")
                    if cursor == 0:
                        break
                return DatabaseScanResult(provider=provider, schema=schema, database_name="redis")
            finally:
                client.close()

        return await asyncio.to_thread(_work)

    @staticmethod
    def _quote_identifier(identifier: str) -> str:
        return '"' + identifier.replace('"', '""') + '"'

    @staticmethod
    async def _fetch_postgres_rows(
        connection_string: str,
        selections: list[dict[str, Any]],
        row_limit: int,
    ) -> list[dict[str, Any]]:
        connect_kwargs = DatabaseIntegrationService._build_postgres_connect_kwargs(connection_string)
        conn = await asyncpg.connect(**connect_kwargs)
        try:
            records = []
            for selection in selections:
                table_name = selection["table"]
                schema_name = selection.get("schema") or selection.get("schema_name") or "public"
                columns = [col for col in selection.get("columns", []) if col]
                if not columns:
                    continue
                quoted_columns = ", ".join(DatabaseIntegrationService._quote_identifier(col) for col in columns)
                query = (
                    f"SELECT {quoted_columns} "
                    f"FROM {DatabaseIntegrationService._quote_identifier(schema_name)}.{DatabaseIntegrationService._quote_identifier(table_name)} "
                    f"LIMIT {max(1, min(row_limit, 200))}"
                )
                rows = await conn.fetch(query)
                for row in rows:
                    row_preview = {column: row[column] for column in columns}
                    text = DatabaseIntegrationService._row_to_text(table_name, columns, row_preview)
                    records.append(
                        {
                            "table": f"{schema_name}.{table_name}",
                            "columns": columns,
                            "row_preview": row_preview,
                            "text": text,
                        }
                    )
            return records
        finally:
            await conn.close()

    @staticmethod
    async def _fetch_sqlite_rows(
        connection_string: str,
        selections: list[dict[str, Any]],
        row_limit: int,
    ) -> list[dict[str, Any]]:
        def _work() -> list[dict[str, Any]]:
            parsed = urlparse(connection_string)
            path = parsed.path if parsed.scheme == "sqlite" else connection_string.replace("sqlite:///", "", 1)
            conn = sqlite3.connect(path)
            conn.row_factory = sqlite3.Row
            try:
                records: list[dict[str, Any]] = []
                for selection in selections:
                    table_name = selection["table"]
                    columns = [col for col in selection.get("columns", []) if col]
                    if not columns:
                        continue
                    quoted_columns = ", ".join(f'"{col}"' for col in columns)
                    rows = conn.execute(
                        f'SELECT {quoted_columns} FROM "{table_name}" LIMIT ?',
                        (max(1, min(row_limit, 200)),),
                    ).fetchall()
                    for row in rows:
                        row_preview = {column: row[column] for column in columns}
                        records.append(
                            {
                                "table": table_name,
                                "columns": columns,
                                "row_preview": row_preview,
                                "text": DatabaseIntegrationService._row_to_text(table_name, columns, row_preview),
                            }
                        )
                return records
            finally:
                conn.close()

        return await asyncio.to_thread(_work)

    @staticmethod
    async def _fetch_redis_records(
        connection_string: str,
        selections: list[dict[str, Any]],
        row_limit: int,
    ) -> list[dict[str, Any]]:
        def _work() -> list[dict[str, Any]]:
            client = redis.Redis.from_url(connection_string, decode_responses=True)
            try:
                selected_keys = [item["table"] for item in selections if item.get("table")]
                records: list[dict[str, Any]] = []
                for key in selected_keys[: max(1, min(row_limit, 200))]:
                    key_type = client.type(key)
                    value_preview = DatabaseIntegrationService._redis_preview(client, key, key_type)
                    row_preview = {"key": key, "type": key_type, "value_preview": value_preview}
                    records.append(
                        {
                            "table": key,
                            "columns": ["key", "type", "value_preview"],
                            "row_preview": row_preview,
                            "text": DatabaseIntegrationService._row_to_text(key, ["key", "type", "value_preview"], row_preview),
                        }
                    )
                return records
            finally:
                client.close()

        return await asyncio.to_thread(_work)

    @staticmethod
    def _redis_preview(client: redis.Redis, key: str, key_type: str) -> str:
        if key_type == "string":
            return str(client.get(key) or "")[:500]
        if key_type == "hash":
            return json.dumps(client.hgetall(key))[:500]
        if key_type == "list":
            return json.dumps(client.lrange(key, 0, 20))[:500]
        if key_type == "set":
            return json.dumps(sorted(list(client.smembers(key)))[:20])[:500]
        if key_type == "zset":
            return json.dumps(client.zrange(key, 0, 20, withscores=True))[:500]
        return ""

    @staticmethod
    def _row_to_text(table_name: str, columns: list[str], row_preview: dict[str, Any]) -> str:
        lines = [f"table: {table_name}"]
        for column in columns:
            value = row_preview.get(column)
            lines.append(f"{column}: {value}")
        return "\n".join(lines)
