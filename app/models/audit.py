from sqlalchemy import Column, String, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID, INET, JSONB
from app.db.session import Base
from sqlalchemy.sql import func
import uuid

class SuperAdminAuditLog(Base):
    __tablename__ = "superadmin_audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    superadmin_id = Column(UUID(as_uuid=True), ForeignKey("superadmin_users.id"))
    action = Column(String, nullable=False)
    risk_level = Column(String, nullable=False) # 'low','medium','high','critical'
    target_type = Column(String)
    target_id = Column(String)
    ip_address = Column(INET)
    user_agent = Column(String)
    request_id = Column(String)
    before_state = Column(JSONB)
    after_state = Column(JSONB)
    metadata_ = Column("metadata", JSONB) # 'metadata' is reserved in SQLAlchemy
    created_at = Column(DateTime(timezone=True), server_default=func.now())
