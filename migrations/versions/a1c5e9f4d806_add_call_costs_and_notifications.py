"""Add call_costs (per-call billing ledger) + notifications inbox.

Revision ID: a1c5e9f4d806
Revises: e3b7c2a9d105
Create Date: 2026-05-26 02:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "a1c5e9f4d806"
down_revision: Union[str, Sequence[str], None] = "e3b7c2a9d105"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── call_costs: per-call billing ledger ──────────────────────────────────
    op.create_table(
        "call_costs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenant_resources.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("call_id", sa.String(), nullable=False),
        sa.Column("kind", sa.String(), nullable=False, server_default="inbound"),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("duration_seconds", sa.Numeric(12, 4), nullable=False, server_default="0"),
        sa.Column("rupees", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("rate_per_second", sa.Numeric(10, 6), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("call_id", name="uq_call_costs_call_id"),
    )
    op.create_index("ix_call_costs_organization_id", "call_costs", ["organization_id"])
    op.create_index("ix_call_costs_tenant_id", "call_costs", ["tenant_id"])
    op.create_index("ix_call_costs_call_id", "call_costs", ["call_id"])
    op.create_index("ix_call_costs_campaign_id", "call_costs", ["campaign_id"])
    op.create_index(
        "ix_call_costs_org_started_at",
        "call_costs",
        ["organization_id", "started_at"],
    )

    # ── notifications: per-org inbox ─────────────────────────────────────────
    op.create_table(
        "notifications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenant_resources.tenant_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("type", sa.String(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False, server_default="p2"),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("body", sa.Text(), nullable=True),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("dedup_key", sa.String(), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_notifications_organization_id", "notifications", ["organization_id"])
    op.create_index("ix_notifications_tenant_id", "notifications", ["tenant_id"])
    op.create_index(
        "ix_notifications_org_created_at",
        "notifications",
        ["organization_id", "created_at"],
    )
    op.create_index(
        "ix_notifications_dedup",
        "notifications",
        ["organization_id", "type", "dedup_key", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_dedup", table_name="notifications")
    op.drop_index("ix_notifications_org_created_at", table_name="notifications")
    op.drop_index("ix_notifications_tenant_id", table_name="notifications")
    op.drop_index("ix_notifications_organization_id", table_name="notifications")
    op.drop_table("notifications")

    op.drop_index("ix_call_costs_org_started_at", table_name="call_costs")
    op.drop_index("ix_call_costs_campaign_id", table_name="call_costs")
    op.drop_index("ix_call_costs_call_id", table_name="call_costs")
    op.drop_index("ix_call_costs_tenant_id", table_name="call_costs")
    op.drop_index("ix_call_costs_organization_id", table_name="call_costs")
    op.drop_table("call_costs")
