"""Add call_transcripts table — durable per-call transcripts.

One row per voice call (keyed by call_id), written at teardown and purged on a
rolling 1-month retention. Powers the Transcripts page + per-call .txt download.
Independent of call_costs (the billing ledger is never purged).

Revision ID: c1d2e3f4a5b6
Revises: b9c2d5e8f1a3
Create Date: 2026-06-15 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, Sequence[str], None] = "b9c2d5e8f1a3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    return table_name in inspect(bind).get_table_names()


def upgrade() -> None:
    if not _has_table("call_transcripts"):
        op.create_table(
            "call_transcripts",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "tenant_id",
                sa.String(),
                sa.ForeignKey("tenant_resources.tenant_id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("call_id", sa.String(), nullable=False),
            sa.Column("kind", sa.String(), nullable=False, server_default="inbound"),
            sa.Column("caller_phone", sa.String(), nullable=True),
            sa.Column("turns", JSONB(), nullable=False, server_default="[]"),
            sa.Column("turn_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duration_seconds", sa.Numeric(12, 4), nullable=False, server_default="0"),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.UniqueConstraint("call_id", name="uq_call_transcripts_call_id"),
        )
        op.create_index(
            "ix_call_transcripts_org_started_at",
            "call_transcripts",
            ["organization_id", "started_at"],
        )


def downgrade() -> None:
    if _has_table("call_transcripts"):
        op.drop_index("ix_call_transcripts_org_started_at", table_name="call_transcripts")
        op.drop_table("call_transcripts")
