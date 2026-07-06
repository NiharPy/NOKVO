"""Add apex_support_tickets — tickets raised in-product via Nova.

One row per ticket with the diagnosis snapshot Nova attached at raise time
(recent call failures / campaign states / wallet), so the operator sees the
problem without asking the user to reproduce it.

Revision ID: apex_support_tickets_v1
Revises: campaign_contacts_scale_v1
Create Date: 2026-07-06 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import JSONB, UUID


revision: str = "apex_support_tickets_v1"
down_revision: Union[str, Sequence[str], None] = "campaign_contacts_scale_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("apex_support_tickets"):
        return
    op.create_table(
        "apex_support_tickets",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "organization_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("tenant_id", sa.String(), nullable=True),
        sa.Column(
            "requested_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("organization_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("requested_by_email", sa.String(), nullable=True),
        sa.Column("subject", sa.String(300), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("diagnosis", JSONB(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="open"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("resolution_note", sa.Text(), nullable=True),
    )
    op.create_index("ix_apex_support_tickets_organization_id", "apex_support_tickets", ["organization_id"])
    op.create_index("ix_apex_support_tickets_tenant_id", "apex_support_tickets", ["tenant_id"])
    op.create_index("ix_apex_support_tickets_status_created", "apex_support_tickets", ["status", "created_at"])


def downgrade() -> None:
    if _has_table("apex_support_tickets"):
        op.drop_table("apex_support_tickets")
