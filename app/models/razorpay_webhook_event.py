"""Razorpay webhook audit — durable record of every inbound webhook.

Payment webhooks drive money-affecting side effects (crediting the wallet, flipping an
org to ``pending_activation``). If one fails to process — a transient DB error, a bug, a
Razorpay retry storm — we must be able to SEE it and REPLAY it rather than silently drop a
paid cycle. Every verified webhook is persisted here; a SuperAdmin "Resync" action re-runs
the handler for a ``failed``/``received`` row. Idempotency lives downstream (per-cycle
``minute_purchases`` unique index), so a replay is always safe.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base

RAZORPAY_EVENT_STATUSES = {"received", "processed", "failed"}


class RazorpayWebhookEvent(Base):
    __tablename__ = "razorpay_webhook_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # Razorpay's ``x-razorpay-event-id`` header (unique per event) — for dedupe/lookup.
    event_id = Column(String, nullable=True, index=True)
    event_type = Column(String, nullable=True)          # e.g. subscription.charged
    subscription_id = Column(String, nullable=True, index=True)
    payment_id = Column(String, nullable=True)
    invoice_id = Column(String, nullable=True)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    status = Column(String, nullable=False, server_default="received")
    error = Column(Text, nullable=True)
    payload = Column(JSONB, nullable=False, server_default="{}")
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    processed_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_razorpay_webhook_events_status_created", "status", "created_at"),
    )
