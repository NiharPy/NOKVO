"""Add customer_base table + customer-targeted follow-ups.

customer_base records every inbound caller, deduplicated per tenant on
(tenant_id, phone_e164). Populated for all tenants at call end; surfaced in
the UI for clinics only. `name` is fill-only at the service layer (COALESCE)
because STT mishears names — admin PATCH is the only overwrite path.

lead_followup_schedules learns to target a customer instead of a lead:
lead_id becomes nullable, customer_id (FK customer_base) is added, plus a
CHECK that exactly one target style is present, and `note` — the admin's
typed purpose for a manual follow-up (rendered into the outbound prompt).

Revision ID: b9c2d5e8f1a3
Revises: a8b3c1d4e9f2
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision: str = "b9c2d5e8f1a3"
down_revision: Union[str, Sequence[str], None] = "a8b3c1d4e9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    return table_name in inspect(bind).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_table("customer_base"):
        op.create_table(
            "customer_base",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.String(),
                sa.ForeignKey("tenant_resources.tenant_id"),
                nullable=False,
                index=True,
            ),
            sa.Column("phone_e164", sa.String(), nullable=False),
            sa.Column("name", sa.String(), nullable=True),
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_call_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("call_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_call_id", sa.String(), nullable=True),
            sa.Column("last_call_summary", sa.Text(), nullable=True),
            sa.Column("opt_out", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("tenant_id", "phone_e164", name="uq_customer_base_tenant_phone"),
        )
        op.create_index(
            "ix_customer_base_tenant_lastcall",
            "customer_base",
            ["tenant_id", "last_call_at"],
        )

    if not _has_column("lead_followup_schedules", "customer_id"):
        op.alter_column("lead_followup_schedules", "lead_id", nullable=True)
        op.add_column(
            "lead_followup_schedules",
            sa.Column(
                "customer_id",
                UUID(as_uuid=True),
                sa.ForeignKey("customer_base.id"),
                nullable=True,
            ),
        )
        op.add_column(
            "lead_followup_schedules",
            sa.Column("note", sa.Text(), nullable=True),
        )
        op.create_index(
            "ix_followup_customer",
            "lead_followup_schedules",
            ["customer_id", "status"],
        )
        op.create_check_constraint(
            "ck_followup_has_target",
            "lead_followup_schedules",
            "(lead_id IS NOT NULL) OR (customer_id IS NOT NULL)",
        )


def downgrade() -> None:
    if _has_column("lead_followup_schedules", "customer_id"):
        op.drop_constraint("ck_followup_has_target", "lead_followup_schedules", type_="check")
        op.drop_index("ix_followup_customer", table_name="lead_followup_schedules")
        op.drop_column("lead_followup_schedules", "note")
        op.drop_column("lead_followup_schedules", "customer_id")
        op.execute("DELETE FROM lead_followup_schedules WHERE lead_id IS NULL")
        op.alter_column("lead_followup_schedules", "lead_id", nullable=False)

    if _has_table("customer_base"):
        op.drop_index("ix_customer_base_tenant_lastcall", table_name="customer_base")
        op.drop_table("customer_base")
