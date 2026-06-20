"""Add billed_minutes to call_costs.

Billing moved from per-second to per-whole-minute (every started minute is
charged in full, ceil). ``billed_minutes`` is the new unit the tiered tariff
and the dashboard invoice count; ``duration_seconds`` is retained as the actual
call length. NOT NULL with a server default of 0, and existing rows are
backfilled to ``ceil(duration_seconds / 60)`` so historical month-to-date
minute counts (which drive the tier the next call lands in) stay correct.

Revision ID: b9f4c2a7e1d8
Revises: d5a8f1c3e9b2
Create Date: 2026-06-20 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "b9f4c2a7e1d8"
down_revision: Union[str, Sequence[str], None] = "d5a8f1c3e9b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("call_costs", "billed_minutes"):
        op.add_column(
            "call_costs",
            sa.Column("billed_minutes", sa.Integer(), nullable=False, server_default="0"),
        )
        # Backfill: ceil(duration_seconds / 60). CEIL on the numeric division is
        # exact for our 4-dp seconds; a 0-second row stays 0.
        op.execute(
            "UPDATE call_costs "
            "SET billed_minutes = CEIL(duration_seconds / 60.0) "
            "WHERE duration_seconds > 0"
        )


def downgrade() -> None:
    if _has_column("call_costs", "billed_minutes"):
        op.drop_column("call_costs", "billed_minutes")
