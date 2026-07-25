"""NOKVO APEX access request — the public "request access" record.

APEX no longer has self-serve signup. A prospect submits this from the public site;
SuperAdmin reviews it and creates the actual account (which stamps ``converted_org_id``
and flips ``status`` to ``converted``). No Organization/user/token is created here.

``status`` walks new → contacted → converted (or → rejected). ``status_updated_at`` is
stamped on every transition so the operator sees how long a request has been sitting.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base

APEX_REQUEST_STATUSES = {"new", "contacted", "converted", "rejected"}
# Statuses that count as an OPEN request for the per-email dedupe of public submissions.
APEX_REQUEST_OPEN_STATUSES = {"new", "contacted"}


class ApexAccessRequest(Base):
    __tablename__ = "apex_access_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    company_name = Column(String, nullable=True)
    contact_name = Column(String, nullable=True)
    email = Column(String, nullable=False, index=True)
    phone = Column(String, nullable=True)
    # The plan the prospect asked for (advisory — SuperAdmin picks the final plan).
    requested_plan = Column(String, nullable=True)
    notes = Column(Text, nullable=True)
    status = Column(String, nullable=False, server_default="new")
    status_updated_at = Column(DateTime(timezone=True), nullable=True)
    # SET NULL so deleting the created org keeps the request for the audit trail.
    converted_org_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_apex_access_requests_status_created", "status", "created_at"),
    )
