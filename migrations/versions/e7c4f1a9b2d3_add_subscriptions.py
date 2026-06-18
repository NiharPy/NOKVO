"""Add subscriptions table (Razorpay monthly plan per organization).

Revision ID: e7c4f1a9b2d3
Revises: d2f4a6c8e1b0
Create Date: 2026-06-18 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "e7c4f1a9b2d3"
down_revision: Union[str, Sequence[str], None] = "d2f4a6c8e1b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("subscriptions"):
        op.create_table(
            "subscriptions",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("tenant_id", sa.String(), nullable=True),
            sa.Column("plan", sa.String(), nullable=False),
            sa.Column("amount_paise", sa.Integer(), nullable=False),
            sa.Column("currency", sa.String(), nullable=False, server_default="INR"),
            sa.Column("razorpay_plan_id", sa.String(), nullable=True),
            sa.Column("razorpay_subscription_id", sa.String(), nullable=False),
            sa.Column("razorpay_payment_id", sa.String(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="created"),
            sa.Column("provisioned_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw_event", postgresql.JSONB(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("razorpay_subscription_id", name="uq_subscriptions_rzp_sub_id"),
        )
        op.create_index(
            "ix_subscriptions_organization_id", "subscriptions", ["organization_id"]
        )
        op.create_index(
            "ix_subscriptions_tenant_id", "subscriptions", ["tenant_id"]
        )
        op.create_index(
            "ix_subscriptions_rzp_sub_id", "subscriptions", ["razorpay_subscription_id"]
        )
        op.create_index(
            "ix_subscriptions_org_status", "subscriptions", ["organization_id", "status"]
        )


def downgrade() -> None:
    if _has_table("subscriptions"):
        for ix in (
            "ix_subscriptions_org_status",
            "ix_subscriptions_rzp_sub_id",
            "ix_subscriptions_tenant_id",
            "ix_subscriptions_organization_id",
        ):
            op.drop_index(ix, table_name="subscriptions")
        op.drop_table("subscriptions")
