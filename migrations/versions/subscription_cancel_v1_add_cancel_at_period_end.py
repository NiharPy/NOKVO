"""Add cancel_at_period_end to subscriptions.

The cancel-subscription endpoint cancels the Razorpay subscription AT CYCLE END
(cancel_at_cycle_end=1): the row stays status="active" and service continues until
current_period_end, then Razorpay stops billing. Razorpay's status alone can't
express "active but scheduled to stop", so we track it locally. NOT NULL with a
server default of false, so existing subscriptions backfill to false — no
data migration needed.

Revision ID: subscription_cancel_v1
Revises: prepaid_minutes_v1
Create Date: 2026-07-02 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "subscription_cancel_v1"
down_revision: Union[str, Sequence[str], None] = "prepaid_minutes_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column(table_name: str, column_name: str) -> bool:
    bind = op.get_bind()
    inspector = inspect(bind)
    if table_name not in inspector.get_table_names():
        return False
    return column_name in {c["name"] for c in inspector.get_columns(table_name)}


def upgrade() -> None:
    if not _has_column("subscriptions", "cancel_at_period_end"):
        op.add_column(
            "subscriptions",
            sa.Column(
                "cancel_at_period_end",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )


def downgrade() -> None:
    if _has_column("subscriptions", "cancel_at_period_end"):
        op.drop_column("subscriptions", "cancel_at_period_end")
