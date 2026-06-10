"""Add handoff_note + handoff_note_generated_at to outgoing_leads.

The post-call condenser (app/services/call_condenser_service.py) writes a
3-sentence summary into ``handoff_note`` immediately after every outbound
call ends. The follow-up agent reads it back on the next call's preamble,
and the LeadsView surfaces it for manager review. Both columns nullable so
no backfill is required.

Revision ID: d7f4a2b9c103
Revises: c5e8b1f3a72d
Create Date: 2026-06-04 12:30:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "d7f4a2b9c103"
down_revision: Union[str, Sequence[str], None] = "c5e8b1f3a72d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("outgoing_leads", "handoff_note"):
        op.add_column(
            "outgoing_leads",
            sa.Column("handoff_note", sa.Text(), nullable=True),
        )
    if not _has_column("outgoing_leads", "handoff_note_generated_at"):
        op.add_column(
            "outgoing_leads",
            sa.Column(
                "handoff_note_generated_at",
                sa.DateTime(timezone=True),
                nullable=True,
            ),
        )


def downgrade() -> None:
    if _has_column("outgoing_leads", "handoff_note_generated_at"):
        op.drop_column("outgoing_leads", "handoff_note_generated_at")
    if _has_column("outgoing_leads", "handoff_note"):
        op.drop_column("outgoing_leads", "handoff_note")
