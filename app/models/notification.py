"""Per-organization notification inbox.

Each row is one user-facing notification (hot lead captured, integration
went quiet, etc). Live push happens via the WebSocket fanout in
:class:`app.services.notification_service.NotificationService`; this
table is the durable backing store so a user who was offline still sees
the events on next login.

Severity tiers
--------------
- ``p1`` (push / interrupt) — hot lead, site visit, integration down.
- ``p2`` (in-app banner) — outbound batch summaries, sync results.
- ``p3`` (digest, rolled up into the EOD email) — never directly shown
  as a buzzing card. Currently unused but reserved so the schema doesn't
  need a migration when we add digests.

Read state
----------
``read_at`` is a timestamp (not a boolean) so we can later show "marked
read 3 minutes ago" + drive re-engagement logic without a second column.

Dedup
-----
``dedup_key`` is a free-form string (e.g. ``"integration_quiet:<conn_id>"``)
that the service layer uses to suppress repeat fires within a sliding
window. We don't UNIQUE it at the database level — two genuinely
different hot leads can share the same key bucket — but we DO index it
so the dedup query is cheap.
"""
from __future__ import annotations

import uuid

from sqlalchemy import Column, DateTime, ForeignKey, Index, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


# String constants. Keeping the enum out of SQLAlchemy (using a plain
# ``String`` column) lets us add new types without a schema migration —
# a notification kind that's already obsolete in the code can still be
# rendered from a backfilled row.
NOTIFICATION_INBOUND_LEAD = "inbound_lead"
NOTIFICATION_SITE_VISIT = "site_visit"
NOTIFICATION_OUTBOUND_BATCH = "outbound_batch"
NOTIFICATION_LEAD_SYNC = "lead_sync_captured"
NOTIFICATION_SYNC_QUIET = "lead_sync_quiet"

NOTIFICATION_TYPES = {
    NOTIFICATION_INBOUND_LEAD,
    NOTIFICATION_SITE_VISIT,
    NOTIFICATION_OUTBOUND_BATCH,
    NOTIFICATION_LEAD_SYNC,
    NOTIFICATION_SYNC_QUIET,
}

SEVERITY_P1 = "p1"  # interrupt / push
SEVERITY_P2 = "p2"  # in-app banner
SEVERITY_P3 = "p3"  # digest only

SEVERITIES = {SEVERITY_P1, SEVERITY_P2, SEVERITY_P3}


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    tenant_id = Column(
        String,
        ForeignKey("tenant_resources.tenant_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Short discriminator. See ``NOTIFICATION_*`` constants above.
    type = Column(String, nullable=False)
    severity = Column(String, nullable=False, server_default=SEVERITY_P2)
    # Display fields — kept on the row so the dropdown can render without
    # joining or re-running a template engine. Title is the one-line
    # headline ("New lead: Asha Rao wants a site visit"); body is the
    # optional second line ("Phone +91…, budget ₹85L").
    title = Column(String, nullable=False)
    body = Column(Text, nullable=True)
    # Structured payload for deep-links and analytics. Shape varies by
    # ``type`` (a ``site_visit`` carries ``record_id`` + ``visit_at``; a
    # ``lead_sync_captured`` carries ``connection_id`` + ``count``).
    payload = Column(JSONB, nullable=False, server_default="{}")
    # Dedup bucket. Suppresses repeat fires within the service-layer
    # window. NULL means "do not dedup" (always emit).
    dedup_key = Column(String, nullable=True)
    # NULL = unread. Stored as a timestamp so we can show "read 3 min ago"
    # without a second column.
    read_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    __table_args__ = (
        # Inbox query: "unread for my org, newest first". The partial
        # filter on read_at would buy us a smaller index, but partial
        # indexes complicate read_at IS NULL vs IS NOT NULL plans; the
        # composite below is good enough at the scales we care about.
        Index("ix_notifications_org_created_at", "organization_id", "created_at"),
        # Dedup query: "have we emitted this type+key for this org in the
        # last N seconds?" — supports the suppression check.
        Index("ix_notifications_dedup", "organization_id", "type", "dedup_key", "created_at"),
    )
