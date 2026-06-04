"""Add lead_followup_schedules table — drives the follow-up agent loop.

One row per scheduled follow-up call. The follow-up scheduler drains rows whose
``scheduled_at`` is due and ``status='pending'``. Two indexes:
  - ix_followup_due  (status, scheduled_at)  — drain query hot path
  - ix_followup_lead (lead_id, status)       — per-lead chip + cancel sweeps

Revision ID: c5e8b1f3a72d
Revises: b3e7c1f4d502
Create Date: 2026-06-04 11:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "c5e8b1f3a72d"
down_revision: Union[str, Sequence[str], None] = "b3e7c1f4d502"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_FOLLOWUP_REASON_VALUES = (
    "promise_extracted",
    "no_answer_retry",
    "voicemail_retry",
    "failed_retry",
    "partial_retry",
    "manual",
)

_FOLLOWUP_STATUS_VALUES = (
    "pending",
    "in_flight",
    "completed",
    "exhausted",
    "cancelled",
    "paused",
)


def _has_table(table_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    return table_name in inspector.get_table_names()


def upgrade() -> None:
    if _has_table("lead_followup_schedules"):
        return

    op.create_table(
        "lead_followup_schedules",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "tenant_id",
            sa.String(),
            sa.ForeignKey("tenant_resources.tenant_id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "lead_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outgoing_leads.id"),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "campaign_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("outbound_campaigns.id"),
            nullable=True,
            index=True,
        ),
        sa.Column("source_call_id", sa.String(), nullable=True),
        sa.Column(
            "scheduled_at",
            sa.DateTime(timezone=True),
            nullable=False,
            index=True,
        ),
        sa.Column(
            "reason",
            sa.Enum(*_FOLLOWUP_REASON_VALUES, name="followupreason"),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "status",
            sa.Enum(*_FOLLOWUP_STATUS_VALUES, name="followupstatus"),
            nullable=False,
            server_default="pending",
            index=True,
        ),
        sa.Column("placed_call_id", sa.String(), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "paused_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("organization_users.id"),
            nullable=True,
        ),
        sa.Column("cancelled_reason", sa.String(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.create_index(
        "ix_followup_due",
        "lead_followup_schedules",
        ["status", "scheduled_at"],
    )
    op.create_index(
        "ix_followup_lead",
        "lead_followup_schedules",
        ["lead_id", "status"],
    )


def downgrade() -> None:
    if not _has_table("lead_followup_schedules"):
        return
    op.drop_index("ix_followup_lead", table_name="lead_followup_schedules")
    op.drop_index("ix_followup_due", table_name="lead_followup_schedules")
    op.drop_table("lead_followup_schedules")
    # Drop the enum types so a future re-run won't collide.
    sa.Enum(name="followupstatus").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="followupreason").drop(op.get_bind(), checkfirst=True)
