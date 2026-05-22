"""Nokvo Connect — voice/text session minted via the public API.

One row per ``POST /api/voice/sessions`` call. Holds the transcript, usage
counters, and lifecycle timestamps. The WebSocket pipeline reads/writes the
``transcript`` and ``usage`` columns as the session progresses; on close we
also emit a ``TenantUsageEvent`` so the existing billing rollup picks the
session up without a separate path.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class ConnectSession(Base):
    __tablename__ = "connect_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    api_key_id = Column(UUID(as_uuid=True), ForeignKey("organization_api_keys.id"), nullable=False, index=True)
    channel = Column(String, nullable=False, default="voice")  # 'voice' | 'text'
    status = Column(String, nullable=False, default="created")  # created | live | completed | failed | expired
    language = Column(String, nullable=False, default="en")
    client_session_id = Column(String, nullable=True)
    origin = Column(String, nullable=True)
    transcript = Column(JSONB, nullable=False, default=list)
    usage = Column(JSONB, nullable=False, default=dict)
    metadata_ = Column("metadata", JSONB, nullable=False, default=dict)
    failure_reason = Column(String, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
