from sqlalchemy import Column, String, DateTime, ForeignKey, Boolean, Integer, Numeric
from sqlalchemy.dialects.postgresql import UUID, JSONB
from app.db.session import Base
from sqlalchemy.sql import func
import uuid

class TenantResources(Base):
    __tablename__ = "tenant_resources"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id"), nullable=False)
    tenant_id = Column(String, unique=True, nullable=False)
    
    azure_resource_group_name = Column(String)
    azure_region = Column(String)
    
    qdrant_collection_name = Column(String)
    qdrant_url_ref = Column(String)
    redis_namespace = Column(String)
    redis_host = Column(String)
    redis_port = Column(Integer)
    
    storage_account_name = Column(String)
    storage_container_name = Column(String)
    blob_prefix = Column(String)
    
    key_vault_name = Column(String)
    secret_refs = Column(JSONB, default=dict)
    
    twilio_status = Column(String)
    twilio_phone_number = Column(String, nullable=True)
    usage_minutes = Column(Integer, nullable=False, default=0)
    total_cost_usd = Column(Numeric(12, 2), nullable=False, default=0)
    
    provider_status = Column(JSONB, default=dict)
    provisioning_status = Column(String, nullable=False) # 'pending', 'success', 'partial', 'failed'
    provisioning_steps = Column(JSONB, default=list)
    cleanup_required = Column(Boolean, default=False)
    
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())
