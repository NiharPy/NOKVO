"""Nokvo Connect — public API key per organization.

The raw key (returned exactly once at creation) carries the `nk_live_` /
`nk_test_` prefix plus a high-entropy secret. We persist:

* ``key_prefix`` — the first 12 chars, indexable, used to narrow lookup so we
  argon2-verify against a single candidate row.
* ``secret_hash`` — argon2id over the raw secret half. Never logged.

Everything else is operator-facing knobs the dashboard exposes: scopes the key
is allowed to call, CORS origin allowlist, RPM/concurrency limits, optional
webhook URL for lifecycle events, and audit metadata.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class OrganizationApiKey(Base):
    __tablename__ = "organization_api_keys"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False, index=True)
    # NULL → org-wide Nokvo Connect key. Set → APEX campaign-scoped ``nkap_…``
    # key: every /apex/v1/* request resolves its one campaign from the key, so
    # no endpoint carries a campaign id (and a leaked key exposes one campaign).
    campaign_id = Column(UUID(as_uuid=True), ForeignKey("outbound_campaigns.id"), nullable=True, index=True)
    label = Column(String, nullable=False)
    key_prefix = Column(String(16), nullable=False, unique=True, index=True)
    secret_hash = Column(String, nullable=False)
    mode = Column(String, nullable=False, default="live")  # 'live' | 'test'
    scopes = Column(JSONB, nullable=False, default=list)
    allowed_origins = Column(JSONB, nullable=False, default=list)
    rate_limit_rpm = Column(Integer, nullable=False, default=60)
    max_concurrent_sessions = Column(Integer, nullable=False, default=5)
    webhook_url = Column(String, nullable=True)
    webhook_secret_hash = Column(String, nullable=True)
    status = Column(String, nullable=False, default="active")  # 'active' | 'revoked'
    created_by_user_id = Column(UUID(as_uuid=True), nullable=True)
    last_used_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    revoke_reason = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
