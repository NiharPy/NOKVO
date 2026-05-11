"""Add Nokvo One product tiers, org status, calling gate, password auth, encrypted TOTP v2, email verification and invitation tables.

Revision ID: c8d4e7f1a902
Revises: a1b2c3d4e5f6
Create Date: 2026-05-11 12:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "c8d4e7f1a902"
down_revision: Union[str, Sequence[str], None] = "a1b2c3d4e5f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    cols = {col["name"] for col in inspector.get_columns(table_name)}
    return column_name in cols


def _has_constraint(table_name: str, constraint_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    cks = {c["name"] for c in inspector.get_check_constraints(table_name)}
    return constraint_name in cks


def upgrade() -> None:
    if _has_table("organizations"):
        if not _has_column("organizations", "product_tier"):
            op.add_column(
                "organizations",
                sa.Column(
                    "product_tier",
                    sa.String(),
                    nullable=False,
                    server_default="nokvo_prime",
                ),
            )
        if not _has_column("organizations", "status"):
            op.add_column(
                "organizations",
                sa.Column(
                    "status",
                    sa.String(),
                    nullable=False,
                    server_default="active",
                ),
            )
        if not _has_column("organizations", "calling_enabled"):
            op.add_column(
                "organizations",
                sa.Column(
                    "calling_enabled",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                ),
            )

        op.execute(
            "UPDATE organizations SET product_tier = 'nokvo_prime' WHERE product_tier IS NULL"
        )
        op.execute(
            "UPDATE organizations SET status = 'active' WHERE status IS NULL"
        )
        op.execute(
            "UPDATE organizations SET calling_enabled = TRUE WHERE product_tier = 'nokvo_prime'"
        )

        if not _has_constraint("organizations", "ck_organizations_product_tier"):
            op.create_check_constraint(
                "ck_organizations_product_tier",
                "organizations",
                "product_tier IN ('nokvo_one','nokvo_prime','nokvo_apex')",
            )
        if not _has_constraint("organizations", "ck_organizations_status"):
            op.create_check_constraint(
                "ck_organizations_status",
                "organizations",
                "status IN ('pending_email_verification','pending_totp','pending_approval','active','suspended')",
            )

    if _has_table("organization_users"):
        if not _has_column("organization_users", "password_hash"):
            op.add_column(
                "organization_users",
                sa.Column("password_hash", sa.String(), nullable=True),
            )
        if not _has_column("organization_users", "totp_secret_encrypted_v2"):
            op.add_column(
                "organization_users",
                sa.Column("totp_secret_encrypted_v2", sa.String(), nullable=True),
            )

    if not _has_table("email_verifications"):
        op.create_table(
            "email_verifications",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "organization_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organization_users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("email", sa.String(), nullable=False),
            sa.Column("token_hash", sa.String(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_email_verifications_email", "email_verifications", ["email"])
        op.create_unique_constraint(
            "uq_email_verifications_token_hash",
            "email_verifications",
            ["token_hash"],
        )

    if not _has_table("nokvo_one_agents"):
        op.create_table(
            "nokvo_one_agents",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(), nullable=False),
            sa.Column("description", sa.Text(), nullable=True),
            sa.Column("system_prompt", sa.Text(), nullable=True),
            sa.Column("tool_keys", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column(
                "created_by_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organization_users.id"),
                nullable=True,
            ),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_nokvo_one_agents_org", "nokvo_one_agents", ["organization_id"])

    if not _has_table("nokvo_one_tool_records"):
        op.create_table(
            "nokvo_one_tool_records",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_by_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organization_users.id"),
                nullable=True,
            ),
            sa.Column("record_type", sa.String(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="open"),
            sa.Column("data", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index(
            "ix_nokvo_one_tool_records_org_type",
            "nokvo_one_tool_records",
            ["organization_id", "record_type"],
        )

    if not _has_table("member_invitations"):
        op.create_table(
            "member_invitations",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "organization_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organizations.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "organization_user_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organization_users.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "invited_by",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("organization_users.id"),
                nullable=True,
            ),
            sa.Column("email", sa.String(), nullable=False),
            sa.Column("role", sa.String(), nullable=False, server_default="member"),
            sa.Column("token_hash", sa.String(), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_member_invitations_email", "member_invitations", ["email"])
        op.create_unique_constraint(
            "uq_member_invitations_token_hash",
            "member_invitations",
            ["token_hash"],
        )


def downgrade() -> None:
    if _has_table("member_invitations"):
        op.drop_constraint("uq_member_invitations_token_hash", "member_invitations", type_="unique")
        op.drop_index("ix_member_invitations_email", "member_invitations")
        op.drop_table("member_invitations")
    if _has_table("nokvo_one_tool_records"):
        op.drop_index("ix_nokvo_one_tool_records_org_type", "nokvo_one_tool_records")
        op.drop_table("nokvo_one_tool_records")
    if _has_table("nokvo_one_agents"):
        op.drop_index("ix_nokvo_one_agents_org", "nokvo_one_agents")
        op.drop_table("nokvo_one_agents")
    if _has_table("email_verifications"):
        op.drop_constraint("uq_email_verifications_token_hash", "email_verifications", type_="unique")
        op.drop_index("ix_email_verifications_email", "email_verifications")
        op.drop_table("email_verifications")
    if _has_table("organization_users"):
        if _has_column("organization_users", "totp_secret_encrypted_v2"):
            op.drop_column("organization_users", "totp_secret_encrypted_v2")
        if _has_column("organization_users", "password_hash"):
            op.drop_column("organization_users", "password_hash")
    if _has_table("organizations"):
        if _has_constraint("organizations", "ck_organizations_status"):
            op.drop_constraint("ck_organizations_status", "organizations", type_="check")
        if _has_constraint("organizations", "ck_organizations_product_tier"):
            op.drop_constraint("ck_organizations_product_tier", "organizations", type_="check")
        if _has_column("organizations", "calling_enabled"):
            op.drop_column("organizations", "calling_enabled")
        if _has_column("organizations", "status"):
            op.drop_column("organizations", "status")
        if _has_column("organizations", "product_tier"):
            op.drop_column("organizations", "product_tier")
