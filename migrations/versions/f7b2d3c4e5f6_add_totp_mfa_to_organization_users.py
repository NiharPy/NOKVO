"""Add TOTP MFA to organization users

Revision ID: f7b2d3c4e5f6
Revises: e8f7c1a4d2b0
Create Date: 2026-04-30 21:10:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = "f7b2d3c4e5f6"
down_revision: Union[str, Sequence[str], None] = "e8f7c1a4d2b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {col["name"] for col in inspector.get_columns(table_name)}
    return column_name in cols


def upgrade() -> None:
    if not _has_table("organization_users"):
        return

    if not _has_column("organization_users", "mfa_required"):
        op.add_column(
            "organization_users",
            sa.Column("mfa_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        )
    if not _has_column("organization_users", "totp_secret_encrypted"):
        op.add_column(
            "organization_users",
            sa.Column("totp_secret_encrypted", sa.String(), nullable=True),
        )


def downgrade() -> None:
    if not _has_table("organization_users"):
        return
    if _has_column("organization_users", "totp_secret_encrypted"):
        op.drop_column("organization_users", "totp_secret_encrypted")
    if _has_column("organization_users", "mfa_required"):
        op.drop_column("organization_users", "mfa_required")
