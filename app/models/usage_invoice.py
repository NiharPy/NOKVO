"""Recurring usage-invoice ledger.

One row per billing cycle per organization. The mailer anchors each tenant's
next cycle to the ``period_end`` of their most recent row (and the very first
cycle to the day they first paid, i.e. ``Subscription.provisioned_at``), so a
``(organization_id, period_start)`` row is the idempotency key: a cycle that
already has a row is never billed or emailed twice.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base


class UsageInvoice(Base):
    __tablename__ = "usage_invoices"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id = Column(String, nullable=True, index=True)

    # The billed window: [period_start, period_end).
    period_start = Column(DateTime(timezone=True), nullable=False)
    period_end = Column(DateTime(timezone=True), nullable=False)

    # Usage billed for this window (summed from the CallCost ledger).
    minutes = Column(Integer, nullable=False, server_default="0")
    call_count = Column(Integer, nullable=False, server_default="0")
    amount_inr = Column(Numeric(14, 4), nullable=False, server_default="0")

    # "sent" | "skipped_zero" (no usage → no email) | "failed".
    status = Column(String, nullable=False, server_default="sent")
    email_to = Column(String, nullable=True)
    error = Column(String, nullable=True)
    sent_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("organization_id", "period_start", name="uq_usage_invoices_org_period"),
        Index("ix_usage_invoices_org_period_end", "organization_id", "period_end"),
    )
