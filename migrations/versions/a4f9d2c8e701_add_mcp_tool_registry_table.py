"""Add MCP tool registry table

Revision ID: a4f9d2c8e701
Revises: f7b2d3c4e5f6
Create Date: 2026-05-01 12:30:00.000000
"""

from datetime import datetime
from typing import Sequence, Union
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "a4f9d2c8e701"
down_revision: Union[str, Sequence[str], None] = "f7b2d3c4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def _parse_datetime(value):
    if not value:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _parse_uuid(value):
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


registry_table = sa.table(
    "mcp_tool_registry_entries",
    sa.column("id", postgresql.UUID(as_uuid=True)),
    sa.column("organization_id", postgresql.UUID(as_uuid=True)),
    sa.column("tenant_id", sa.String()),
    sa.column("integration_type", sa.String()),
    sa.column("provider", sa.String()),
    sa.column("tool_name", sa.String()),
    sa.column("title", sa.String()),
    sa.column("description", sa.String()),
    sa.column("tool_definition", postgresql.JSONB()),
    sa.column("status", sa.String()),
    sa.column("version", sa.Integer()),
    sa.column("draft_id", sa.String()),
    sa.column("approved_by", postgresql.UUID(as_uuid=True)),
    sa.column("approved_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)


def upgrade() -> None:
    if not _has_table("mcp_tool_registry_entries"):
        op.create_table(
            "mcp_tool_registry_entries",
            sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("organization_id", postgresql.UUID(as_uuid=True), nullable=False),
            sa.Column("tenant_id", sa.String(), nullable=False),
            sa.Column("integration_type", sa.String(), nullable=False),
            sa.Column("provider", sa.String(), nullable=False),
            sa.Column("tool_name", sa.String(), nullable=False),
            sa.Column("title", sa.String(), nullable=True),
            sa.Column("description", sa.String(), nullable=True),
            sa.Column("tool_definition", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="active"),
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("draft_id", sa.String(), nullable=True),
            sa.Column("approved_by", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=True),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint(
                "organization_id",
                "integration_type",
                "provider",
                "tool_name",
                name="uq_mcp_tool_registry_org_integration_tool",
            ),
        )
        op.create_index("ix_mcp_tool_registry_org", "mcp_tool_registry_entries", ["organization_id"])
        op.create_index(
            "ix_mcp_tool_registry_lookup",
            "mcp_tool_registry_entries",
            ["organization_id", "integration_type", "provider", "status"],
        )

    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT organization_id, tenant_id, provider_status FROM tenant_resources")
    ).mappings()
    for row in rows:
        provider_status = row["provider_status"] or {}
        legacy_registry = provider_status.get("mcp_tool_registry") or {}
        if not isinstance(legacy_registry, dict):
            continue
        for key, tools in legacy_registry.items():
            if not isinstance(key, str) or ":" not in key or not isinstance(tools, list):
                continue
            integration_type, provider = key.split(":", 1)
            for tool in tools:
                if not isinstance(tool, dict):
                    continue
                tool_name = ((tool.get("mcp") or {}).get("tool_name") or tool.get("name") or "").strip()
                if not tool_name:
                    continue
                insert_stmt = postgresql.insert(registry_table).values(
                    id=uuid.uuid4(),
                    organization_id=row["organization_id"],
                    tenant_id=row["tenant_id"],
                    integration_type=integration_type,
                    provider=provider,
                    tool_name=tool_name,
                    title=tool.get("title") or tool.get("name"),
                    description=tool.get("description"),
                    tool_definition=tool,
                    status="active",
                    version=1,
                    draft_id=tool.get("registry_id"),
                    approved_by=_parse_uuid(tool.get("approved_by")),
                    approved_at=_parse_datetime(tool.get("approved_at")),
                )
                bind.execute(
                    insert_stmt.on_conflict_do_update(
                        index_elements=["organization_id", "integration_type", "provider", "tool_name"],
                        set_={
                            "title": insert_stmt.excluded.title,
                            "description": insert_stmt.excluded.description,
                            "tool_definition": insert_stmt.excluded.tool_definition,
                            "status": "active",
                            "draft_id": insert_stmt.excluded.draft_id,
                            "approved_by": insert_stmt.excluded.approved_by,
                            "approved_at": insert_stmt.excluded.approved_at,
                            "updated_at": sa.text("now()"),
                        },
                    )
                )

    op.execute("UPDATE tenant_resources SET provider_status = provider_status - 'mcp_tool_registry' WHERE provider_status ? 'mcp_tool_registry'")


def downgrade() -> None:
    if not _has_table("mcp_tool_registry_entries"):
        return

    bind = op.get_bind()
    grouped: dict[tuple, dict[str, list[dict]]] = {}
    rows = bind.execute(
        sa.text(
            """
            SELECT organization_id, tenant_id, integration_type, provider, tool_definition
            FROM mcp_tool_registry_entries
            WHERE status = 'active'
            ORDER BY integration_type, provider, tool_name
            """
        )
    ).mappings()
    for row in rows:
        group_key = (row["organization_id"], row["tenant_id"])
        registry_key = f"{row['integration_type']}:{row['provider']}"
        grouped.setdefault(group_key, {}).setdefault(registry_key, []).append(row["tool_definition"])

    for (organization_id, tenant_id), registry in grouped.items():
        tenant_row = bind.execute(
            sa.text(
                """
                SELECT provider_status
                FROM tenant_resources
                WHERE organization_id = :organization_id AND tenant_id = :tenant_id
                """
            ),
            {"organization_id": organization_id, "tenant_id": tenant_id},
        ).mappings().first()
        provider_status = dict((tenant_row or {}).get("provider_status") or {})
        provider_status["mcp_tool_registry"] = registry
        bind.execute(
            sa.text(
                """
                UPDATE tenant_resources
                SET provider_status = :provider_status
                WHERE organization_id = :organization_id AND tenant_id = :tenant_id
                """
            ).bindparams(sa.bindparam("provider_status", type_=postgresql.JSONB())),
            {
                "provider_status": provider_status,
                "organization_id": organization_id,
                "tenant_id": tenant_id,
            },
        )

    op.drop_index("ix_mcp_tool_registry_lookup", table_name="mcp_tool_registry_entries")
    op.drop_index("ix_mcp_tool_registry_org", table_name="mcp_tool_registry_entries")
    op.drop_table("mcp_tool_registry_entries")
