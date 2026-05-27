"""Security hardening: refresh indexes and voice data audit log.

Revision ID: e3b7c2a9d105
Revises: d8f1c0b9a4e7
Create Date: 2026-05-25 01:45:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "e3b7c2a9d105"
down_revision: Union[str, Sequence[str], None] = "d8f1c0b9a4e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(name: str) -> bool:
    return name in inspect(op.get_bind()).get_table_names()


def _has_index(table: str, name: str) -> bool:
    return any(index["name"] == name for index in inspect(op.get_bind()).get_indexes(table))


def _create_index_once(name: str, table: str, columns: list[str]) -> None:
    if _has_table(table) and not _has_index(table, name):
        op.create_index(name, table, columns)


def upgrade() -> None:
    _create_index_once("ix_superadmin_sessions_refresh_token_hash", "superadmin_sessions", ["refresh_token_hash"])
    _create_index_once(
        "ix_superadmin_sessions_refresh_active",
        "superadmin_sessions",
        ["refresh_token_hash", "revoked_at", "expires_at"],
    )
    _create_index_once("ix_organization_sessions_refresh_token_hash", "organization_sessions", ["refresh_token_hash"])
    _create_index_once(
        "ix_organization_sessions_refresh_active",
        "organization_sessions",
        ["refresh_token_hash", "revoked_at", "expires_at"],
    )

    if not _has_table("voice_data_access_audit_logs"):
        op.create_table(
            "voice_data_access_audit_logs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id"), nullable=False),
            sa.Column("tenant_id", sa.String(), nullable=True),
            sa.Column("actor_type", sa.String(), nullable=False),
            sa.Column("actor_id", sa.String(), nullable=True),
            sa.Column("access_type", sa.String(), nullable=False),
            sa.Column("resource_type", sa.String(), nullable=False),
            sa.Column("resource_id", sa.String(), nullable=True),
            sa.Column("call_id", sa.String(), nullable=True),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("ip_address", postgresql.INET(), nullable=True),
            sa.Column("user_agent", sa.String(), nullable=True),
            sa.Column("request_id", sa.String(), nullable=True),
            sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=True),
        )
    _create_index_once("ix_voice_data_access_audit_logs_organization_id", "voice_data_access_audit_logs", ["organization_id"])
    _create_index_once("ix_voice_data_access_audit_logs_tenant_id", "voice_data_access_audit_logs", ["tenant_id"])
    _create_index_once("ix_voice_data_access_audit_logs_call_id", "voice_data_access_audit_logs", ["call_id"])


def downgrade() -> None:
    if _has_table("voice_data_access_audit_logs"):
        for name in (
            "ix_voice_data_access_audit_logs_call_id",
            "ix_voice_data_access_audit_logs_tenant_id",
            "ix_voice_data_access_audit_logs_organization_id",
        ):
            if _has_index("voice_data_access_audit_logs", name):
                op.drop_index(name, table_name="voice_data_access_audit_logs")
        op.drop_table("voice_data_access_audit_logs")
    for table, name in (
        ("organization_sessions", "ix_organization_sessions_refresh_active"),
        ("organization_sessions", "ix_organization_sessions_refresh_token_hash"),
        ("superadmin_sessions", "ix_superadmin_sessions_refresh_active"),
        ("superadmin_sessions", "ix_superadmin_sessions_refresh_token_hash"),
    ):
        if _has_table(table) and _has_index(table, name):
            op.drop_index(name, table_name=table)
