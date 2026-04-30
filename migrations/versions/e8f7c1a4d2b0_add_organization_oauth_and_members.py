"""Add organization oauth and members

Revision ID: e8f7c1a4d2b0
Revises: cc39e7ac9142
Create Date: 2026-04-30 20:05:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, table, column
from sqlalchemy.dialects import postgresql

import uuid


revision: str = "e8f7c1a4d2b0"
down_revision: Union[str, Sequence[str], None] = "cc39e7ac9142"
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


def upgrade() -> None:
    bind = op.get_bind()

    if _has_table("organizations") and not _has_column("organizations", "email_domain"):
        op.add_column("organizations", sa.Column("email_domain", sa.String(), nullable=True))

    if _has_table("organizations") and _has_column("organizations", "admin_email") and _has_column("organizations", "email_domain"):
        op.execute(
            """
            UPDATE organizations
            SET email_domain = lower(split_part(admin_email, '@', 2))
            WHERE admin_email IS NOT NULL
              AND admin_email <> ''
              AND (email_domain IS NULL OR email_domain = '')
            """
        )

    if not _has_table("organization_users"):
        op.create_table(
            "organization_users",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("organization_id", sa.UUID(), nullable=False),
            sa.Column("invited_by", sa.UUID(), nullable=True),
            sa.Column("email", sa.String(), nullable=False),
            sa.Column("full_name", sa.String(), nullable=True),
            sa.Column("role", sa.String(), nullable=False, server_default="member"),
            sa.Column("status", sa.String(), nullable=False, server_default="invited"),
            sa.Column("auth_provider", sa.String(), nullable=False, server_default="google"),
            sa.Column("email_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("last_login_ip", postgresql.INET(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["invited_by"], ["organization_users.id"]),
            sa.PrimaryKeyConstraint("id"),
            sa.UniqueConstraint("organization_id", "email", name="uq_organization_users_org_email"),
        )

    if not _has_table("organization_sessions"):
        op.create_table(
            "organization_sessions",
            sa.Column("id", sa.UUID(), nullable=False),
            sa.Column("organization_user_id", sa.UUID(), nullable=False),
            sa.Column("refresh_token_hash", sa.String(), nullable=False),
            sa.Column("ip_address", postgresql.INET(), nullable=True),
            sa.Column("user_agent", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True, server_default=sa.text("now()")),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("revoke_reason", sa.String(), nullable=True),
            sa.ForeignKeyConstraint(["organization_user_id"], ["organization_users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id"),
        )

    if _has_table("organizations") and _has_table("organization_users"):
        organization_users = table(
            "organization_users",
            column("id", sa.UUID()),
            column("organization_id", sa.UUID()),
            column("email", sa.String()),
            column("full_name", sa.String()),
            column("role", sa.String()),
            column("status", sa.String()),
            column("auth_provider", sa.String()),
            column("email_verified", sa.Boolean()),
        )

        rows = bind.execute(
            sa.text(
                """
                SELECT id, admin_email, admin_name
                FROM organizations
                WHERE admin_email IS NOT NULL
                  AND admin_email <> ''
                """
            )
        ).mappings()

        for row in rows:
            email = row["admin_email"].strip().lower()
            exists = bind.execute(
                sa.text(
                    """
                    SELECT 1
                    FROM organization_users
                    WHERE organization_id = :organization_id
                      AND lower(email) = :email
                    LIMIT 1
                    """
                ),
                {"organization_id": row["id"], "email": email},
            ).first()
            if exists:
                continue

            bind.execute(
                organization_users.insert().values(
                    id=uuid.uuid4(),
                    organization_id=row["id"],
                    email=email,
                    full_name=row["admin_name"],
                    role="admin",
                    status="invited",
                    auth_provider="google",
                    email_verified=False,
                )
            )


def downgrade() -> None:
    if _has_table("organization_sessions"):
        op.drop_table("organization_sessions")
    if _has_table("organization_users"):
        op.drop_table("organization_users")
    if _has_table("organizations") and _has_column("organizations", "email_domain"):
        op.drop_column("organizations", "email_domain")
