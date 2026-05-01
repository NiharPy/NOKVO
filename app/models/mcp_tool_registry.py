import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class MCPToolRegistryEntry(Base):
    __tablename__ = "mcp_tool_registry_entries"
    __table_args__ = (
        UniqueConstraint(
            "organization_id",
            "integration_type",
            "provider",
            "tool_name",
            name="uq_mcp_tool_registry_org_integration_tool",
        ),
    )

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False)
    tenant_id = Column(String, nullable=False)
    integration_type = Column(String, nullable=False)
    provider = Column(String, nullable=False)
    tool_name = Column(String, nullable=False)
    title = Column(String, nullable=True)
    description = Column(String, nullable=True)
    tool_definition = Column(JSONB, nullable=False)
    status = Column(String, nullable=False, default="active")
    version = Column(Integer, nullable=False, default=1)
    draft_id = Column(String, nullable=True)
    approved_by = Column(UUID(as_uuid=True), nullable=True)
    approved_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
