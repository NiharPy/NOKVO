"""Add organization_blocked_slots for org-wide closures (holidays etc.).

Revision ID: b2d3e4f5c6d7
Revises: a1c2d3e4f5b6
Create Date: 2026-05-20 18:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision: str = "b2d3e4f5c6d7"
down_revision: Union[str, Sequence[str], None] = "a1c2d3e4f5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    return table_name in inspect(bind).get_table_names()


def upgrade() -> None:
    if not _has_table("organization_blocked_slots"):
        op.create_table(
            "organization_blocked_slots",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("repeat_rule", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )


def downgrade() -> None:
    if _has_table("organization_blocked_slots"):
        op.drop_table("organization_blocked_slots")
