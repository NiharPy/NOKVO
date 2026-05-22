"""Nokvo Connect: organization_api_keys + connect_sessions

Revision ID: d8f1c0b9a4e7
Revises: c4d5e6f7a8b9
Create Date: 2026-05-23 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "d8f1c0b9a4e7"
down_revision: Union[str, Sequence[str], None] = "c4d5e6f7a8b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "organization_api_keys",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("key_prefix", sa.String(length=16), nullable=False, unique=True),
        sa.Column("secret_hash", sa.String(), nullable=False),
        sa.Column("mode", sa.String(), nullable=False, server_default=sa.text("'live'")),
        sa.Column("scopes", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("allowed_origins", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("rate_limit_rpm", sa.Integer(), nullable=False, server_default="60"),
        sa.Column("max_concurrent_sessions", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("webhook_url", sa.String(), nullable=True),
        sa.Column("webhook_secret_hash", sa.String(), nullable=True),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'active'")),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_organization_api_keys_org",
        "organization_api_keys",
        ["organization_id"],
    )

    op.create_table(
        "connect_sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
        sa.Column("api_key_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization_api_keys.id"), nullable=False),
        sa.Column("channel", sa.String(), nullable=False, server_default=sa.text("'voice'")),
        sa.Column("status", sa.String(), nullable=False, server_default=sa.text("'created'")),
        sa.Column("language", sa.String(), nullable=False, server_default=sa.text("'en'")),
        sa.Column("client_session_id", sa.String(), nullable=True),
        sa.Column("origin", sa.String(), nullable=True),
        sa.Column("transcript", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("usage", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("failure_reason", sa.String(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_connect_sessions_org", "connect_sessions", ["organization_id"])
    op.create_index("ix_connect_sessions_api_key", "connect_sessions", ["api_key_id"])


def downgrade() -> None:
    op.drop_index("ix_connect_sessions_api_key", table_name="connect_sessions")
    op.drop_index("ix_connect_sessions_org", table_name="connect_sessions")
    op.drop_table("connect_sessions")
    op.drop_index("ix_organization_api_keys_org", table_name="organization_api_keys")
    op.drop_table("organization_api_keys")
