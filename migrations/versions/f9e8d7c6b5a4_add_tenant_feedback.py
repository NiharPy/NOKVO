"""Add tenant_feedback table.

Stores tenant-submitted feedback / feature requests (the in-product
"Feedback / Suggest a feature" button), surfaced read-only in the SuperAdmin
console's Feedback tab.

Revision ID: f9e8d7c6b5a4
Revises: b9f4c2a7e1d8
Create Date: 2026-06-20 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision: str = "f9e8d7c6b5a4"
down_revision: Union[str, Sequence[str], None] = "b9f4c2a7e1d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("tenant_feedback"):
        return
    op.create_table(
        "tenant_feedback",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "submitted_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organization_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("category", sa.String(), nullable=False, server_default="feedback"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="new"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_tenant_feedback_organization_id", "tenant_feedback", ["organization_id"])
    op.create_index("ix_tenant_feedback_created_at", "tenant_feedback", ["created_at"])


def downgrade() -> None:
    if _has_table("tenant_feedback"):
        op.drop_table("tenant_feedback")
