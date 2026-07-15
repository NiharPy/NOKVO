"""Affiliate program core: number generation, code resolution, commission accrual.

The affiliate number ("NKV" + 7 unambiguous chars) is both the public referral
code an APEX customer types at payment and the affiliate's login identifier.
Attribution is stamped on the customer ``Organization`` (``affiliate_id``) at
create-subscription; :func:`accrue_affiliate_commission` then reads THAT (never
the Razorpay notes) at each confirmed subscription charge.

Accrual contract (mirrors ``_record_minute_purchase``): best-effort, never
raises into a webhook/verify handler, commits itself, and is idempotent on the
UNIQUE ``razorpay_payment_id`` — a verify+webhook race or Razorpay retry can
never double-pay. First month (5%) vs recurring (2%) is decided by counting
prior commission rows for the same subscription id.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.affiliate import Affiliate
from app.models.affiliate_commission import AffiliateCommission
from app.models.organization import Organization

logger = logging.getLogger(__name__)

AFFILIATE_NUMBER_PREFIX = "NKV"
# Unambiguous, typeable-over-the-phone alphabet: A-Z minus I/O, digits minus 0/1.
AFFILIATE_NUMBER_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_AFFILIATE_NUMBER_LENGTH = 7  # 32^7 ≈ 3.4e10 — collisions are lottery-rare


def generate_affiliate_number() -> str:
    body = "".join(secrets.choice(AFFILIATE_NUMBER_ALPHABET) for _ in range(_AFFILIATE_NUMBER_LENGTH))
    return f"{AFFILIATE_NUMBER_PREFIX}{body}"


def normalize_affiliate_number(raw: Any) -> str:
    """Typo-tolerant form of a typed code: uppercase, spaces/hyphens stripped."""
    return "".join(str(raw or "").upper().split()).replace("-", "")


async def allocate_affiliate_number(db: AsyncSession) -> str:
    """A number not currently in use. The exists-check loop keeps the happy
    path collision-free; the UNIQUE constraint is the true guard (the signup
    insert retries once on an affiliate_number IntegrityError)."""
    for _ in range(10):
        candidate = generate_affiliate_number()
        exists = (
            await db.execute(select(Affiliate.id).where(Affiliate.affiliate_number == candidate))
        ).scalars().first()
        if exists is None:
            return candidate
    # 10 straight collisions means something is deeply wrong (or the table has
    # billions of rows) — surface it rather than looping forever.
    raise RuntimeError("Could not allocate a unique affiliate number")


async def resolve_active_affiliate_by_code(db: AsyncSession, code: Any) -> Affiliate | None:
    number = normalize_affiliate_number(code)
    if not number:
        return None
    affiliate = (
        await db.execute(select(Affiliate).where(Affiliate.affiliate_number == number))
    ).scalars().first()
    if affiliate is None or affiliate.status != "active":
        return None
    return affiliate


def mask_org_name(name: Any) -> str:
    """Privacy mask for the affiliate dashboard: 'Acme Corp' -> 'Ac•••'."""
    text = str(name or "").strip()
    if not text:
        return "•••"
    return f"{text[:2]}•••"


def mask_account_number(number: Any) -> str:
    digits = str(number or "").strip()
    return f"••••{digits[-4:]}" if digits else ""


def bank_details_complete(affiliate: Affiliate) -> bool:
    return bool(
        (affiliate.bank_account_holder or "").strip()
        and (affiliate.bank_account_number or "").strip()
        and (affiliate.bank_ifsc or "").strip()
    )


def settlement_eligible(affiliate: Affiliate) -> bool:
    """Derived payout gate: operator-verified KYC + bank details + active."""
    return bool(
        affiliate.status == "active"
        and affiliate.kyc_verified_at is not None
        and bank_details_complete(affiliate)
    )


async def accrue_affiliate_commission(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    razorpay_payment_id: str,
    razorpay_subscription_id: str,
    amount_paise: int,
    raw_event: dict | None = None,
) -> None:
    """Record the commission for one confirmed subscription charge, EXACTLY
    ONCE. No-op when the org has no affiliate attribution, the affiliate isn't
    active, the payment id is already recorded, or inputs are unusable.

    Call only AFTER the calling handler has committed its own state — this
    commits itself (and rolls back only its own insert on the race)."""
    try:
        if not razorpay_payment_id or not razorpay_subscription_id:
            return
        try:
            amount = int(amount_paise)
        except (TypeError, ValueError):
            return
        if amount <= 0:
            return

        org = await db.get(Organization, organization_id)
        if org is None or org.affiliate_id is None:
            return
        affiliate = await db.get(Affiliate, org.affiliate_id)
        if affiliate is None or affiliate.status != "active":
            # Suspension halts NEW accrual; existing rows are untouched.
            return

        existing = (
            await db.execute(
                select(AffiliateCommission.id).where(
                    AffiliateCommission.razorpay_payment_id == razorpay_payment_id
                )
            )
        ).scalars().first()
        if existing is not None:
            return

        # First-vs-recurring: any prior commission for this subscription means
        # the first invoice was already paid out. (Two DIFFERENT payments of one
        # subscription racing would need two invoices minted within milliseconds
        # — real cycles are a month apart, so a row count in this transaction is
        # sufficient; the unique payment id handles the verify-vs-webhook race.)
        prior = (
            await db.execute(
                select(func.count(AffiliateCommission.id)).where(
                    AffiliateCommission.razorpay_subscription_id == razorpay_subscription_id
                )
            )
        ).scalar() or 0
        if prior == 0:
            commission_type = "first_month"
            rate = Decimal(str(settings.AFFILIATE_FIRST_MONTH_RATE))
        else:
            commission_type = "recurring"
            rate = Decimal(str(settings.AFFILIATE_RECURRING_RATE))
        amount_rupees = (Decimal(amount) / Decimal(100) * rate).quantize(Decimal("0.0001"))

        db.add(
            AffiliateCommission(
                id=uuid.uuid4(),
                affiliate_id=affiliate.id,
                organization_id=organization_id,
                commission_type=commission_type,
                billed_paise=amount,
                rate=rate,
                amount_rupees=amount_rupees,
                razorpay_payment_id=razorpay_payment_id,
                razorpay_subscription_id=razorpay_subscription_id,
                raw_event=raw_event,
            )
        )
        try:
            await db.commit()
        except IntegrityError:
            # Verify+webhook race: the other path recorded this payment first.
            await db.rollback()
            return
        logger.info(
            "AFFILIATE-COMMISSION: %s %s earned ₹%s (%s of ₹%s) from org %s payment %s",
            affiliate.affiliate_number, commission_type, amount_rupees,
            rate, amount / 100, organization_id, razorpay_payment_id,
        )
    except Exception:
        # Best-effort by contract: an accrual failure must never break payment
        # activation or make the webhook non-200.
        logger.exception(
            "AFFILIATE-COMMISSION: accrual failed for org %s payment %s",
            organization_id, razorpay_payment_id,
        )
