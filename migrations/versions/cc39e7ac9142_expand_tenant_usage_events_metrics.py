"""Expand tenant usage events metrics

Revision ID: cc39e7ac9142
Revises: b2c914d0a55
Create Date: 2026-04-30 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "cc39e7ac9142"
down_revision: Union[str, Sequence[str], None] = "b2c914d0a55"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {col["name"] for col in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    if not _has_column("tenant_usage_events", "llm_api_calls"):
        op.add_column("tenant_usage_events", sa.Column("llm_api_calls", sa.Integer(), nullable=False, server_default="0"))
    if not _has_column("tenant_usage_events", "storage_gb"):
        op.add_column("tenant_usage_events", sa.Column("storage_gb", sa.Numeric(10, 3), nullable=False, server_default="0"))
    if not _has_column("tenant_usage_events", "infrastructure_units"):
        op.add_column("tenant_usage_events", sa.Column("infrastructure_units", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    if _has_column("tenant_usage_events", "infrastructure_units"):
        op.drop_column("tenant_usage_events", "infrastructure_units")
    if _has_column("tenant_usage_events", "storage_gb"):
        op.drop_column("tenant_usage_events", "storage_gb")
    if _has_column("tenant_usage_events", "llm_api_calls"):
        op.drop_column("tenant_usage_events", "llm_api_calls")
