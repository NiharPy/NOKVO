"""Add enterprise organization profile fields

Revision ID: b9f6e4a2c101
Revises: 8e2c7b9d1f34
Create Date: 2026-04-29 18:30:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = "b9f6e4a2c101"
down_revision: Union[str, Sequence[str], None] = "8e2c7b9d1f34"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {col["name"] for col in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    if not _has_column("organizations", "admin_email"):
        op.add_column("organizations", sa.Column("admin_email", sa.String(), nullable=True))
    if not _has_column("organizations", "admin_name"):
        op.add_column("organizations", sa.Column("admin_name", sa.String(), nullable=True))
    if not _has_column("organizations", "call_type"):
        op.add_column("organizations", sa.Column("call_type", sa.String(), nullable=True))
    if not _has_column("organizations", "language"):
        op.add_column("organizations", sa.Column("language", sa.String(), nullable=True))
    if not _has_column("organizations", "plan_type"):
        op.add_column("organizations", sa.Column("plan_type", sa.String(), nullable=True))
    if not _has_column("organizations", "stores_pii"):
        op.add_column("organizations", sa.Column("stores_pii", sa.Boolean(), nullable=True, server_default=sa.true()))
    if not _has_column("organizations", "record_calls"):
        op.add_column("organizations", sa.Column("record_calls", sa.Boolean(), nullable=True, server_default=sa.true()))
    if not _has_column("organizations", "create_resource_group"):
        op.add_column("organizations", sa.Column("create_resource_group", sa.Boolean(), nullable=True, server_default=sa.true()))
    if not _has_column("organizations", "plivo_auto_provision"):
        op.add_column("organizations", sa.Column("plivo_auto_provision", sa.Boolean(), nullable=True, server_default=sa.false()))


def downgrade() -> None:
    if _has_column("organizations", "plivo_auto_provision"):
        op.drop_column("organizations", "plivo_auto_provision")
    if _has_column("organizations", "create_resource_group"):
        op.drop_column("organizations", "create_resource_group")
    if _has_column("organizations", "record_calls"):
        op.drop_column("organizations", "record_calls")
    if _has_column("organizations", "stores_pii"):
        op.drop_column("organizations", "stores_pii")
    if _has_column("organizations", "plan_type"):
        op.drop_column("organizations", "plan_type")
    if _has_column("organizations", "language"):
        op.drop_column("organizations", "language")
    if _has_column("organizations", "call_type"):
        op.drop_column("organizations", "call_type")
    if _has_column("organizations", "admin_name"):
        op.drop_column("organizations", "admin_name")
    if _has_column("organizations", "admin_email"):
        op.drop_column("organizations", "admin_email")
