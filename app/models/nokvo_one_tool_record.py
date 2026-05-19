from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class NokvoOneToolRecord(Base):
    """
    Generic store for Nokvo One predefined-tool side-effects.

    record_type discriminates: 'lead', 'call_log', 'ticket', 'callback', 'email_draft'.
    All tool writes go here; reads aggregate by record_type + organization_id.
    """

    __tablename__ = "nokvo_one_tool_records"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by_user_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organization_users.id"),
        nullable=True,
    )
    record_type = Column(String, nullable=False, index=True)
    status = Column(String, nullable=False, server_default="open")
    data = Column(JSONB, nullable=False, server_default="{}")
    contact_phone = Column(String, nullable=True)
    contact_email = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
