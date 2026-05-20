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
# Re-chained after c3d4e5f6a7b8 (pending_tool_retries) so this migration
# joins the linear history. It was previously a sibling of b2d3e4f5c6d7
# off the same parent, producing two alembic heads and stranding these
# four tables (outgoing_leads, lead_capture_forms, etc.) on a branch the
# DB never advanced onto.
down_revision: Union[str, Sequence[str], None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Column-side enum references with ``create_type=False`` so they never
# re-issue ``CREATE TYPE`` — the explicit DO-block creation in ``upgrade``
# is the only place the types are actually defined, and it's idempotent
# against a prior partial run.
leadsourceprovider = postgresql.ENUM(
    "meta_ads", "google_ads", "google_forms", "nokvo_form",
    name="leadsourceprovider", create_type=False,
)
leadconnectionstatus = postgresql.ENUM(
    "connected", "needs_reauth", "error", "disabled",
    name="leadconnectionstatus", create_type=False,
)
leadcaptureformstatus = postgresql.ENUM(
    "active", "disabled", "archived",
    name="leadcaptureformstatus", create_type=False,
)
leadconsentstatus = postgresql.ENUM(
    "granted", "unknown", "revoked",
    name="leadconsentstatus", create_type=False,
)
leadcallstatus = postgresql.ENUM(
    "new", "queued", "called", "opted_out", "invalid",
    name="leadcallstatus", create_type=False,
)


_ENUM_DEFS: list[tuple[str, tuple[str, ...]]] = [
    ("leadsourceprovider", ("meta_ads", "google_ads", "google_forms", "nokvo_form")),
    ("leadconnectionstatus", ("connected", "needs_reauth", "error", "disabled")),
    ("leadcaptureformstatus", ("active", "disabled", "archived")),
    ("leadconsentstatus", ("granted", "unknown", "revoked")),
    ("leadcallstatus", ("new", "queued", "called", "opted_out", "invalid")),
]


def _create_enum_if_absent(name: str, values: tuple[str, ...]) -> None:
    """CREATE TYPE wrapped in DO/EXCEPTION so a prior partial run that
    leaked the type doesn't blow up this migration on retry."""
    values_sql = ", ".join(f"'{v}'" for v in values)
    op.execute(
        f"""
        DO $$ BEGIN
            CREATE TYPE {name} AS ENUM ({values_sql});
        EXCEPTION WHEN duplicate_object THEN NULL;
        END $$;
        """
    )


def upgrade() -> None:
    for enum_name, enum_values in _ENUM_DEFS:
        _create_enum_if_absent(enum_name, enum_values)

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
