"""Add whatsapp config to real_estate_projects.

Per-project Meta-approved WhatsApp template config: the project-location message
(auto-sent on a site-visit booking) and the brochure message (sent from
whatsapp_mode). JSONB, server_default '{}' so existing rows + inserts that omit
it are valid with no backfill.

Revision ID: a8b3c1d4e9f2
Revises: f3a1c7d92e58
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB


revision: str = "a8b3c1d4e9f2"
down_revision: Union[str, Sequence[str], None] = "f3a1c7d92e58"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("real_estate_projects", "whatsapp"):
        op.add_column(
            "real_estate_projects",
            sa.Column("whatsapp", JSONB(), nullable=False, server_default="{}"),
        )


def downgrade() -> None:
    if _has_column("real_estate_projects", "whatsapp"):
        op.drop_column("real_estate_projects", "whatsapp")
