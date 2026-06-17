"""Add org-wide assignment defaults (team-wide working hours members inherit).

Revision ID: d2f4a6c8e1b0
Revises: c1d2e3f4a5b6
Create Date: 2026-06-17 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "d2f4a6c8e1b0"
down_revision: Union[str, Sequence[str], None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("organization_assignment_defaults"):
        op.create_table(
            "organization_assignment_defaults",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("working_days", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("start_time", sa.Time(), nullable=True),
            sa.Column("end_time", sa.Time(), nullable=True),
            sa.Column("timezone", sa.String(), nullable=False, server_default="Asia/Kolkata"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("organization_id", name="uq_org_assignment_defaults_org"),
        )
        op.create_index(
            "ix_org_assignment_defaults_org",
            "organization_assignment_defaults",
            ["organization_id"],
        )


def downgrade() -> None:
    if _has_table("organization_assignment_defaults"):
        op.drop_index("ix_org_assignment_defaults_org", table_name="organization_assignment_defaults")
        op.drop_table("organization_assignment_defaults")
