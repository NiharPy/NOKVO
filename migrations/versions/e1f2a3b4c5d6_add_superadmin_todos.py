"""Add superadmin_todos table.

Internal SuperAdmin to-do list; each item can optionally be tagged to a
``tenant_feedback`` row.

Revision ID: e1f2a3b4c5d6
Revises: f9e8d7c6b5a4
Create Date: 2026-06-20 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision: str = "e1f2a3b4c5d6"
down_revision: Union[str, Sequence[str], None] = "f9e8d7c6b5a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("superadmin_todos"):
        return
    op.create_table(
        "superadmin_todos",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column(
            "feedback_id",
            UUID(as_uuid=True),
            sa.ForeignKey("tenant_feedback.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_by_superadmin_id",
            UUID(as_uuid=True),
            sa.ForeignKey("superadmin_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_superadmin_todos_feedback_id", "superadmin_todos", ["feedback_id"])
    op.create_index("ix_superadmin_todos_status_created", "superadmin_todos", ["status", "created_at"])


def downgrade() -> None:
    if _has_table("superadmin_todos"):
        op.drop_table("superadmin_todos")
