"""Affiliate commission ledger + settlement batches.

One :class:`AffiliateCommission` row per confirmed Razorpay subscription charge
of a referred APEX org: 5% of the first invoice (platform fee + prepaid-minutes
addon), 2% of every later monthly charge. Credit top-ups never accrue (they are
Order payments with no subscription id and never reach the accrual hook).

``razorpay_payment_id`` is UNIQUE — the verify + webhook convergence point
inserts idempotently, so a replayed event can never double-pay (same contract
as ``minute_purchases``). First-vs-recurring is decided by counting prior rows
for the same ``razorpay_subscription_id``.

Settlement is operator-marked manual payout: a row is *pending* while
``settlement_id IS NULL``, *due* once older than the T+2 window, and *settled*
when stamped with an :class:`AffiliateSettlement` batch (one bank transfer, one
UTR, N rows). There is no status column — the FK is the state.
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
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.sql import func

from app.db.session import Base


class AffiliateSettlement(Base):
    __tablename__ = "affiliate_settlements"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    affiliate_id = Column(
        UUID(as_uuid=True),
        ForeignKey("affiliates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # SUM of the commission rows stamped with this batch, for audit display.
    amount_rupees = Column(Numeric(14, 4), nullable=False)
    commission_count = Column(Integer, nullable=False)
    # Bank transfer reference the operator entered when marking settled.
    utr_reference = Column(String, nullable=False)
    settled_by = Column(String, nullable=False)  # superadmin email
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AffiliateCommission(Base):
    __tablename__ = "affiliate_commissions"

    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    affiliate_id = Column(
        UUID(as_uuid=True),
        ForeignKey("affiliates.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # The referred customer org whose charge earned this commission.
    organization_id = Column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    commission_type = Column(String, nullable=False)  # "first_month" | "recurring"
    # The charge the commission was computed on (actual paise Razorpay charged)
    # + the rate applied — kept separately so a policy change can recompute.
    billed_paise = Column(Integer, nullable=False)
    rate = Column(Numeric(5, 4), nullable=False)
    amount_rupees = Column(Numeric(14, 4), nullable=False)
    # THE idempotency anchor — verify + webhook both key off the payment id.
    razorpay_payment_id = Column(String, nullable=False, unique=True, index=True)
    # First-vs-recurring count key.
    razorpay_subscription_id = Column(String, nullable=False, index=True)
    raw_event = Column(JSONB, nullable=True)
    # SET NULL: deleting a settlement (defensive; not exposed) reverts rows to
    # pending rather than erasing money history.
    settlement_id = Column(
        UUID(as_uuid=True),
        ForeignKey("affiliate_settlements.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("razorpay_payment_id", name="uq_affiliate_commissions_rzp_payment_id"),
        Index("ix_affiliate_commissions_affiliate_created", "affiliate_id", "created_at"),
        # The due-queue filter: unsettled rows per affiliate, oldest first.
        Index(
            "ix_affiliate_commissions_affiliate_unsettled",
            "affiliate_id",
            "created_at",
            postgresql_where=text("settlement_id IS NULL"),
        ),
    )
