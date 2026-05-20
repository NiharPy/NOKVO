"""Add pending_tool_retries for out-of-band retry of failed tool executions.

Revision ID: c3d4e5f6a7b8
Revises: b2d3e4f5c6d7
Create Date: 2026-05-21 10:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, Sequence[str], None] = "b2d3e4f5c6d7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    bind = op.get_bind()
    return name in inspect(bind).get_table_names()


def upgrade() -> None:
    if _has_table("pending_tool_retries"):
        return
    op.create_table(
        "pending_tool_retries",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("tool_key", sa.String(), nullable=False, index=True),
        sa.Column("arguments", JSONB(), nullable=False, server_default="{}"),
        sa.Column("context", JSONB(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending", index=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    if _has_table("pending_tool_retries"):
        op.drop_table("pending_tool_retries")
