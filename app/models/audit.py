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


class VoiceDataAccessAuditLog(Base):
    __tablename__ = "voice_data_access_audit_logs"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    tenant_id = Column(String, nullable=True, index=True)
    actor_type = Column(String, nullable=False)  # organization_user | api_key | system | superadmin
    actor_id = Column(String, nullable=True)
    access_type = Column(String, nullable=False)  # read | write | export | delete | summarize
    resource_type = Column(String, nullable=False)  # transcript | call_log | session_history | recording
    resource_id = Column(String, nullable=True)
    call_id = Column(String, nullable=True, index=True)
    reason = Column(String, nullable=True)
    ip_address = Column(INET)
    user_agent = Column(String)
    request_id = Column(String)
    metadata_ = Column("metadata", JSONB)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
