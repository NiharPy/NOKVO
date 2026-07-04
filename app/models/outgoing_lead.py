import enum
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class LeadSourceProvider(str, enum.Enum):
    meta_ads = "meta_ads"
    google_ads = "google_ads"
    google_forms = "google_forms"
    nokvo_form = "nokvo_form"


class LeadConnectionStatus(str, enum.Enum):
    connected = "connected"
    needs_reauth = "needs_reauth"
    error = "error"
    disabled = "disabled"


class LeadCaptureFormStatus(str, enum.Enum):
    active = "active"
    disabled = "disabled"
    archived = "archived"


class LeadConsentStatus(str, enum.Enum):
    granted = "granted"
    unknown = "unknown"
    revoked = "revoked"


class LeadCallStatus(str, enum.Enum):
    new = "new"
    queued = "queued"
    called = "called"
    opted_out = "opted_out"
    invalid = "invalid"


class LeadSourceConnection(Base):
    __tablename__ = "lead_source_connections"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, ForeignKey("tenant_resources.tenant_id"), nullable=False, index=True)
    provider = Column(SAEnum(LeadSourceProvider), nullable=False)
    status = Column(SAEnum(LeadConnectionStatus), nullable=False, default=LeadConnectionStatus.connected)
    display_name = Column(String, nullable=False)
    provider_account_id = Column(String, nullable=True)
    scopes = Column(JSONB, nullable=False, default=list)
    access_token_encrypted = Column(Text, nullable=True)
    refresh_token_encrypted = Column(Text, nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    metadata_ = Column("metadata", JSONB, nullable=False, default=dict)
    last_sync_at = Column(DateTime(timezone=True), nullable=True)
    last_error = Column(Text, nullable=True)
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("organization_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())


class LeadCaptureForm(Base):
    __tablename__ = "lead_capture_forms"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, ForeignKey("tenant_resources.tenant_id"), nullable=False, index=True)
    source_connection_id = Column(
        UUID(as_uuid=True), ForeignKey("lead_source_connections.id"), nullable=True, index=True
    )
    provider = Column(SAEnum(LeadSourceProvider), nullable=False, index=True)
    status = Column(
        SAEnum(LeadCaptureFormStatus),
        nullable=False,
        default=LeadCaptureFormStatus.active,
    )
    name = Column(String, nullable=False)
    provider_form_id = Column(String, nullable=True)
    provider_account_id = Column(String, nullable=True)
    public_slug = Column(String, nullable=True, unique=True)
    external_url = Column(String, nullable=True)
    field_schema = Column(JSONB, nullable=False, default=list)
    field_mapping = Column(JSONB, nullable=False, default=dict)
    consent_field_key = Column(String, nullable=True)
    consent_text = Column(Text, nullable=True)
    default_call_consent = Column(Boolean, nullable=False, default=False)
    metadata_ = Column("metadata", JSONB, nullable=False, default=dict)
    last_synced_at = Column(DateTime(timezone=True), nullable=True)
    created_by_user_id = Column(UUID(as_uuid=True), ForeignKey("organization_users.id"), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_form_id",
            name="uq_lead_capture_form_provider_form",
        ),
    )


