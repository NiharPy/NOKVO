"""Durable per-call transcript — powers the Transcripts page + downloads.

One row per voice call (keyed by the pipeline's ``call_id``), written best-effort
at teardown from the session's retained turns. Subject to a **rolling 1-month
retention purge** (``TranscriptRetentionService``): the row is deleted roughly a
month after the call, independent of the ``CallCost`` billing ledger (which is the
authoritative, never-purged call list the Transcripts page joins against).
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
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class CallTranscript(Base):
    __tablename__ = "call_transcripts"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id = Column(
        String,
        ForeignKey("tenant_resources.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The voice pipeline's call_id — unique so the teardown writer is idempotent
    # and aligns 1:1 with the CallCost row of the same call_id.
    call_id = Column(String, nullable=False, unique=True, index=True)
    kind = Column(String, nullable=False, server_default="inbound")  # inbound|outbound|tester
    caller_phone = Column(String, nullable=True)
    # Retained conversation turns: [{"role": "...", "content": "..."}].
    turns = Column(JSONB, nullable=False, default=list)
    turn_count = Column(Integer, nullable=False, server_default="0")
    duration_seconds = Column(Numeric(12, 4), nullable=False, server_default="0")
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        UniqueConstraint("call_id", name="uq_call_transcripts_call_id"),
        # List query ("transcripts for org X, newest first") + the retention
        # purge both scan by (organization_id, started_at).
        Index("ix_call_transcripts_org_started_at", "organization_id", "started_at"),
    )
