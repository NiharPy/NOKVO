"""Prepaid voice minutes.

Billing overhaul: keep the two existing plans (Inbound Only ₹4499, Inbound+Outbound
₹6499) but make voice minutes PREPAID (flat-by-bracket). Adds the
``minute_purchases`` table — one row per bundle bought (onboarding + top-ups); the
org balance = SUM(minutes) − SUM(CallCost.billed_minutes). Both plans answer
inbound, so no inbound direction gate is needed; the existing
``organizations.calling_enabled`` still gates outbound.

Revision ID: prepaid_minutes_v1
Revises: e5a1d9c2f4b7
Create Date: 2026-06-25 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "prepaid_minutes_v1"
down_revision: Union[str, Sequence[str], None] = "e5a1d9c2f4b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_column(table: str, column: str) -> bool:
    insp = inspect(op.get_bind())
    if table not in insp.get_table_names():
        return False
    return any(c["name"] == column for c in insp.get_columns(table))


def upgrade() -> None:
    # Prepaid bundle bought with the onboarding subscription (one-time addon).
    if not _has_column("subscriptions", "minutes"):
        op.add_column("subscriptions", sa.Column("minutes", sa.Integer(), nullable=True))

    # Per-call PREPAID deduction (₹): 0.6 connection fee + rate×seconds. Depletes
    # the prepaid balance. 0 for deterministic/bulk + never-connected + legacy rows.
    if not _has_column("call_costs", "prepaid_rupees"):
        op.add_column(
            "call_costs",
            sa.Column("prepaid_rupees", sa.Numeric(14, 4), nullable=False, server_default="0"),
        )

    if _has_table("minute_purchases"):
        return
    op.create_table(
        "minute_purchases",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("minutes", sa.Integer(), nullable=False),
        sa.Column("rupees", sa.Numeric(14, 4), nullable=False),
        sa.Column("rate_per_minute", sa.Numeric(6, 2), nullable=False),
        sa.Column("source", sa.String(), nullable=False, server_default="topup"),
        sa.Column("razorpay_payment_id", sa.String(), nullable=True),
        sa.Column("razorpay_ref", sa.String(), nullable=True),
        sa.Column("raw_event", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("razorpay_payment_id", name="uq_minute_purchases_rzp_payment_id"),
    )
    op.create_index(
        "ix_minute_purchases_organization_id", "minute_purchases", ["organization_id"]
    )
    op.create_index(
        "ix_minute_purchases_org_created", "minute_purchases", ["organization_id", "created_at"]
    )


def downgrade() -> None:
    if _has_table("minute_purchases"):
        op.drop_table("minute_purchases")
    if _has_column("call_costs", "prepaid_rupees"):
        op.drop_column("call_costs", "prepaid_rupees")
    if _has_column("subscriptions", "minutes"):
        op.drop_column("subscriptions", "minutes")
