"""Customer base — every inbound caller, deduplicated per tenant.

One row per (tenant_id, phone_e164). Upserted at call END by
``customer_base_service.upsert_customer_from_call`` (call end, not start:
the caller's name from conversational memory only exists at teardown, and
teardown runs exactly once per call). Populated for ALL tenants; the
Customer base tab / manual follow-up UI is exposed for clinics only.

``name`` is fill-only from call data (COALESCE — STT mishears names, so a
later call must never overwrite a name already on file). The admin PATCH
endpoint is the only overwrite path.

``last_call_summary`` mirrors ``OutgoingLead.handoff_note``: the post-call
3-sentence condenser summary, quoted into the outbound prompt when the admin
commands a follow-up so the agent opens aware of the prior conversation.
"""
import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base


class CustomerBase(Base):
    __tablename__ = "customer_base"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, ForeignKey("tenant_resources.tenant_id"), nullable=False, index=True)

    phone_e164 = Column(String, nullable=False)
    name = Column(String, nullable=True)

    first_seen_at = Column(DateTime(timezone=True), nullable=True)
    last_call_at = Column(DateTime(timezone=True), nullable=True)
    call_count = Column(Integer, nullable=False, default=0)

    # The most recent call's link_id + condenser summary.
    last_call_id = Column(String, nullable=True)
    last_call_summary = Column(Text, nullable=True)

    # Customer asked not to be called — blocks manual follow-up dispatch.
    opt_out = Column(Boolean, nullable=False, default=False)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("tenant_id", "phone_e164", name="uq_customer_base_tenant_phone"),
        # List view: WHERE tenant_id=... ORDER BY last_call_at DESC
        Index("ix_customer_base_tenant_lastcall", "tenant_id", "last_call_at"),
    )
