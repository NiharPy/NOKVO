from sqlalchemy import Column, DateTime, ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID, INET
from app.db.session import Base
from sqlalchemy.sql import func
import uuid

class SuperAdminSession(Base):
    __tablename__ = "superadmin_sessions"
    __table_args__ = (
        Index("ix_superadmin_sessions_refresh_active", "refresh_token_hash", "revoked_at", "expires_at"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    superadmin_id = Column(UUID(as_uuid=True), ForeignKey("superadmin_users.id", ondelete="CASCADE"))
    refresh_token_hash = Column(String, nullable=False, index=True)
    ip_address = Column(INET)
    user_agent = Column(String)
    device_fingerprint = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))
    revoke_reason = Column(String)
