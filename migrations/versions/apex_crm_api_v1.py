"""APEX CRM API v1 — per-campaign keys, external lead identity, webhook outbox.

Three additive pieces for the CRM-in / CRM-out surface:

* ``organization_api_keys.campaign_id`` — NULL for the existing org-wide Nokvo
  Connect keys; set for APEX campaign-scoped ``nkap_…`` keys (the key IS the
  campaign: every ``/apex/v1/*`` call resolves its campaign from the key).
* ``outbound_campaign_contacts.external_id`` — the CRM's own record id, echoed
  back on result webhooks so the CRM can map the verdict to its record. Lookup
  only (phone stays the dedupe key), hence a plain index, not unique.
* ``crm_webhook_outbox`` — durable at-least-once delivery queue for result
  webhooks (lead.completed / lead.unreachable / lead.dnd_blocked). Drained by
  ``crm_webhook_drainer`` with backoff; partial index keeps the due-scan tiny.

Revision ID: apex_crm_api_v1
Revises: affiliate_program_v1
Create Date: 2026-07-18 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision: str = "apex_crm_api_v1"
down_revision: Union[str, Sequence[str], None] = "affiliate_program_v1"
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


def upgrade() -> None:
    keys = "organization_api_keys"
    if "campaign_id" not in _cols(keys):
        op.add_column(
            keys,
            sa.Column(
                "campaign_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("outbound_campaigns.id"),
                nullable=True,
            ),
        )
    if "ix_organization_api_keys_campaign_id" not in _indexes(keys):
        op.create_index("ix_organization_api_keys_campaign_id", keys, ["campaign_id"])

    occ = "outbound_campaign_contacts"
    if "external_id" not in _cols(occ):
        op.add_column(occ, sa.Column("external_id", sa.Text(), nullable=True))
    if "ix_occ_campaign_external_id" not in _indexes(occ):
        op.create_index("ix_occ_campaign_external_id", occ, ["campaign_id", "external_id"])

    if "crm_webhook_outbox" not in _insp().get_table_names():
        op.create_table(
            "crm_webhook_outbox",
            sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
            sa.Column(
                "campaign_id",
                postgresql.UUID(as_uuid=True),
                sa.ForeignKey("outbound_campaigns.id"),
                nullable=False,
                index=True,
            ),
            sa.Column("call_link_id", sa.String(), nullable=True),
            sa.Column("event", sa.String(), nullable=False),
            sa.Column("payload", postgresql.JSONB(), nullable=False, server_default="{}"),
            # pending | delivered | dead
            sa.Column("status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_status_code", sa.Integer(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index(
            "ix_crm_webhook_outbox_due",
            "crm_webhook_outbox",
            ["next_attempt_at"],
            postgresql_where=sa.text("status = 'pending'"),
        )


def downgrade() -> None:
    if "crm_webhook_outbox" in _insp().get_table_names():
        op.drop_table("crm_webhook_outbox")
    occ = "outbound_campaign_contacts"
    if "ix_occ_campaign_external_id" in _indexes(occ):
        op.drop_index("ix_occ_campaign_external_id", table_name=occ)
    if "external_id" in _cols(occ):
        op.drop_column(occ, "external_id")
    keys = "organization_api_keys"
    if "ix_organization_api_keys_campaign_id" in _indexes(keys):
        op.drop_index("ix_organization_api_keys_campaign_id", table_name=keys)
    if "campaign_id" in _cols(keys):
        op.drop_column(keys, "campaign_id")
