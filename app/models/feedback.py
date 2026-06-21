"""Tenant-submitted feedback / feature requests.

One row per submission from a tenant admin/member via the in-product
"Feedback / Suggest a feature" button. Surfaced read-only in the SuperAdmin
console's Feedback tab. Deliberately minimal — free-text plus a coarse category.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base

FEEDBACK_CATEGORIES = {"feedback", "feature"}


class TenantFeedback(Base):
    __tablename__ = "tenant_feedback"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Who submitted it. SET NULL (not CASCADE) so removing a member doesn't erase
    # their feedback — the org link is what matters for triage.
    submitted_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id", ondelete="SET NULL"),
        nullable=True,
    )
    category = Column(String, nullable=False, server_default="feedback")  # 'feedback' | 'feature'
    message = Column(Text, nullable=False)
    status = Column(String, nullable=False, server_default="new")  # 'new' | 'reviewed'
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_tenant_feedback_created_at", "created_at"),
    )
