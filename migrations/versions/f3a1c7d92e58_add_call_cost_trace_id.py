"""Add trace_id to call_costs.

The voice pipeline now stamps a W3C OpenTelemetry trace id on each call
(app/services/otel_tracer.py) and the recorder persists it here so support can
pivot "this call → its logs / LangSmith run" from the cost dashboard. Nullable
+ indexed: rows from before this column (and calls placed with OTel disabled)
keep NULL, so no backfill is required.

Revision ID: f3a1c7d92e58
Revises: e2c9a4f17b85
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "f3a1c7d92e58"
down_revision: Union[str, Sequence[str], None] = "e2c9a4f17b85"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def _has_index(table_name: str, index_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return index_name in {ix["name"] for ix in inspector.get_indexes(table_name)}


def upgrade() -> None:
    if not _has_column("call_costs", "trace_id"):
        op.add_column(
            "call_costs",
            sa.Column("trace_id", sa.String(), nullable=True),
        )
    if not _has_index("call_costs", "ix_call_costs_trace_id"):
        op.create_index("ix_call_costs_trace_id", "call_costs", ["trace_id"])


def downgrade() -> None:
    if _has_index("call_costs", "ix_call_costs_trace_id"):
        op.drop_index("ix_call_costs_trace_id", table_name="call_costs")
    if _has_column("call_costs", "trace_id"):
        op.drop_column("call_costs", "trace_id")
