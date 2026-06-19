"""SuperAdmin console overhaul: per-call COGS columns + drop approval workflow.

Three changes land together because they're one product change (the console
rebuild):

1. Add the per-call COGS breakdown columns to ``call_costs`` (STT / LLM / TTS /
   Plivo usage counters + their INR costs). All nullable — historical rows stay
   NULL and render as "total-only", so no backfill.
2. Drop the unused ``superadmin_approval_requests`` table — org approval is
   removed; no org needs a SuperAdmin sign-off anymore.
3. Data migration: collapse the ``pending_approval`` org state into ``active``
   (and promote the founding admin parked at ``invited`` for those orgs). Run
   inline so it executes deterministically in staging/prod. NOTE: the founding
   admin update is carefully scoped — ``invited`` is ALSO the status of genuine
   pending member invites, so we only touch ``role='admin' AND invited_by IS
   NULL`` users of orgs that were ``pending_approval``.

Revision ID: d5a8f1c3e9b2
Revises: a3d9f2c5b8e1
Create Date: 2026-06-19 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "d5a8f1c3e9b2"
down_revision: Union[str, Sequence[str], None] = "a3d9f2c5b8e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# New COGS columns on call_costs: (name, type). Counters first, then INR costs.
_COGS_COLUMNS = [
    ("llm_input_tokens", sa.Integer()),
    ("llm_output_tokens", sa.Integer()),
    ("llm_cached_tokens", sa.Integer()),
    ("stt_seconds", sa.Numeric(12, 4)),
    ("tts_characters", sa.Integer()),
    ("cost_stt_inr", sa.Numeric(12, 4)),
    ("cost_llm_inr", sa.Numeric(12, 4)),
    ("cost_tts_inr", sa.Numeric(12, 4)),
    ("cost_telephony_inr", sa.Numeric(12, 4)),
    ("cost_total_inr", sa.Numeric(12, 4)),
]


def _has_table(table_name: str) -> bool:
    return table_name in inspect(op.get_bind()).get_table_names()


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    # 1. Per-call COGS columns (all nullable — no backfill).
    if _has_table("call_costs"):
        for name, col_type in _COGS_COLUMNS:
            if not _has_column("call_costs", name):
                op.add_column("call_costs", sa.Column(name, col_type, nullable=True))

    # 2. Data: collapse pending_approval → active. Promote founding admins
    #    (role=admin, invited_by IS NULL) of those orgs FIRST, while the orgs
    #    are still flagged pending_approval, then flip the orgs themselves.
    if _has_table("organization_users") and _has_table("organizations"):
        op.execute(
            """
            UPDATE organization_users
            SET status = 'active'
            WHERE status = 'invited'
              AND role = 'admin'
              AND invited_by IS NULL
              AND organization_id IN (
                  SELECT id FROM organizations WHERE status = 'pending_approval'
              )
            """
        )
    if _has_table("organizations"):
        op.execute(
            "UPDATE organizations SET status = 'active' WHERE status = 'pending_approval'"
        )

    # 3. Drop the unused approval-request table.
    if _has_table("superadmin_approval_requests"):
        op.drop_table("superadmin_approval_requests")


def downgrade() -> None:
    # Recreate the approval table for schema parity (data is not restored).
    if not _has_table("superadmin_approval_requests"):
        op.create_table(
            "superadmin_approval_requests",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column("requested_by", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("superadmin_users.id")),
            sa.Column("approved_by", postgresql.UUID(as_uuid=True),
                      sa.ForeignKey("superadmin_users.id")),
            sa.Column("action", sa.String(), nullable=False),
            sa.Column("target_type", sa.String()),
            sa.Column("target_id", sa.String()),
            sa.Column("payload", postgresql.JSONB(), nullable=False),
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
            sa.Column("decided_at", sa.DateTime(timezone=True)),
            sa.Column("executed_at", sa.DateTime(timezone=True)),
        )

    # The pending_approval → active flip is intentionally NOT reversed: we have
    # no record of which orgs/users were previously pending.

    if _has_table("call_costs"):
        for name, _ in reversed(_COGS_COLUMNS):
            if _has_column("call_costs", name):
                op.drop_column("call_costs", name)
