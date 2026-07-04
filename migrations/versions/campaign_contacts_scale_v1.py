"""Scale bulk-campaign contacts to per-row storage (1M-ready).

Promotes the O(n) JSONB contact blob to indexed per-row state on
``outbound_campaign_contacts``: hot columns (phone/name/attempt/answered_at/
duration_s/qualified/lead_score/claimed_*), a cold ``result`` JSONB, partial
indexes for the dialer claim + qualified pool, ``unique(campaign_id, phone)`` for
ON CONFLICT dedupe, and makes ``outgoing_lead_id`` nullable (bulk dial-targets are
not leads). Adds ``outbound_campaigns.contacts_dialed`` + the ``ingesting`` /
``ingest_failed`` campaign statuses, and makes the legacy ``contacts`` blob
nullable (no longer written). All additive/idempotent — the blob is retained for
rollback and dropped in a later migration.

Revision ID: campaign_contacts_scale_v1
Revises: subscription_cancel_v1
Create Date: 2026-07-04 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "campaign_contacts_scale_v1"
down_revision: Union[str, Sequence[str], None] = "subscription_cancel_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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


def _constraints(table: str) -> set[str]:
    ins = _insp()
    if table not in ins.get_table_names():
        return set()
    return {c["name"] for c in ins.get_unique_constraints(table)}


def upgrade() -> None:
    occ = "outbound_campaign_contacts"
    have = _cols(occ)
    add = lambda name, col: op.add_column(occ, sa.Column(name, col, nullable=True)) if name not in have else None
    add("phone", sa.String())
    add("name", sa.String())
    add("attempt", sa.SmallInteger())
    add("answered_at", sa.DateTime(timezone=True))
    add("duration_s", sa.Numeric(12, 4))
    add("qualified", sa.Boolean())
    add("lead_score", sa.Integer())
    add("claimed_by", sa.dialects.postgresql.UUID(as_uuid=True))
    add("claimed_at", sa.DateTime(timezone=True))
    add("result", sa.dialects.postgresql.JSONB())
    # Defaults / not-null tightening for the columns that need it.
    op.execute(f"UPDATE {occ} SET phone = COALESCE(phone, '') WHERE phone IS NULL")
    op.execute(f"UPDATE {occ} SET attempt = COALESCE(attempt, 0) WHERE attempt IS NULL")
    op.execute(f"UPDATE {occ} SET qualified = COALESCE(qualified, false) WHERE qualified IS NULL")
    op.execute(f"UPDATE {occ} SET result = COALESCE(result, '{{}}'::jsonb) WHERE result IS NULL")
    op.alter_column(occ, "phone", nullable=False, server_default="")
    op.alter_column(occ, "attempt", nullable=False, server_default="0")
    op.alter_column(occ, "qualified", nullable=False, server_default="false")
    op.alter_column(occ, "result", nullable=False, server_default="{}")
    # Bulk dial-targets have no lead row.
    op.alter_column(occ, "outgoing_lead_id", nullable=True)

    # Drop the old lead-pair unique (bulk rows have NULL lead_id); add phone dedupe.
    cons = _constraints(occ)
    if "uq_outbound_campaign_contact_lead" in cons:
        op.drop_constraint("uq_outbound_campaign_contact_lead", occ, type_="unique")
    if "uq_outbound_campaign_contact_phone" not in cons:
        op.create_unique_constraint("uq_outbound_campaign_contact_phone", occ, ["campaign_id", "phone"])

    idx = _indexes(occ)
    if "ix_occ_campaign_pending" not in idx:
        op.create_index("ix_occ_campaign_pending", occ, ["campaign_id"],
                        postgresql_where=sa.text("status = 'pending'"))
    if "ix_occ_campaign_qualified" not in idx:
        op.create_index("ix_occ_campaign_qualified", occ, ["campaign_id"],
                        postgresql_where=sa.text("qualified"))
    if "ix_occ_campaign_status" not in idx:
        op.create_index("ix_occ_campaign_status", occ, ["campaign_id", "status"])

    # Campaign: dialed counter + nullable legacy blob.
    camp = "outbound_campaigns"
    if "contacts_dialed" not in _cols(camp):
        op.add_column(camp, sa.Column("contacts_dialed", sa.Integer(), nullable=False, server_default="0"))
    op.alter_column(camp, "contacts", nullable=True)

    # New campaign statuses (enum ADD VALUE is idempotent w/ IF NOT EXISTS, PG 12+).
    for val in ("ingesting", "ingest_failed"):
        op.execute(f"ALTER TYPE campaignstatus ADD VALUE IF NOT EXISTS '{val}'")


def downgrade() -> None:
    occ = "outbound_campaign_contacts"
    for ix in ("ix_occ_campaign_pending", "ix_occ_campaign_qualified", "ix_occ_campaign_status"):
        if ix in _indexes(occ):
            op.drop_index(ix, table_name=occ)
    if "uq_outbound_campaign_contact_phone" in _constraints(occ):
        op.drop_constraint("uq_outbound_campaign_contact_phone", occ, type_="unique")
    have = _cols(occ)
    for name in ("phone", "name", "attempt", "answered_at", "duration_s", "qualified",
                 "lead_score", "claimed_by", "claimed_at", "result"):
        if name in have:
            op.drop_column(occ, name)
    if "contacts_dialed" in _cols("outbound_campaigns"):
        op.drop_column("outbound_campaigns", "contacts_dialed")
    # Enum values + outgoing_lead_id nullability are left in place (non-destructive).
