"""Add clinic_services + clinic_service_providers.

The Clinics vertical lets an org define its own service catalog
(``clinic_services``) and map each service to the doctors who provide it
(``clinic_service_providers``, M2M). The inbound agent uses these to answer
service questions and to route a booking service-first to an eligible doctor.

Revision ID: e2c9a4f17b85
Revises: d7f4a2b9c103
Create Date: 2026-06-08 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects.postgresql import UUID


revision: str = "e2c9a4f17b85"
down_revision: Union[str, Sequence[str], None] = "d7f4a2b9c103"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    return table_name in inspect(bind).get_table_names()


def upgrade() -> None:
    if not _has_table("clinic_services"):
        op.create_table(
            "clinic_services",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("department", sa.String(length=160), nullable=True),
            sa.Column("duration_minutes", sa.Integer(), nullable=True),
            sa.Column("price", sa.Numeric(12, 2), nullable=True),
            sa.Column("price_display", sa.String(length=120), nullable=True),
            sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
            sa.Column(
                "created_by_user_id",
                UUID(as_uuid=True),
                sa.ForeignKey("organization_users.id"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    if not _has_table("clinic_service_providers"):
        op.create_table(
            "clinic_service_providers",
            sa.Column("id", UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "service_id",
                UUID(as_uuid=True),
                sa.ForeignKey("clinic_services.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column(
                "member_id",
                UUID(as_uuid=True),
                sa.ForeignKey("organization_users.id", ondelete="CASCADE"),
                nullable=False,
                index=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("service_id", "member_id", name="uq_clinic_service_provider"),
        )


def downgrade() -> None:
    if _has_table("clinic_service_providers"):
        op.drop_table("clinic_service_providers")
    if _has_table("clinic_services"):
        op.drop_table("clinic_services")
