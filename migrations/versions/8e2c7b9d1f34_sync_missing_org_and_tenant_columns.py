"""Sync missing organization and tenant_resources columns

Revision ID: 8e2c7b9d1f34
Revises: c2a7eb6e4bfd
Create Date: 2026-04-29 12:05:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "8e2c7b9d1f34"
down_revision: Union[str, Sequence[str], None] = "c2a7eb6e4bfd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {col["name"] for col in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    # organizations table drift fixes
    if not _has_column("organizations", "industry"):
        op.add_column("organizations", sa.Column("industry", sa.String(), nullable=True))
    if not _has_column("organizations", "country_code"):
        op.add_column("organizations", sa.Column("country_code", sa.String(), nullable=True))

    # tenant_resources table drift fixes
    if not _has_column("tenant_resources", "redis_host"):
        op.add_column("tenant_resources", sa.Column("redis_host", sa.String(), nullable=True))
    if not _has_column("tenant_resources", "redis_port"):
        op.add_column("tenant_resources", sa.Column("redis_port", sa.Integer(), nullable=True))


def downgrade() -> None:
    if _has_column("tenant_resources", "redis_port"):
        op.drop_column("tenant_resources", "redis_port")
    if _has_column("tenant_resources", "redis_host"):
        op.drop_column("tenant_resources", "redis_host")
    if _has_column("organizations", "country_code"):
        op.drop_column("organizations", "country_code")
    if _has_column("organizations", "industry"):
        op.drop_column("organizations", "industry")

