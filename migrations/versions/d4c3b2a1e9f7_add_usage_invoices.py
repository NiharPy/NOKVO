"""Add usage_invoices table.

One row per organization per monthly billing cycle, written by the recurring
usage-invoice mailer. ``(organization_id, period_start)`` is unique so a cycle
is never billed or emailed twice.

Revision ID: d4c3b2a1e9f7
Revises: e1f2a3b4c5d6
Create Date: 2026-06-20 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision: str = "d4c3b2a1e9f7"
down_revision: Union[str, Sequence[str], None] = "e1f2a3b4c5d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("usage_invoices"):
        return
    op.create_table(
        "usage_invoices",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
        sa.Column("period_end", sa.DateTime(timezone=True), nullable=False),
        sa.Column("minutes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("call_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("amount_inr", sa.Numeric(14, 4), nullable=False, server_default="0"),
        sa.Column("status", sa.String(), nullable=False, server_default="sent"),
        sa.Column("email_to", sa.String(), nullable=True),
        sa.Column("error", sa.String(), nullable=True),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("organization_id", "period_start", name="uq_usage_invoices_org_period"),
    )
    op.create_index("ix_usage_invoices_organization_id", "usage_invoices", ["organization_id"])
    op.create_index("ix_usage_invoices_tenant_id", "usage_invoices", ["tenant_id"])
    op.create_index("ix_usage_invoices_org_period_end", "usage_invoices", ["organization_id", "period_end"])


def downgrade() -> None:
    if _has_table("usage_invoices"):
        op.drop_table("usage_invoices")
