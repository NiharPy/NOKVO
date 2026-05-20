"""Add appointment_duration_minutes to Nokvo One assignment settings.

Revision ID: a1c2d3e4f5b6
Revises: f24a8b1d7e02
Create Date: 2026-05-20 12:30:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "a1c2d3e4f5b6"
down_revision: Union[str, Sequence[str], None] = "f24a8b1d7e02"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {column["name"] for column in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("organization_member_assignment_settings", "appointment_duration_minutes"):
        op.add_column(
            "organization_member_assignment_settings",
            sa.Column(
                "appointment_duration_minutes",
                sa.Integer(),
                nullable=False,
                server_default="30",
            ),
        )


def downgrade() -> None:
    if _has_column("organization_member_assignment_settings", "appointment_duration_minutes"):
        op.drop_column("organization_member_assignment_settings", "appointment_duration_minutes")
