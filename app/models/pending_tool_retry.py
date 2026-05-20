from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class PendingToolRetry(Base):
    """A tool execution that failed in-call and needs to be retried out-of-band.

    The voice pipeline already attempts one inline retry before falling back to
    "the team will call you back." Persistent failures land here so a worker
    (or the operator dashboard) can re-drive them once the underlying issue —
    transient DB outage, third-party API blip — clears up. The caller's data
    is *not* lost; it's queued with full context.
    """

    __tablename__ = "pending_tool_retries"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tool_key = Column(String, nullable=False, index=True)
    arguments = Column(JSONB, nullable=False, server_default="{}")
    context = Column(JSONB, nullable=False, server_default="{}")
    status = Column(String, nullable=False, server_default="pending", index=True)
    attempts = Column(Integer, nullable=False, server_default="0")
    last_error = Column(Text, nullable=True)
    next_attempt_at = Column(DateTime(timezone=True), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    resolved_at = Column(DateTime(timezone=True), nullable=True)
