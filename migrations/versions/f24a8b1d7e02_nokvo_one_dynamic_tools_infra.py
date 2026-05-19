"""Nokvo One dynamic-tools infra: shadow contact cols, GIN index, agent_tool_invocations.

Adds:
  - nokvo_one_tool_records.contact_phone, contact_email (indexed shadow cols for
    fast find_contact / dedup lookups; mirror values inside data JSONB)
  - GIN index on nokvo_one_tool_records.data (for jsonb @> filter queries)
  - agent_tool_invocations table (idempotency + audit log for every tool call)

Revision ID: f24a8b1d7e02
Revises: e0f1a2b3c4d5
Create Date: 2026-05-18 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "f24a8b1d7e02"
down_revision: Union[str, Sequence[str], None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {idx["name"] for idx in inspector.get_indexes(table_name)}


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_column("nokvo_one_tool_records", "contact_phone"):
        op.add_column(
            "nokvo_one_tool_records",
            sa.Column("contact_phone", sa.String(), nullable=True),
        )
    if not _has_column("nokvo_one_tool_records", "contact_email"):
        op.add_column(
            "nokvo_one_tool_records",
            sa.Column("contact_email", sa.String(), nullable=True),
        )

    if not _has_index("nokvo_one_tool_records", "ix_nokvo_one_tool_records_contact_phone"):
        op.create_index(
            "ix_nokvo_one_tool_records_contact_phone",
            "nokvo_one_tool_records",
            ["organization_id", "contact_phone"],
        )
    if not _has_index("nokvo_one_tool_records", "ix_nokvo_one_tool_records_contact_email"):
        op.create_index(
            "ix_nokvo_one_tool_records_contact_email",
            "nokvo_one_tool_records",
            ["organization_id", "contact_email"],
        )
    if not _has_index("nokvo_one_tool_records", "ix_nokvo_one_tool_records_data_gin"):
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_nokvo_one_tool_records_data_gin "
            "ON nokvo_one_tool_records USING gin (data jsonb_path_ops)"
        )

    if not _has_table("agent_tool_invocations"):
        op.create_table(
            "agent_tool_invocations",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "agent_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("nokvo_one_agents.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("session_id", sa.String(), nullable=True),
            sa.Column("tool_key", sa.String(), nullable=False),
            sa.Column("idempotency_key", sa.String(), nullable=False),
            sa.Column("args", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("result", postgresql.JSONB(), nullable=True),
            sa.Column(
                "result_record_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("nokvo_one_tool_records.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("status", sa.String(), nullable=False, server_default="ok"),
            sa.Column("error", sa.Text(), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )
        op.create_index(
            "ix_agent_tool_invocations_org_idem",
            "agent_tool_invocations",
            ["organization_id", "idempotency_key", "created_at"],
        )
        op.create_index(
            "ix_agent_tool_invocations_org_tool",
            "agent_tool_invocations",
            ["organization_id", "tool_key", "created_at"],
        )


def downgrade() -> None:
    if _has_table("agent_tool_invocations"):
        op.drop_index("ix_agent_tool_invocations_org_tool", table_name="agent_tool_invocations")
        op.drop_index("ix_agent_tool_invocations_org_idem", table_name="agent_tool_invocations")
        op.drop_table("agent_tool_invocations")

    if _has_index("nokvo_one_tool_records", "ix_nokvo_one_tool_records_data_gin"):
        op.execute("DROP INDEX IF EXISTS ix_nokvo_one_tool_records_data_gin")
    if _has_index("nokvo_one_tool_records", "ix_nokvo_one_tool_records_contact_email"):
        op.drop_index("ix_nokvo_one_tool_records_contact_email", table_name="nokvo_one_tool_records")
    if _has_index("nokvo_one_tool_records", "ix_nokvo_one_tool_records_contact_phone"):
        op.drop_index("ix_nokvo_one_tool_records_contact_phone", table_name="nokvo_one_tool_records")

    if _has_column("nokvo_one_tool_records", "contact_email"):
        op.drop_column("nokvo_one_tool_records", "contact_email")
    if _has_column("nokvo_one_tool_records", "contact_phone"):
        op.drop_column("nokvo_one_tool_records", "contact_phone")
