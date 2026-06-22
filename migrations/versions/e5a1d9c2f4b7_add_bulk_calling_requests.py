"""Add bulk_calling_requests table.

Tenants on the Inbound + Outbound plan request access to the Bulk CSV Calling
add-on (leaving a contact number); the SuperAdmin grants it from the console by
provisioning dedicated Plivo telephony. This table is the request/audit trail;
the granted telephony lives on TenantResources.provider_status["bulk_calling"].

Revision ID: e5a1d9c2f4b7
Revises: c7b6a5d4e3f2
Create Date: 2026-06-22 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision: str = "e5a1d9c2f4b7"
down_revision: Union[str, Sequence[str], None] = "c7b6a5d4e3f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def upgrade() -> None:
    if _has_table("bulk_calling_requests"):
        return
    op.create_table(
        "bulk_calling_requests",
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
        sa.Column("contact_number", sa.String(), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "reviewed_by_superadmin_id",
            UUID(as_uuid=True),
            sa.ForeignKey("superadmin_users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_bulk_calling_requests_organization_id",
        "bulk_calling_requests",
        ["organization_id"],
    )
    op.create_index(
        "ix_bulk_calling_requests_tenant_id",
        "bulk_calling_requests",
        ["tenant_id"],
    )
    op.create_index(
        "ix_bulk_calling_requests_status_created",
        "bulk_calling_requests",
        ["status", "created_at"],
    )


def downgrade() -> None:
    if _has_table("bulk_calling_requests"):
        op.drop_table("bulk_calling_requests")
