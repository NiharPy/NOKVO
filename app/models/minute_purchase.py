"""Prepaid voice-minute purchases.

One row per bundle a customer buys — at onboarding (alongside the platform-fee
subscription) and on every later top-up. The org's **balance** is
``SUM(minute_purchases.minutes) − SUM(CallCost.billed_minutes consumed)``; calls
are gated on a positive balance. Each purchase records the flat bracket rate it
was priced at (see :mod:`app.services.minute_pricing`) for audit.

``razorpay_payment_id`` is UNIQUE so the verify + webhook convergence point (and
top-up verify) can insert idempotently — a replayed payment can never double-credit
minutes.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class MinutePurchase(Base):
    __tablename__ = "minute_purchases"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    minutes = Column(Integer, nullable=False)
    # Ledger rupee value (4 dp) + the flat bracket rate it was priced at.
    rupees = Column(Numeric(14, 4), nullable=False)
    rate_per_minute = Column(Numeric(6, 2), nullable=False)
    # "onboarding" (first bundle, via the subscription addon) | "topup" (Order).
    source = Column(String, nullable=False, server_default="topup")
    # The idempotency anchor — verify + webhook both key off the payment id.
    razorpay_payment_id = Column(String, nullable=True, unique=True, index=True)
    # The subscription_id (onboarding) or order_id (top-up) this purchase came from.
    razorpay_ref = Column(String, nullable=True)
    raw_event = Column(JSONB, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("razorpay_payment_id", name="uq_minute_purchases_rzp_payment_id"),
        Index("ix_minute_purchases_org_created", "organization_id", "created_at"),
    )
