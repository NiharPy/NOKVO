"""platform_settings — operator-tunable runtime knobs (first: the USD→INR FX).

Tiny key/value table the SuperAdmin console writes and every instance's
background refresher reads into the in-process ``settings`` object, so a rate
change reprices per-call COGS without a redeploy. A missing key falls back to
the ``settings`` default.

Revision ID: platform_settings_v1
Revises: cogs_cache_v1
Create Date: 2026-07-09 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "platform_settings_v1"
down_revision: Union[str, Sequence[str], None] = "cogs_cache_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("platform_settings"):
        return
    op.create_table(
        "platform_settings",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column("value", sa.String(256), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(320), nullable=True),
    )


def downgrade() -> None:
    if _has_table("platform_settings"):
        op.drop_table("platform_settings")
