"""COGS visibility counters — LLM request count + TTS cache efficiency.

Three nullable ₹0 counters on ``call_costs``:
  * ``llm_requests``    — completions the call made (main turns + aux
    classifiers + post-call attribution).
  * ``tts_cache_hits`` / ``tts_cache_chars`` — TTS lines served free from the
    Redis byte-cache (never billed to Sarvam; cost columns are unaffected).

Nullable, no backfill: rows recorded before this landed stay NULL and the
SuperAdmin console renders "—", same convention as the original COGS columns.

Revision ID: cogs_cache_v1
Revises: campaign_blob_gin_v1
Create Date: 2026-07-09 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "cogs_cache_v1"
down_revision: Union[str, Sequence[str], None] = "campaign_blob_gin_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_NEW_COLUMNS = [
    ("llm_requests", sa.Integer()),
    ("tts_cache_hits", sa.Integer()),
    ("tts_cache_chars", sa.Integer()),
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
    if not _has_table("call_costs"):
        return
    for name, col_type in _NEW_COLUMNS:
        if not _has_column("call_costs", name):
            op.add_column("call_costs", sa.Column(name, col_type, nullable=True))


def downgrade() -> None:
    if not _has_table("call_costs"):
        return
    for name, _ in _NEW_COLUMNS:
        if _has_column("call_costs", name):
            op.drop_column("call_costs", name)
