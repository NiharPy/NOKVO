"""GIN index on outbound_campaigns.contacts for call_link_id containment lookup.

``get_by_call_link_id`` resolves legacy blob-only campaigns with a JSONB
containment query (``contacts @> '[{"call_link_id": …}]'``) instead of loading
every running/completed campaign into Python — this index makes that query
sub-linear. jsonb_path_ops (not the default jsonb_ops): smaller and it serves
``@>``, the only operator this query uses.

Revision ID: campaign_blob_gin_v1
Revises: apex_support_tickets_v1
Create Date: 2026-07-08 00:00:00.000000
"""
from typing import Sequence, Union

from alembic import op


revision: str = "campaign_blob_gin_v1"
down_revision: Union[str, Sequence[str], None] = "apex_support_tickets_v1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_outbound_campaigns_contacts_gin "
        "ON outbound_campaigns USING gin (contacts jsonb_path_ops)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_outbound_campaigns_contacts_gin")
