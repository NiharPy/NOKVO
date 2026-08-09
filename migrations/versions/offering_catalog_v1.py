"""offering_catalog_v1 — generic Offering catalog (P2 of the NOKVO ONE SDK).

Creates the ``offerings`` table: one shape for real-estate projects, clinic
services, ecommerce products and service packages (distinguished by ``kind``).
``RealEstateProject`` / ``ClinicService`` become adapters over this; their read
paths are byte-preserved. Additive only — this migration moves no data.

Revision ID: offering_catalog_v1
Revises: apex_plans_v1
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql

revision: str = "offering_catalog_v1"
down_revision: Union[str, Sequence[str], None] = "apex_plans_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _insp():
    return inspect(op.get_bind())


def upgrade() -> None:
    insp = _insp()
    if "offerings" not in insp.get_table_names():
        op.create_table(
            "offerings",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(length=40), nullable=False),
            sa.Column("name", sa.String(length=200), nullable=False),
            sa.Column("category", sa.String(length=160), nullable=True),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("price", sa.Numeric(14, 2), nullable=True),
            sa.Column("price_display", sa.String(length=200), nullable=True),
            sa.Column("duration_min", sa.Integer(), nullable=True),
            sa.Column("attributes", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("availability", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("provider_ids", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
            sa.Column("media", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
            sa.Column("status", sa.String(length=40), nullable=False, server_default="active"),
            sa.Column(
                "created_by_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organization_users.id"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )

    have_table = "offerings" in _insp().get_table_names()
    existing_idx = {i["name"] for i in _insp().get_indexes("offerings")} if have_table else set()
    if "ix_offerings_organization_id" not in existing_idx:
        op.create_index("ix_offerings_organization_id", "offerings", ["organization_id"])
    if "ix_offerings_kind" not in existing_idx:
        op.create_index("ix_offerings_kind", "offerings", ["kind"])


def downgrade() -> None:
    insp = _insp()
    if "offerings" in insp.get_table_names():
        idx = {i["name"] for i in insp.get_indexes("offerings")}
        if "ix_offerings_kind" in idx:
            op.drop_index("ix_offerings_kind", table_name="offerings")
        if "ix_offerings_organization_id" in idx:
            op.drop_index("ix_offerings_organization_id", table_name="offerings")
        op.drop_table("offerings")
