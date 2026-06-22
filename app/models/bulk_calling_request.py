"""Tenant request for the Bulk CSV Calling feature.

Bulk CSV calling (upload a CSV of phone numbers → the outbound agent dials the
whole list) is an add-on that's only offered on the **Inbound + Outbound** plan
and is NOT self-serve: it needs a dedicated Plivo number/credentials that an
operator provisions by hand. So a tenant *requests* access — leaving a contact
number the SuperAdmin can reach them on — and the SuperAdmin grants it from the
console (entering the dedicated Plivo auth + DID), which flips the feature on.

One row per request. ``status`` walks pending → approved | denied. The granted
telephony itself lives on ``TenantResources.provider_status["bulk_calling"]``
(so the dialer can read it), not here — this table is the request/audit trail.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base

BULK_CALLING_REQUEST_STATUSES = {"pending", "approved", "denied"}


class BulkCallingRequest(Base):
    __tablename__ = "bulk_calling_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized so the SuperAdmin grant can write the telephony onto the
    # tenant's resources without re-resolving the org→tenant link.
    tenant_id = Column(String, nullable=True, index=True)
    # Who asked. SET NULL (not CASCADE) so removing a member doesn't erase the
    # request — the org link is what matters for triage.
    requested_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # The number the SuperAdmin should call to reach the requester.
    contact_number = Column(String, nullable=False)
    note = Column(Text, nullable=True)
    status = Column(String, nullable=False, server_default="pending")  # pending|approved|denied

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    reviewed_by_superadmin_id = Column(
        UUID(as_uuid=True),
        ForeignKey("superadmin_users.id", ondelete="SET NULL"),
        nullable=True,
    )

    __table_args__ = (
        Index("ix_bulk_calling_requests_status_created", "status", "created_at"),
    )
