"""Per-call diagnostics + retry scheduling on outbound campaign contacts.

Everything the dialer needed to explain itself was being computed and thrown
away. The hangup cause was read on the webhook, used for one two-way branch and
discarded, so "no_pickup" collapsed four completely different problems —
invalid number, phone switched off, call rejected, genuine no-answer — into one
bucket with four different fixes. The rendition/opener variant a call spoke was
chosen deterministically and never recorded, so the humanization rollout could
not be measured. And a call abandoned at the concurrency cap was indistinguishable
from one nobody answered.

Adds, all nullable/defaulted and therefore backfill-free:
  * ``hangup_cause``    — the carrier's verbatim cause, for the histogram that
                          decides whether a miss is even worth retrying.
  * ``opener_variant`` / ``tts_variant`` — which take this call spoke.
  * ``abandoned``       — connected but no conversation slot was free (ours, not
                          theirs).
  * ``next_attempt_at`` — when the retry cadence should re-arm this row.

Indexes:
  * ``(campaign_id, hangup_cause)`` for the cause histogram.
  * a partial index on rows due for a retry.
  * ``ix_occ_campaign_pending`` is REPLACED by ``(campaign_id, attempt,
    created_at)`` partial on pending. The old index carried campaign_id alone, so
    the dialer's ``ORDER BY ... LIMIT k`` claim sorted every pending row for the
    campaign — at 1M rows a real cost that predates this change. The new one
    serves the claim's ordering in index order and lets LIMIT stop early.

Revision ID: apex_call_diagnostics_v1
Revises: offering_backfill_v1
Create Date: 2026-08-14 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "apex_call_diagnostics_v1"
down_revision: Union[str, Sequence[str], None] = "offering_backfill_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

OCC = "outbound_campaign_contacts"


def _insp():
    return inspect(op.get_bind())


def _cols(table: str) -> set[str]:
    ins = _insp()
    if table not in ins.get_table_names():
        return set()
    return {c["name"] for c in ins.get_columns(table)}


def _indexes(table: str) -> set[str]:
    ins = _insp()
    if table not in ins.get_table_names():
        return set()
    return {i["name"] for i in ins.get_indexes(table)}


def upgrade() -> None:
    have = _cols(OCC)
    if not have:
        return  # table not present (fresh DB builds it from the models)

    def add(name: str, col) -> None:
        if name not in have:
            op.add_column(OCC, sa.Column(name, col, nullable=True))

    add("hangup_cause", sa.String())
    add("opener_variant", sa.SmallInteger())
    add("tts_variant", sa.SmallInteger())
    add("abandoned", sa.Boolean())
    add("next_attempt_at", sa.DateTime(timezone=True))

    # abandoned is a counted fact, not a tri-state — default it so the pacer's
    # abandon-rate query never has to reason about NULL.
    if "abandoned" not in have:
        op.execute(f"UPDATE {OCC} SET abandoned = false WHERE abandoned IS NULL")
        op.alter_column(OCC, "abandoned", nullable=False, server_default="false")

    idx = _indexes(OCC)
    if "ix_occ_campaign_hangup_cause" not in idx:
        op.create_index("ix_occ_campaign_hangup_cause", OCC, ["campaign_id", "hangup_cause"])
    if "ix_occ_retry_due" not in idx:
        op.create_index(
            "ix_occ_retry_due",
            OCC,
            ["campaign_id", "next_attempt_at"],
            postgresql_where=sa.text("status = 'no_answer' AND next_attempt_at IS NOT NULL"),
        )
    # Claim ordering: attempt-then-age, so first attempts always precede retries
    # and a freshly-ingested CRM lead is never queued behind a retry backlog.
    if "ix_occ_campaign_claim" not in idx:
        op.create_index(
            "ix_occ_campaign_claim",
            OCC,
            ["campaign_id", "attempt", "created_at"],
            postgresql_where=sa.text("status = 'pending'"),
        )
    # Superseded by the above (campaign_id alone could not serve the ordering).
    if "ix_occ_campaign_pending" in idx:
        op.drop_index("ix_occ_campaign_pending", table_name=OCC)


def downgrade() -> None:
    idx = _indexes(OCC)
    if "ix_occ_campaign_pending" not in idx:
        op.create_index(
            "ix_occ_campaign_pending", OCC, ["campaign_id"],
            postgresql_where=sa.text("status = 'pending'"),
        )
    for name in ("ix_occ_campaign_claim", "ix_occ_retry_due", "ix_occ_campaign_hangup_cause"):
        if name in idx:
            op.drop_index(name, table_name=OCC)
    have = _cols(OCC)
    for name in ("next_attempt_at", "abandoned", "tts_variant", "opener_variant", "hangup_cause"):
        if name in have:
            op.drop_column(OCC, name)
