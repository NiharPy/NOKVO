"""Add outgoing lead source and consent tables.

Revision ID: b6d7e8f9a0b1
Revises: a1c2d3e4f5b6
Create Date: 2026-05-20 16:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "b6d7e8f9a0b1"
down_revision: Union[str, Sequence[str], None] = "a1c2d3e4f5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


leadsourceprovider = sa.Enum(
    "meta_ads",
    "google_ads",
    "google_forms",
    "nokvo_form",
    name="leadsourceprovider",
)
leadconnectionstatus = sa.Enum(
    "connected",
    "needs_reauth",
    "error",
    "disabled",
    name="leadconnectionstatus",
)
leadcaptureformstatus = sa.Enum(
    "active",
    "disabled",
    "archived",
    name="leadcaptureformstatus",
)
leadconsentstatus = sa.Enum(
    "granted",
    "unknown",
    "revoked",
    name="leadconsentstatus",
)
leadcallstatus = sa.Enum(
    "new",
    "queued",
    "called",
    "opted_out",
    "invalid",
    name="leadcallstatus",
)


def upgrade() -> None:
    bind = op.get_bind()
    leadsourceprovider.create(bind, checkfirst=True)
    leadconnectionstatus.create(bind, checkfirst=True)
    leadcaptureformstatus.create(bind, checkfirst=True)
    leadconsentstatus.create(bind, checkfirst=True)
    leadcallstatus.create(bind, checkfirst=True)

    op.create_table(
        "lead_source_connections",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant_resources.tenant_id"), nullable=False),
        sa.Column("provider", leadsourceprovider, nullable=False),
        sa.Column("status", leadconnectionstatus, nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("provider_account_id", sa.String(), nullable=True),
        sa.Column("scopes", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("access_token_encrypted", sa.Text(), nullable=True),
        sa.Column("refresh_token_encrypted", sa.Text(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("last_sync_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization_users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_lead_source_connections_tenant_id", "lead_source_connections", ["tenant_id"])

    op.create_table(
        "lead_capture_forms",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant_resources.tenant_id"), nullable=False),
        sa.Column("source_connection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("lead_source_connections.id"), nullable=True),
        sa.Column("provider", leadsourceprovider, nullable=False),
        sa.Column("status", leadcaptureformstatus, nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("provider_form_id", sa.String(), nullable=True),
        sa.Column("provider_account_id", sa.String(), nullable=True),
        sa.Column("public_slug", sa.String(), nullable=True, unique=True),
        sa.Column("external_url", sa.String(), nullable=True),
        sa.Column("field_schema", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("field_mapping", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("consent_field_key", sa.String(), nullable=True),
        sa.Column("consent_text", sa.Text(), nullable=True),
        sa.Column("default_call_consent", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by_user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("organization_users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "provider", "provider_form_id", name="uq_lead_capture_form_provider_form"),
    )
    op.create_index("ix_lead_capture_forms_tenant_id", "lead_capture_forms", ["tenant_id"])
    op.create_index("ix_lead_capture_forms_source_connection_id", "lead_capture_forms", ["source_connection_id"])
    op.create_index("ix_lead_capture_forms_provider", "lead_capture_forms", ["provider"])

    op.create_table(
        "outgoing_leads",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("tenant_id", sa.String(), sa.ForeignKey("tenant_resources.tenant_id"), nullable=False),
        sa.Column("source_provider", leadsourceprovider, nullable=False),
        sa.Column("source_connection_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("lead_source_connections.id"), nullable=True),
        sa.Column("capture_form_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("lead_capture_forms.id"), nullable=True),
        sa.Column("provider_lead_id", sa.String(), nullable=True),
        sa.Column("name", sa.String(), nullable=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("phone_raw", sa.String(), nullable=True),
        sa.Column("phone_e164", sa.String(), nullable=True),
        sa.Column("fields", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("source_metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("consent_status", leadconsentstatus, nullable=False),
        sa.Column("consent_text", sa.Text(), nullable=True),
        sa.Column("consent_field_key", sa.String(), nullable=True),
        sa.Column("consent_value", sa.String(), nullable=True),
        sa.Column("consented_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("opt_out_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("call_status", leadcallstatus, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("tenant_id", "source_provider", "provider_lead_id", name="uq_outgoing_lead_provider_lead"),
    )
    op.create_index("ix_outgoing_leads_tenant_id", "outgoing_leads", ["tenant_id"])
    op.create_index("ix_outgoing_leads_source_provider", "outgoing_leads", ["source_provider"])
    op.create_index("ix_outgoing_leads_source_connection_id", "outgoing_leads", ["source_connection_id"])
    op.create_index("ix_outgoing_leads_capture_form_id", "outgoing_leads", ["capture_form_id"])
    op.create_index("ix_outgoing_leads_phone_e164", "outgoing_leads", ["phone_e164"])
    op.create_index("ix_outgoing_leads_consent_status", "outgoing_leads", ["consent_status"])
    op.create_index("ix_outgoing_leads_tenant_consent", "outgoing_leads", ["tenant_id", "consent_status"])

    op.create_table(
        "outbound_campaign_contacts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("campaign_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("outbound_campaigns.id"), nullable=False),
        sa.Column("outgoing_lead_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("outgoing_leads.id"), nullable=False),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("call_id", sa.String(), nullable=True),
        sa.Column("call_link_id", sa.String(), nullable=False, unique=True),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("campaign_id", "outgoing_lead_id", name="uq_outbound_campaign_contact_lead"),
    )
    op.create_index("ix_outbound_campaign_contacts_campaign_id", "outbound_campaign_contacts", ["campaign_id"])
    op.create_index("ix_outbound_campaign_contacts_outgoing_lead_id", "outbound_campaign_contacts", ["outgoing_lead_id"])


def downgrade() -> None:
    op.drop_index("ix_outbound_campaign_contacts_outgoing_lead_id", "outbound_campaign_contacts")
    op.drop_index("ix_outbound_campaign_contacts_campaign_id", "outbound_campaign_contacts")
    op.drop_table("outbound_campaign_contacts")

    op.drop_index("ix_outgoing_leads_tenant_consent", "outgoing_leads")
    op.drop_index("ix_outgoing_leads_consent_status", "outgoing_leads")
    op.drop_index("ix_outgoing_leads_phone_e164", "outgoing_leads")
    op.drop_index("ix_outgoing_leads_capture_form_id", "outgoing_leads")
    op.drop_index("ix_outgoing_leads_source_connection_id", "outgoing_leads")
    op.drop_index("ix_outgoing_leads_source_provider", "outgoing_leads")
    op.drop_index("ix_outgoing_leads_tenant_id", "outgoing_leads")
    op.drop_table("outgoing_leads")

    op.drop_index("ix_lead_capture_forms_provider", "lead_capture_forms")
    op.drop_index("ix_lead_capture_forms_source_connection_id", "lead_capture_forms")
    op.drop_index("ix_lead_capture_forms_tenant_id", "lead_capture_forms")
    op.drop_table("lead_capture_forms")

    op.drop_index("ix_lead_source_connections_tenant_id", "lead_source_connections")
    op.drop_table("lead_source_connections")

    bind = op.get_bind()
    leadcallstatus.drop(bind, checkfirst=True)
    leadconsentstatus.drop(bind, checkfirst=True)
    leadcaptureformstatus.drop(bind, checkfirst=True)
    leadconnectionstatus.drop(bind, checkfirst=True)
    leadsourceprovider.drop(bind, checkfirst=True)
