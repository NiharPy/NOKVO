from sqlalchemy import Column, String, DateTime, Boolean, ForeignKey
from sqlalchemy.dialects.postgresql import UUID
from app.db.session import Base
from sqlalchemy.sql import func
import uuid

class Organization(Base):
    __tablename__ = "organizations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String, nullable=False)
    admin_email = Column(String, nullable=True)
    admin_name = Column(String, nullable=True)
    email_domain = Column(String, nullable=True)
    region = Column(String, nullable=False)
    environment = Column(String, nullable=False)
    call_type = Column(String, nullable=True)
    language = Column(String, nullable=True)
    plan_type = Column(String, nullable=True)
    product_tier = Column(String, nullable=False, server_default="nokvo_prime", default="nokvo_prime")
    status = Column(String, nullable=False, server_default="active", default="active")
    # Gates OUTBOUND (campaigns). Set from the plan: inbound_only → False,
    # inbound_outbound → True. Both plans answer INBOUND, so there's no separate
    # inbound direction gate — inbound is gated only on the prepaid-minute balance.
    calling_enabled = Column(Boolean, nullable=False, server_default="false", default=False)
    stores_pii = Column(Boolean, nullable=False, default=True)
    record_calls = Column(Boolean, nullable=False, default=True)
    create_resource_group = Column(Boolean, nullable=False, default=True)
    twilio_auto_provision = Column(Boolean, nullable=False, default=False)
    industry = Column(String, nullable=True)
    country_code = Column(String, nullable=True)

    # ── Post-payment onboarding wizard (business KYC → compliance → … → ToS) ──
    # Business / legal details collected in onboarding step ①.
    legal_name = Column(String, nullable=True)
    alias_name = Column(String, nullable=True)        # trading / brand name
    business_pan = Column(String, nullable=True)
    cin = Column(String, nullable=True)
    # Legal consent captured in the final onboarding step.
    terms_accepted_at = Column(DateTime(timezone=True), nullable=True)
    privacy_accepted_at = Column(DateTime(timezone=True), nullable=True)
    terms_version = Column(String, nullable=True)
    # Current wizard step for resume; null once onboarding is done. Values:
    # business_details → documents → working_hours → projects → agent → terms → done.
    onboarding_step = Column(String, nullable=True)

    # ── Affiliate attribution (APEX referral program) ──
    # Stamped at create-subscription when a valid affiliate number is entered;
    # commission accrual reads THIS (never the Razorpay notes). SET NULL so a
    # deleted affiliate never cascades into customer orgs; the code string
    # below survives as the audit trail.
    affiliate_id = Column(
        UUID(as_uuid=True),
        ForeignKey("affiliates.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    affiliate_code_used = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
