import uuid
import enum

from sqlalchemy import Column, String, Integer, Text, DateTime, ForeignKey, Enum as SAEnum
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.sql import func

from app.db.session import Base


class CampaignStatus(str, enum.Enum):
    draft = "draft"
    running = "running"
    completed = "completed"
    failed = "failed"
    cancelled = "cancelled"


class OutboundCampaign(Base):
    __tablename__ = "outbound_campaigns"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(String, ForeignKey("tenant_resources.tenant_id"), nullable=False, index=True)
    name = Column(String, nullable=False)
    status = Column(SAEnum(CampaignStatus), nullable=False, default=CampaignStatus.draft)

    # Contacts: [{"phone": str, "name": str, "status": str, "call_id": str|None,
    #             "call_link_id": str, "duration_s": int|None, "answered_at": str|None}]
    contacts = Column(JSONB, nullable=False, default=list)

    # Reference document
    doc_blob_path = Column(String, nullable=True)   # Azure Blob path
    doc_text = Column(Text, nullable=True)           # Full extracted text

    # Proactive-agent configuration (separate from the KB context held
    # in ``doc_text``). Shape::
    #
    #   {
    #     "agent_prompt":   str,         # role / tone / drive instructions
    #     "objectives":     [str, ...],  # ordered list of questions to land
    #     "exit_conditions": [str, ...], # signals that the call is "done"
    #     "tone":           str | null,  # "warm" | "neutral" | …
    #   }
    #
    # Used by :func:`agent_outbound_context.load_outbound_context` to
    # compose the per-campaign system prompt. When the column is empty
    # the outbound agent falls back to the legacy ``campaign_goal``
    # one-liner so existing campaigns keep working.
    agent_config = Column(JSONB, nullable=False, default=dict, server_default="{}")

    # The Telnyx number used as caller ID for this campaign
    from_number = Column(String, nullable=True)

    # Aggregate counters (updated as calls complete)
    total_count = Column(Integer, nullable=False, default=0)
    answered_count = Column(Integer, nullable=False, default=0)
    failed_count = Column(Integer, nullable=False, default=0)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    started_at = Column(DateTime(timezone=True), nullable=True)
    completed_at = Column(DateTime(timezone=True), nullable=True)
