from sqlalchemy import Column, String, Boolean, Integer, DateTime, text
from sqlalchemy.dialects.postgresql import UUID, INET
from app.db.session import Base
from sqlalchemy.sql import func
import uuid

class SuperAdminUser(Base):
    __tablename__ = "superadmin_users"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email = Column(String, unique=True, nullable=False)
    password_hash = Column(String, nullable=False)
    full_name = Column(String)
    role = Column(String, nullable=False) # 'founder','engineering','support','billing','readonly'
    status = Column(String, nullable=False, default='pending') # 'pending','active','locked','disabled'
    mfa_required = Column(Boolean, nullable=False, default=True)
    totp_secret_encrypted = Column(String)
    webauthn_enabled = Column(Boolean, nullable=False, default=False)
    failed_login_count = Column(Integer, nullable=False, default=0)
    locked_until = Column(DateTime(timezone=True))
    last_login_at = Column(DateTime(timezone=True))
    last_login_ip = Column(INET)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
