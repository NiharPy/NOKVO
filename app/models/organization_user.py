from sqlalchemy import Boolean, Column, DateTime, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import INET, UUID
from sqlalchemy.sql import func

from app.db.session import Base

import uuid


class OrganizationUser(Base):
    __tablename__ = "organization_users"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_organization_users_org_email"),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    invited_by = Column(UUID(as_uuid=True), ForeignKey("organization_users.id"), nullable=True)
    email = Column(String, nullable=False)
    full_name = Column(String, nullable=True)
    role = Column(String, nullable=False, default="member")
    status = Column(String, nullable=False, default="invited")
    auth_provider = Column(String, nullable=False, default="google")
    password_hash = Column(String, nullable=True)
    mfa_required = Column(Boolean, nullable=False, default=True)
    totp_secret_encrypted = Column(String, nullable=True)
    totp_secret_encrypted_v2 = Column(String, nullable=True)
    email_verified = Column(Boolean, nullable=False, default=False)
    last_login_at = Column(DateTime(timezone=True), nullable=True)
    last_login_ip = Column(INET, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    @property
    def mfa_pending(self) -> bool:
        """True when the user has no TOTP secret yet (deferred-MFA onboarding)."""
        return not bool(self.totp_secret_encrypted_v2 or self.totp_secret_encrypted)
