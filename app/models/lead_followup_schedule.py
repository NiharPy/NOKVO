"""Follow-up schedule table — drives the recall-after-condition loop.

A row is the contract: "this lead is scheduled to be called at this time, with
this reason, under this campaign, at this attempt index." The follow-up
scheduler drains rows whose ``scheduled_at`` is due and ``status='pending'``.

The decision tree that creates these rows lives in
``app.services.followup_scheduler_service`` — promise-extracted memory takes
priority over admin disposition rules, both are clamped into the campaign's
allowed call window, and four kill switches gate enqueue (opt-out, max
attempts, conversion, manual pause).
"""
import enum
import uuid

from sqlalchemy import (
    Column,
    DateTime,
    Enum as SAEnum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base


class FollowupReason(str, enum.Enum):
    """Why this follow-up was scheduled. Drives surfacing/analytics, not flow."""

    # Memory extracted a callback-time promise from the prior call's transcript.
    # Highest priority — overrides disposition rules.
    promise_extracted = "promise_extracted"

    # Prior call's Exotel disposition was no_answer/voicemail/failed and the
    # campaign's followup_rules block defined a retry policy for that case.
    no_answer_retry = "no_answer_retry"
    voicemail_retry = "voicemail_retry"
    failed_retry = "failed_retry"

    # Prior call's classifier outcome was ``partial`` (soft yes, call later,
    # send details) — admin-configurable retry policy applies.
    partial_retry = "partial_retry"

    # Admin clicked "Schedule follow-up" on a lead in the UI.
    manual = "manual"


class FollowupStatus(str, enum.Enum):
    """State machine for a single follow-up row."""

    pending = "pending"        # scheduled, waiting for scheduled_at to be due
    in_flight = "in_flight"    # scheduler has placed the call, awaiting webhook
    completed = "completed"    # call placed and disposition received
    exhausted = "exhausted"    # max attempts hit at enqueue time
    cancelled = "cancelled"    # opt-out / conversion / admin cancel
    paused = "paused"          # admin pause (resumable, unlike cancelled)


class LeadFollowupSchedule(Base):
    """One scheduled follow-up call for one lead under (optionally) one campaign.

    A lead can have at most one ``pending`` row at a time (enforced at the
    service layer, not the DB — keeps the model flexible for cross-campaign
    futures). Once the call is placed, the row goes ``in_flight``; once the
    Exotel status webhook closes the call, the row goes ``completed`` and the
    service may enqueue a new row for the next attempt.
    """

    __tablename__ = "lead_followup_schedules"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    tenant_id = Column(
        String, ForeignKey("tenant_resources.tenant_id"), nullable=False, index=True
    )

    # Target: exactly one of lead_id / customer_id (DB CHECK enforces at least
    # one). Leads are the real-estate campaign path; customers are the clinic
    # manual-followup path (admin-commanded from the Customer base).
    lead_id = Column(
        UUID(as_uuid=True),
        ForeignKey("outgoing_leads.id"),
        nullable=True,
        index=True,
    )

    customer_id = Column(
        UUID(as_uuid=True),
        ForeignKey("customer_base.id"),
        nullable=True,
        index=True,
    )

    # Nullable because manual follow-ups may not be tied to a campaign. When
    # set, the agent re-uses this campaign's prompt + objectives + doc_text.
    campaign_id = Column(
        UUID(as_uuid=True),
        ForeignKey("outbound_campaigns.id"),
        nullable=True,
        index=True,
    )

    # The prior call's ``call_link_id`` (UUID-as-string the webhook resolves
    # against). Used by the prompt composer to surface a "previous call ended
    # X ago" line in the system prompt.
    source_call_id = Column(String, nullable=True)

    # Decisive moment: when the scheduler is allowed to place the call.
    # Always TZ-aware (UTC at rest); clamped into the campaign's local call
    # window before insert.
    scheduled_at = Column(DateTime(timezone=True), nullable=False, index=True)

    reason = Column(SAEnum(FollowupReason), nullable=False)

    # Admin's typed purpose for a manual follow-up ("remind about tomorrow's
    # appointment"). Rendered into the outbound system prompt as the REASON
    # FOR THIS CALL block, and shown on the follow-up chip.
    note = Column(Text, nullable=True)

    # How many follow-up calls have been placed for this lead+campaign pair
    # BEFORE this row. The scheduler increments and re-checks max_attempts
    # against the campaign's ``followup_rules`` at dispatch time.
    attempts = Column(Integer, nullable=False, default=0)

    status = Column(
        SAEnum(FollowupStatus),
        nullable=False,
        default=FollowupStatus.pending,
        index=True,
    )

    # The call_link_id of the actual follow-up call once placed (so the
    # /exotel/outbound-* webhooks can resolve this row from the call).
    placed_call_id = Column(String, nullable=True)

    # Admin pause/resume mechanism. Paused rows stay paused until resumed —
    # the scheduler skips them without bumping attempts.
    paused_at = Column(DateTime(timezone=True), nullable=True)
    paused_by = Column(
        UUID(as_uuid=True), ForeignKey("organization_users.id"), nullable=True
    )

    # Audit text for cancelled/exhausted rows ("opted_out", "converted",
    # "max_attempts", "admin", etc.) — surfaced in the per-lead status chip.
    cancelled_reason = Column(String, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        # Drain query: WHERE status='pending' AND scheduled_at <= now()
        Index("ix_followup_due", "status", "scheduled_at"),
        # Per-lead summary + chip: SELECT WHERE lead_id=... AND status IN (...)
        Index("ix_followup_lead", "lead_id", "status"),
        # Per-customer chip on the Customer base list.
        Index("ix_followup_customer", "customer_id", "status"),
    )
