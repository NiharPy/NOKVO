"""Add llm_pool_keys table.

DB-managed LLM pool members (added/changed from the SuperAdmin console). Each
enabled row is merged into the live gpt-5-mini / nano pool. ``api_key_enc`` is
Fernet-encrypted.

Revision ID: c7b6a5d4e3f2
Revises: d4c3b2a1e9f7
Create Date: 2026-06-21 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision: str = "c7b6a5d4e3f2"
down_revision: Union[str, Sequence[str], None] = "d4c3b2a1e9f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("llm_pool_keys"):
        return
    op.create_table(
        "llm_pool_keys",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("pool", sa.String(), nullable=False, server_default="mini"),
        sa.Column("label", sa.String(), nullable=True),
        sa.Column("endpoint", sa.String(), nullable=False),
        sa.Column("api_key_enc", sa.String(), nullable=False),
        sa.Column("deployment", sa.String(), nullable=True),
        sa.Column("tpm", sa.Integer(), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )


def downgrade() -> None:
    if _has_table("llm_pool_keys"):
        op.drop_table("llm_pool_keys")
