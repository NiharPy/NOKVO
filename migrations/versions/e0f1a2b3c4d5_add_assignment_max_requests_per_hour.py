"""Add hourly request cap to Nokvo One assignment settings.

Revision ID: e0f1a2b3c4d5
Revises: d9e8f7a6b5c4
Create Date: 2026-05-16 20:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, Sequence[str], None] = "d9e8f7a6b5c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if _has_column("organization_member_assignment_settings", "timezone"):
        op.execute("UPDATE organization_member_assignment_settings SET timezone = 'Asia/Kolkata'")
    if not _has_column("organization_member_assignment_settings", "max_requests_per_hour"):
        op.add_column(
            "organization_member_assignment_settings",
            sa.Column("max_requests_per_hour", sa.Integer(), nullable=False, server_default="6"),
        )


def downgrade() -> None:
    if _has_column("organization_member_assignment_settings", "max_requests_per_hour"):
        op.drop_column("organization_member_assignment_settings", "max_requests_per_hour")
