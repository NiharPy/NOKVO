"""Per-call billing ledger.

One row per completed voice call. The ``rupees`` column is the ledger
amount at 4-dp precision (see :mod:`app.services.call_cost_calculator`).
Aggregations (today / this-month / all-time totals on the dashboard) sum
this column directly — never the duration — so the displayed bill always
matches the persisted ledger.

Indexing
--------
``(organization_id, started_at desc)`` is the dashboard hot path (the
cost widget loads "this month for my org"). ``call_id`` is unique so the
recorder can be safely retried without double-billing.
"""
from __future__ import annotations

import uuid

from sqlalchemy import (
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.sql import func

from app.db.session import Base


class CallCost(Base):
    __tablename__ = "call_costs"

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
    # The voice pipeline's call_id (may be an internal string for tester
    # calls, an Exotel call link id for inbound/outbound, etc). Unique so
    # the recorder can retry idempotently.
    call_id = Column(String, nullable=False, unique=True, index=True)
    # Discriminator: "inbound" | "outbound" | "tester". Lets the cost
    # summary endpoint split totals by call shape without joining other
    # tables.
    kind = Column(String, nullable=False, server_default="inbound")
    # Optional foreign key into outbound_campaigns when ``kind`` is
    # ``outbound``. NULL otherwise. We don't FK enforce because tester
    # / inbound calls have no campaign and we want to keep the schema
    # minimal — the value is informational.
    campaign_id = Column(UUID(as_uuid=True), nullable=True, index=True)
    # Billed duration as a Decimal (supports sub-second precision). Stored
    # as Numeric(12, 4) so 4 decimals fit and we can record up to
    # 99,999,999.9999 seconds (~3 years) without overflow.
    duration_seconds = Column(Numeric(12, 4), nullable=False, server_default="0")
    # Whole BILLED minutes (ceil of the call's seconds — every started minute is
    # charged in full). This is the unit the tiered tariff and the dashboard
    # invoice count; ``duration_seconds`` is retained only as the actual call
    # length for analytics.
    billed_minutes = Column(Integer, nullable=False, server_default="0")
    # Ledger amount in rupees at 4-dp precision. Numeric(14, 4) gives us
    # 10 integer digits — comfortably more than any realistic per-call
    # bill — without sacrificing the recurring-fraction tail.
    rupees = Column(Numeric(14, 4), nullable=False, server_default="0")
    # The per-second rate that was in force at the time of the call.
    # Persisting it future-proofs us against tariff changes — every old
    # row keeps the rate it was billed at, so historical totals stay
    # reproducible even after we lift the rate to ₹10/min next quarter.
    rate_per_second = Column(Numeric(10, 6), nullable=False)
    # PREPAID usage deduction (₹) for this call = 0.6 connection fee + per-second
    # talk rate × seconds, where the rate is set by the FIFO prepaid bundle the
    # rupees were spent from (see minute_pricing.call_usage_cost). This is what
    # depletes the prepaid rupee balance. 0 (default) for calls that DON'T deplete
    # it — deterministic / bulk-questionnaire outbound, never-connected calls, and
    # all rows before this billing model. The balance = SUM(minute_purchases.rupees)
    # − SUM(call_costs.prepaid_rupees). Distinct from ``rupees`` (legacy post-paid
    # bill, kept for the SuperAdmin margin view) and the COGS columns below.
    prepaid_rupees = Column(Numeric(14, 4), nullable=False, server_default="0")
    started_at = Column(DateTime(timezone=True), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    # W3C OpenTelemetry trace id (32-hex) for this call, when OTel is enabled.
    # Nullable: rows from before this column / calls with OTel off keep NULL.
    # Indexed so support can pivot "this call → its logs/LangSmith" by trace id.
    trace_id = Column(String, nullable=True, index=True)

    # ── Per-call COGS breakdown (STT + LLM + TTS + Plivo) ──────────────────
    # These are Nokvo's *vendor cost* for the call, NOT what the tenant is
    # billed (that's ``rupees``). All nullable: only calls recorded after the
    # instrumentation landed carry them — historical rows stay NULL and the
    # SuperAdmin console renders them as "total-only".
    #
    # Raw usage counters (let us re-price later if a vendor rate changes):
    llm_input_tokens = Column(Integer, nullable=True)
    llm_output_tokens = Column(Integer, nullable=True)
    llm_cached_tokens = Column(Integer, nullable=True)
    stt_seconds = Column(Numeric(12, 4), nullable=True)
    tts_characters = Column(Integer, nullable=True)
    # Component costs in INR (computed at record time from the usage counters
    # and the rate-in-force, mirroring how ``rate_per_second`` is frozen per
    # row). ``cost_total_inr`` is the pre-summed COGS so dashboard rollups
    # don't re-add the four components every query.
    cost_stt_inr = Column(Numeric(12, 4), nullable=True)
    cost_llm_inr = Column(Numeric(12, 4), nullable=True)
    cost_tts_inr = Column(Numeric(12, 4), nullable=True)
    cost_telephony_inr = Column(Numeric(12, 4), nullable=True)
    cost_total_inr = Column(Numeric(12, 4), nullable=True)

    __table_args__ = (
        UniqueConstraint("call_id", name="uq_call_costs_call_id"),
        # Dashboard query: "calls for org X in time range [a, b]". This
        # composite index supports both the "all-time" and the "this
        # month" variants without resorting to a separate plan.
        Index("ix_call_costs_org_started_at", "organization_id", "started_at"),
    )
