"""Add Nokvo One assignment settings, clinic schedules, blocked slots, and audit logs.

Revision ID: d9e8f7a6b5c4
Revises: c8d4e7f1a902
Create Date: 2026-05-16 18:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "d9e8f7a6b5c4"
down_revision: Union[str, Sequence[str], None] = "c8d4e7f1a902"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if not _has_table("organization_member_assignment_settings"):
        op.create_table(
            "organization_member_assignment_settings",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("is_assignable", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("working_days", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("start_time", sa.Time(), nullable=True),
            sa.Column("end_time", sa.Time(), nullable=True),
            sa.Column("timezone", sa.String(), nullable=False, server_default="Asia/Kolkata"),
            sa.Column("request_types", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("max_active_requests", sa.Integer(), nullable=False, server_default="3"),
            sa.Column("max_requests_per_day", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("member_id", name="uq_member_assignment_settings_member"),
        )
        op.create_index("ix_member_assignment_settings_org", "organization_member_assignment_settings", ["organization_id"])
        op.create_index("ix_member_assignment_settings_member", "organization_member_assignment_settings", ["member_id"])

    if not _has_table("clinic_member_schedule_settings"):
        op.create_table(
            "clinic_member_schedule_settings",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("appointment_duration_minutes", sa.Integer(), nullable=False, server_default="30"),
            sa.Column("buffer_minutes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("max_patients_per_hour", sa.Integer(), nullable=True),
            sa.Column("max_patients_per_day", sa.Integer(), nullable=True),
            sa.Column("consultation_types", postgresql.JSONB(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.UniqueConstraint("member_id", name="uq_clinic_schedule_settings_member"),
        )
        op.create_index("ix_clinic_schedule_settings_org", "clinic_member_schedule_settings", ["organization_id"])
        op.create_index("ix_clinic_schedule_settings_member", "clinic_member_schedule_settings", ["member_id"])

    if not _has_table("member_blocked_slots"):
        op.create_table(
            "member_blocked_slots",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization_users.id", ondelete="CASCADE"), nullable=False),
            sa.Column("start_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("end_time", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("repeat_rule", sa.String(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_member_blocked_slots_org", "member_blocked_slots", ["organization_id"])
        op.create_index("ix_member_blocked_slots_member", "member_blocked_slots", ["member_id"])

    if not _has_table("nokvo_one_assignment_audit_logs"):
        op.create_table(
            "nokvo_one_assignment_audit_logs",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("request_id", postgresql.UUID(as_uuid=True), nullable=True),
            sa.Column("organization_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False),
            sa.Column("selected_member_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization_users.id", ondelete="SET NULL"), nullable=True),
            sa.Column("request_type", sa.String(), nullable=False),
            sa.Column("assignment_status", sa.String(), nullable=False),
            sa.Column("reason", sa.String(), nullable=True),
            sa.Column("skipped_member_reasons", postgresql.JSONB(), nullable=False, server_default="{}"),
            sa.Column("selected_member_active_load", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        )
        op.create_index("ix_assignment_audit_logs_request", "nokvo_one_assignment_audit_logs", ["request_id"])
        op.create_index("ix_assignment_audit_logs_org", "nokvo_one_assignment_audit_logs", ["organization_id"])
        op.create_index("ix_assignment_audit_logs_member", "nokvo_one_assignment_audit_logs", ["selected_member_id"])


def downgrade() -> None:
    for table in [
        "nokvo_one_assignment_audit_logs",
        "member_blocked_slots",
        "clinic_member_schedule_settings",
        "organization_member_assignment_settings",
    ]:
        if _has_table(table):
            op.drop_table(table)
