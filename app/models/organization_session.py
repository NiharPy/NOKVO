from sqlalchemy import Column, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.sql import func

from app.db.session import Base

import uuid


class OrganizationSession(Base):
    __tablename__ = "organization_sessions"
    __table_args__ = (
        Index("ix_organization_sessions_refresh_active", "refresh_token_hash", "revoked_at", "expires_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_user_id = Column(UUID(as_uuid=True), ForeignKey("organization_users.id", ondelete="CASCADE"), nullable=False)
    refresh_token_hash = Column(String, nullable=False, index=True)
    ip_address = Column(INET, nullable=True)
    user_agent = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoke_reason = Column(String, nullable=True)
