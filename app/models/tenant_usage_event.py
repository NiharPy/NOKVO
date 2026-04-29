from sqlalchemy import Column, String, DateTime, ForeignKey, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.db.session import Base
from sqlalchemy.sql import func
import uuid


class TenantUsageEvent(Base):
    __tablename__ = "tenant_usage_events"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    tenant_id = Column(String, nullable=False)
    event_type = Column(String, nullable=False)
    minutes = Column(Integer, nullable=False, default=0)
    stt_minutes = Column(Numeric(10, 2), nullable=False, default=0)
    telephony_minutes = Column(Numeric(10, 2), nullable=False, default=0)
    tts_characters = Column(Integer, nullable=False, default=0)
    llm_api_calls = Column(Integer, nullable=False, default=0)
    llm_input_tokens = Column(Integer, nullable=False, default=0)
    llm_output_tokens = Column(Integer, nullable=False, default=0)
    storage_gb = Column(Numeric(10, 3), nullable=False, default=0)
    infrastructure_units = Column(Integer, nullable=False, default=0)
    cost_usd = Column(Numeric(12, 4), nullable=False, default=0)
    metadata_ = Column("metadata", JSONB, default=dict)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
