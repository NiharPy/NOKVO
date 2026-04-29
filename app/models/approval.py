from sqlalchemy import Column, String, DateTime, ForeignKey, text
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.db.session import Base
from sqlalchemy.sql import func
import uuid

class SuperAdminApprovalRequest(Base):
    __tablename__ = "superadmin_approval_requests"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    requested_by = Column(UUID(as_uuid=True), ForeignKey("superadmin_users.id"))
    approved_by = Column(UUID(as_uuid=True), ForeignKey("superadmin_users.id"))
    action = Column(String, nullable=False)
    target_type = Column(String)
    target_id = Column(String)
    payload = Column(JSONB, nullable=False)
    status = Column(String, nullable=False, default='pending') # 'pending','approved','rejected','expired','executed'
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    decided_at = Column(DateTime(timezone=True))
    executed_at = Column(DateTime(timezone=True))
