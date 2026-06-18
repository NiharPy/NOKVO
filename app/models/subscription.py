"""Razorpay monthly subscription per organization.

One row per org's plan subscription. Created (``status=created``) when the
signup flow opens Razorpay Checkout, flipped to ``active`` + ``provisioned_at``
set once payment is confirmed (via the checkout verify call OR the webhook —
whichever arrives first). ``razorpay_subscription_id`` is UNIQUE so the
verify/webhook convergence point can look the row up idempotently and provision
resources exactly once.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Filled once provisioning runs (a tenant doesn't exist until payment).
    tenant_id = Column(String, nullable=True, index=True)

    # "inbound_only" | "inbound_outbound" — drives Organization.calling_enabled.
    plan = Column(String, nullable=False)
    amount_paise = Column(Integer, nullable=False)
    currency = Column(String, nullable=False, server_default="INR")

    razorpay_plan_id = Column(String, nullable=True)
    # The idempotency anchor: verify + webhook both key off this.
    razorpay_subscription_id = Column(String, nullable=False, unique=True, index=True)
    razorpay_payment_id = Column(String, nullable=True)

    # "created" | "active" | "halted" | "cancelled".
    status = Column(String, nullable=False, server_default="created")
    # Set when resources are provisioned (NULL = paid-but-not-yet-provisioned,
    # which the resume/webhook-retry path re-attempts).
    provisioned_at = Column(DateTime(timezone=True), nullable=True)
    current_period_end = Column(DateTime(timezone=True), nullable=True)

    # Last raw Razorpay event/payload, for audit + debugging.
    raw_event = Column(JSONB, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("razorpay_subscription_id", name="uq_subscriptions_rzp_sub_id"),
        Index("ix_subscriptions_org_status", "organization_id", "status"),
    )
