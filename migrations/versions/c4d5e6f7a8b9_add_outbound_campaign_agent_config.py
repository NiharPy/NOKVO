"""Add agent_config JSONB column to outbound_campaigns.

Revision ID: c4d5e6f7a8b9
Revises: b6d7e8f9a0b1
Create Date: 2026-05-21 12:00:00.000000

The outbound voice agent reuses the inbound pipeline but needs a
per-campaign system prompt + ordered objectives so it can be proactive
instead of purely reactive. We store this beside the existing
``doc_text`` (which is the KB context document) in a single JSONB
column so future fields can be added without further migrations.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "c4d5e6f7a8b9"
down_revision: Union[str, Sequence[str], None] = "b6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "outbound_campaigns",
        sa.Column(
            "agent_config",
            postgresql.JSONB(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("outbound_campaigns", "agent_config")
