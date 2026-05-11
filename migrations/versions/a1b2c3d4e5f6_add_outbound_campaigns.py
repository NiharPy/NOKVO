"""Add outbound_campaigns table

Revision ID: a1b2c3d4e5f6
Revises: f13d1f7c9a21
Create Date: 2026-05-10 14:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "a1b2c3d4e5f6"
down_revision: Union[str, Sequence[str], None] = ("f13d1f7c9a21", "a4f9d2c8e701")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "outbound_campaigns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant_resources.tenant_id"), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("draft", "running", "completed", "failed", "cancelled", name="campaignstatus"),
            nullable=False,
            server_default="draft",
        ),
        sa.Column("contacts", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("doc_blob_path", sa.String(), nullable=True),
        sa.Column("doc_text", sa.Text(), nullable=True),
        sa.Column("from_number", sa.String(), nullable=True),
        sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("answered_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_outbound_campaigns_tenant_id", "outbound_campaigns", ["tenant_id"])


def downgrade() -> None:
    op.drop_index("ix_outbound_campaigns_tenant_id", "outbound_campaigns")
    op.drop_table("outbound_campaigns")
    op.execute("DROP TYPE IF EXISTS campaignstatus")
