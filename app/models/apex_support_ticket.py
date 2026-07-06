"""APEX support ticket — raised in-product via Nova (the chat assistant).

One row per ticket. Nova attaches a ``diagnosis`` snapshot (the tenant's recent
calls / campaign states / wallet at raise time) so the operator sees what was
wrong WITHOUT asking the user to reproduce it. ``status`` walks
open → in_progress → resolved. The user is notified in-app on creation; the team
is notified by email to the support address.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base

APEX_TICKET_STATUSES = {"open", "in_progress", "resolved"}


class ApexSupportTicket(Base):
    __tablename__ = "apex_support_tickets"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id = Column(String, nullable=True, index=True)
    # SET NULL so removing the member keeps the ticket for the org's audit trail.
    requested_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    requested_by_email = Column(String, nullable=True)  # denormalized for the operator view

    subject = Column(String(300), nullable=False)
    description = Column(Text, nullable=False)
    # Full-detail diagnosis snapshot (trace_ids, dial errors, wallet) — the
    # operator's copy; the LLM only ever saw the compact redacted view.
    diagnosis = Column(JSONB, nullable=True)

    status = Column(String, nullable=False, server_default="open")  # open|in_progress|resolved
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)
    resolution_note = Column(Text, nullable=True)

    __table_args__ = (
        Index("ix_apex_support_tickets_status_created", "status", "created_at"),
    )
