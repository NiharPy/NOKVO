"""Add usage tracking to tenant resources

Revision ID: 9c6f2b1d7e44
Revises: f13d1f7c9a21
Create Date: 2026-04-30 17:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "9c6f2b1d7e44"
down_revision: Union[str, Sequence[str], None] = "f13d1f7c9a21"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {col["name"] for col in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    if not _has_column("tenant_resources", "usage_minutes"):
        op.add_column(
            "tenant_resources",
            sa.Column("usage_minutes", sa.Integer(), nullable=False, server_default="0"),
        )
    if not _has_column("tenant_resources", "total_cost_usd"):
        op.add_column(
            "tenant_resources",
            sa.Column("total_cost_usd", sa.Numeric(12, 2), nullable=False, server_default="0"),
        )


def downgrade() -> None:
    if _has_column("tenant_resources", "total_cost_usd"):
        op.drop_column("tenant_resources", "total_cost_usd")
    if _has_column("tenant_resources", "usage_minutes"):
        op.drop_column("tenant_resources", "usage_minutes")
