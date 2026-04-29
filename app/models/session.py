from sqlalchemy import Column, String, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID, INET
from app.db.session import Base
from sqlalchemy.sql import func
import uuid

class SuperAdminSession(Base):
    __tablename__ = "superadmin_sessions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    superadmin_id = Column(UUID(as_uuid=True), ForeignKey("superadmin_users.id", ondelete="CASCADE"))
    refresh_token_hash = Column(String, nullable=False)
    ip_address = Column(INET)
    user_agent = Column(String)
    device_fingerprint = Column(String)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    expires_at = Column(DateTime(timezone=True), nullable=False)
    revoked_at = Column(DateTime(timezone=True))
    revoke_reason = Column(String)
