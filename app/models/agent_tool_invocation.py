from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class AgentToolInvocation(Base):
    """
    Idempotency + audit log for every Nokvo One agent tool call.

    A row is written on each invocation. idempotency_key is computed as
    sha256(session_id|tool_key|canonical_args). When a second call with the
    same key lands inside the 60s window, we short-circuit and return the
    prior result instead of re-running the handler.
    """

    __tablename__ = "agent_tool_invocations"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    agent_id = Column(
        UUID(as_uuid=True),
        ForeignKey("nokvo_one_agents.id", ondelete="SET NULL"),
        nullable=True,
    )
    session_id = Column(String, nullable=True)
    tool_key = Column(String, nullable=False)
    idempotency_key = Column(String, nullable=False)
    args = Column(JSONB, nullable=False, server_default="{}")
    result = Column(JSONB, nullable=True)
    result_record_id = Column(
        UUID(as_uuid=True),
        ForeignKey("nokvo_one_tool_records.id", ondelete="SET NULL"),
        nullable=True,
    )
    status = Column(String, nullable=False, server_default="ok")
    error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
