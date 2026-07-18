"""CRM result-webhook outbox — durable at-least-once delivery of call verdicts.

Every terminal contact outcome on a CRM-fed campaign (qualified / not
qualified / unreachable / DND-blocked) is enqueued here and POSTed to the
campaign's configured Result Webhook URL by ``crm_webhook_drainer`` with
HMAC signing and backoff. A row is the delivery attempt state, not the
verdict itself — the verdict lives on the contact row; ``payload`` is the
frozen body we send so retries are byte-identical (stable signatures).
"""
import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class CrmWebhookOutbox(Base):
    __tablename__ = "crm_webhook_outbox"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    campaign_id = Column(UUID(as_uuid=True), ForeignKey("outbound_campaigns.id"), nullable=False, index=True)
    call_link_id = Column(String, nullable=True)
    event = Column(String, nullable=False)  # lead.completed | lead.unreachable | lead.dnd_blocked | test
    payload = Column(JSONB, nullable=False, default=dict, server_default="{}")
    status = Column(String, nullable=False, default="pending")  # pending | delivered | dead
    attempt = Column(SmallInteger, nullable=False, default=0, server_default="0")
    next_attempt_at = Column(DateTime(timezone=True), nullable=False)
    last_status_code = Column(Integer, nullable=True)
    last_error = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    delivered_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        # The drainer's due-scan: pending rows ordered by next_attempt_at.
        # Partial → stays tiny however large the delivered history grows.
        Index(
            "ix_crm_webhook_outbox_due",
            "next_attempt_at",
            postgresql_where=(status == "pending"),
        ),
    )
