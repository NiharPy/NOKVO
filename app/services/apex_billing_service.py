"""NOKVO APEX plan billing — wallet crediting (shared by SuperAdmin create + webhook).

The APEX Call Credits wallet is the same rupee ledger as Nokvo One
(``minute_purchases.rupees`` − ``CallCost.prepaid_rupees``), but APEX credits are
**plan-driven**: ``minutes × plan_rate × (1 + bonus%)`` (see
:func:`app.services.apex_plans.wallet_credit_for`). This module is the single credit
path so the monthly grant (Phase 4 webhook, per-cycle) and the Free-Trial grant
(SuperAdmin create) share one idempotency-safe insert.

Idempotency:
  * ``razorpay_invoice_id`` set → one credit per (org, invoice) via the partial unique
    index ``uq_minute_purchases_org_invoice`` (a webhook retry is a no-op; each new cycle
    has a new invoice id, so it credits).
  * ``razorpay_payment_id`` set → deduped on the existing unique payment-id column.
  * neither (Free-Trial grant) → deduped on (org, source) so a trial credits at most once.
"""
from __future__ import annotations

import logging
import uuid
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.minute_purchase import MinutePurchase
from app.services.apex_plans import wallet_credit_for

logger = logging.getLogger(__name__)


async def credit_apex_wallet(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    minutes: int,
    rate,
    bonus_pct,
    source: str,
    razorpay_payment_id: str | None = None,
    razorpay_ref: str | None = None,
    razorpay_invoice_id: str | None = None,
) -> bool:
    """Credit ``minutes`` (at ``rate`` + ``bonus_pct``%) to the org's APEX wallet EXACTLY
    ONCE. Returns True if a new credit was written, False if it was already credited
    (idempotent). The rupee wallet value is ``wallet_credit_for(...)``; ``minutes`` is
    stored for the dashboard's estimated-minutes readout, ``rate_per_minute`` for audit."""
    if minutes is None or int(minutes) <= 0:
        return False

    # Pre-check the relevant idempotency key (the DB unique indexes are the real guard;
    # this just avoids a noisy IntegrityError on the common retry).
    if razorpay_invoice_id:
        exists = (
            await db.execute(
                select(MinutePurchase.id).where(
                    MinutePurchase.organization_id == organization_id,
                    MinutePurchase.razorpay_invoice_id == razorpay_invoice_id,
                )
            )
        ).first()
        if exists is not None:
            return False
    elif razorpay_payment_id:
        exists = (
            await db.execute(
                select(MinutePurchase.id).where(
                    MinutePurchase.razorpay_payment_id == razorpay_payment_id
                )
            )
        ).first()
        if exists is not None:
            return False
    else:
        # Free-Trial / non-payment grant — at most one per (org, source).
        exists = (
            await db.execute(
                select(MinutePurchase.id).where(
                    MinutePurchase.organization_id == organization_id,
                    MinutePurchase.source == source,
                )
            )
        ).first()
        if exists is not None:
            return False

    credit = wallet_credit_for(int(minutes), rate, bonus_pct)
    db.add(
        MinutePurchase(
            id=uuid.uuid4(),
            organization_id=organization_id,
            minutes=int(minutes),
            rupees=credit,
            rate_per_minute=Decimal(str(rate)),
            source=source,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_ref=razorpay_ref,
            razorpay_invoice_id=razorpay_invoice_id,
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        # A concurrent path (verify+webhook, or a retry) won the unique index — treat as
        # already credited; the wallet is correct either way.
        await db.rollback()
        return False

    from app.services.minute_balance_service import invalidate_balance_cache

    await invalidate_balance_cache(organization_id)
    logger.info(
        "APEX-CREDIT org=%s minutes=%s credit=%s source=%s invoice=%s",
        organization_id, minutes, credit, source, razorpay_invoice_id,
    )
    return True