class OutgoingLead(Base):
    __tablename__ = "outgoing_leads"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, ForeignKey("tenant_resources.tenant_id"), nullable=False, index=True)
    source_provider = Column(SAEnum(LeadSourceProvider), nullable=False, index=True)
    source_connection_id = Column(
        UUID(as_uuid=True), ForeignKey("lead_source_connections.id"), nullable=True, index=True
    )
    capture_form_id = Column(UUID(as_uuid=True), ForeignKey("lead_capture_forms.id"), nullable=True, index=True)
    provider_lead_id = Column(String, nullable=True)
    name = Column(String, nullable=True)
    email = Column(String, nullable=True)
    phone_raw = Column(String, nullable=True)
    phone_e164 = Column(String, nullable=True, index=True)
    fields = Column(JSONB, nullable=False, default=dict)
    source_metadata = Column(JSONB, nullable=False, default=dict)
    consent_status = Column(
        SAEnum(LeadConsentStatus),
        nullable=False,
        default=LeadConsentStatus.unknown,
        index=True,
    )
    consent_text = Column(Text, nullable=True)
    consent_field_key = Column(String, nullable=True)
    consent_value = Column(String, nullable=True)
    consented_at = Column(DateTime(timezone=True), nullable=True)
    submitted_at = Column(DateTime(timezone=True), nullable=True)
    opt_out_at = Column(DateTime(timezone=True), nullable=True)
    call_status = Column(SAEnum(LeadCallStatus), nullable=False, default=LeadCallStatus.new)
    # Post-call handoff note: a 3-sentence human-readable summary of the
    # last call written by the global gpt-5.4-mini condenser the moment
    # the call ends. Read by the follow-up agent's preamble so it opens
    # with awareness of the prior conversation, and surfaced in the
    # LeadsView so managers can scan call outcomes without opening
    # transcripts. Nullable — short or failed calls leave this empty and
    # the preamble falls back to structured-facts injection.
    handoff_note = Column(Text, nullable=True)
    handoff_note_generated_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "source_provider",
            "provider_lead_id",
            name="uq_outgoing_lead_provider_lead",
        ),
        Index("ix_outgoing_leads_tenant_consent", "tenant_id", "consent_status"),
    )


class OutboundCampaignContact(Base):
    """One dial-target row per campaign contact — the SCALABLE source of truth for
    bulk-campaign dialing state (replaces the O(n) JSONB blob on OutboundCampaign).

    Hot fields (queried/updated per call) are indexed COLUMNS so status webhooks
    and the dialer claim are O(1) single-row operations; cold post-call detail
    (score breakdown, call note) rides in ``result`` JSONB. ``outgoing_lead_id`` is
    NULL for bulk CSV dial-targets (a lead row is created only if they qualify)."""
    __tablename__ = "outbound_campaign_contacts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id = Column(UUID(as_uuid=True), ForeignKey("outbound_campaigns.id"), nullable=False, index=True)
    # NULL for bulk dial-targets; set only when the contact is (or becomes) a lead.
    outgoing_lead_id = Column(UUID(as_uuid=True), ForeignKey("outgoing_leads.id"), nullable=True, index=True)
    # pending | dialing | ringing | answered | completed | no_answer | failed | dnd_dropped
    status = Column(String, nullable=False, default="pending")
    phone = Column(String, nullable=False, default="")          # canonical E.164 (dedupe key)
    name = Column(String, nullable=True)
    attempt = Column(SmallInteger, nullable=False, server_default="0", default=0)
    call_id = Column(String, nullable=True)
    call_link_id = Column(String, nullable=False, unique=True)
    answered_at = Column(DateTime(timezone=True), nullable=True)
    duration_s = Column(Numeric(12, 4), nullable=True)
    qualified = Column(Boolean, nullable=False, server_default="false", default=False)
    lead_score = Column(Integer, nullable=True)
    # claim pool (APEX members): who has claimed this qualified lead, when.
    claimed_by = Column(UUID(as_uuid=True), nullable=True)
    claimed_at = Column(DateTime(timezone=True), nullable=True)
    # Cold post-call detail (score_breakdown, call_note, interest_reason, from_number…).
    result = Column(JSONB, nullable=False, default=dict, server_default="{}")
    snapshot = Column(JSONB, nullable=False, default=dict, server_default="{}")  # legacy/back-compat
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        # One row per phone per campaign — enables ON CONFLICT DO NOTHING dedupe at
        # ingest without an in-memory 1M set.
        UniqueConstraint("campaign_id", "phone", name="uq_outbound_campaign_contact_phone"),
        # The dialer claim: pending rows for a campaign. Partial → stays tiny as the
        # campaign drains, so it never bloats at 1M.
        Index(
            "ix_occ_campaign_pending",
            "campaign_id",
            postgresql_where=(status == "pending"),
        ),
        # Qualified page + claim pool.
        Index(
            "ix_occ_campaign_qualified",
            "campaign_id",
            postgresql_where=(qualified.is_(True)),
        ),
        # Live-call count (concurrency cap) + per-bucket summary.
        Index("ix_occ_campaign_status", "campaign_id", "status"),
    )
