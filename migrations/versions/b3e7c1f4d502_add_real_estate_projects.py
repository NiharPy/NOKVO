"""Add real_estate_projects table (per-org real-estate inventory).

Revision ID: b3e7c1f4d502
Revises: a1c5e9f4d806
Create Date: 2026-05-27 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "b3e7c1f4d502"
down_revision: Union[str, Sequence[str], None] = "a1c5e9f4d806"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "real_estate_projects",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, nullable=False),
        sa.Column(
            "organization_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organizations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("location", sa.String(length=500), nullable=True),
        sa.Column("rera_number", sa.String(length=120), nullable=True),
        sa.Column("property_type", sa.String(length=80), nullable=True),
        sa.Column("price_min", sa.Numeric(14, 2), nullable=True),
        sa.Column("price_max", sa.Numeric(14, 2), nullable=True),
        sa.Column("price_display", sa.String(length=200), nullable=True),
        sa.Column(
            "configurations",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column(
            "amenities",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("possession_date", sa.String(length=80), nullable=True),
        sa.Column("builder_name", sa.String(length=200), nullable=True),
        sa.Column("brochure_url", sa.String(length=1000), nullable=True),
        sa.Column("contact_phone", sa.String(length=40), nullable=True),
        sa.Column(
            "extra",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column("status", sa.String(length=40), nullable=False, server_default="active"),
        sa.Column(
            "created_by_user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization_users.id"),
            nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index(
        "ix_real_estate_projects_organization_id",
        "real_estate_projects",
        ["organization_id"],
    )
    op.create_index(
        "ix_real_estate_projects_org_status",
        "real_estate_projects",
        ["organization_id", "status"],
    )


def downgrade() -> None:
    op.drop_index("ix_real_estate_projects_org_status", table_name="real_estate_projects")
    op.drop_index("ix_real_estate_projects_organization_id", table_name="real_estate_projects")
    op.drop_table("real_estate_projects")
