"""Add qdrant url ref to tenant resources

Revision ID: d41a9d9d2ef0
Revises: b9f6e4a2c101
Create Date: 2026-04-29 19:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "d41a9d9d2ef0"
down_revision: Union[str, Sequence[str], None] = "b9f6e4a2c101"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {col["name"] for col in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    if not _has_column("tenant_resources", "qdrant_url_ref"):
        op.add_column("tenant_resources", sa.Column("qdrant_url_ref", sa.String(), nullable=True))


def downgrade() -> None:
    if _has_column("tenant_resources", "qdrant_url_ref"):
        op.drop_column("tenant_resources", "qdrant_url_ref")
