"""NOKVO Affiliate Program — public signup/login + affiliate dashboard.

An affiliate registers on the public /affiliate page: name, 18+ date of birth
(validated against IST — the target market — so a 2 AM IST 18th-birthday
signup isn't rejected because UTC is still the previous day), email, then TOTP
enrollment (QR into an authenticator app). Login is affiliate number + current
TOTP code — deliberately passwordless. Lost authenticator = superadmin
``reset-totp`` (support-ticket flow); there are no backup codes.

Sessions are a plain 12-hour access token on the dedicated ``affiliate`` JWT
tier — cryptographically isolated from org/superadmin tokens via the derived
per-tier secret; ``get_current_affiliate`` additionally hard-checks
``principal_type``. No refresh tokens (re-login is number+TOTP, cheap).

Commission accrual itself lives in payment paths (see
``app.services.affiliate_service.accrue_affiliate_commission``); this router
only reads the ledger for the dashboard.
"""
from __future__ import annotations

import logging
import re
import uuid
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import jwt
import pyotp
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core import security
from app.core.config import settings
from app.core.email_policy import extract_email_domain, normalize_email
from app.core.rate_limit import limiter
from app.core.secret_crypto import decrypt_secret, encrypt_secret
from app.models.affiliate import Affiliate
from app.models.affiliate_commission import AffiliateCommission
from app.models.organization import Organization
from app.services.affiliate_service import (
    allocate_affiliate_number,
    bank_details_complete,
    mask_account_number,
    mask_org_name,
    normalize_affiliate_number,
    resolve_active_affiliate_by_code,
)

logger = logging.getLogger(__name__)
router = APIRouter()

_IST = ZoneInfo("Asia/Kolkata")
_IFSC_RE = re.compile(r"^[A-Z]{4}0[A-Z0-9]{6}$")


def _parse_dob_18_plus(raw: str) -> date:
    """Parse YYYY-MM-DD and enforce the 18+ gate against IST 'today'."""
    try:
        dob = date.fromisoformat((raw or "").strip())
    except ValueError:
        raise HTTPException(status_code=400, detail="Enter your date of birth as YYYY-MM-DD.")
    today = datetime.now(_IST).date()
    if dob > today:
        raise HTTPException(status_code=400, detail="Date of birth can't be in the future.")
    age = today.year - dob.year - ((today.month, today.day) < (dob.month, dob.day))
    if age > 100:
        raise HTTPException(status_code=400, detail="Please check the date of birth you entered.")
    if age < 18:
        raise HTTPException(status_code=403, detail="You must be at least 18 to join the affiliate program.")
    return dob


# ─────────── tokens ───────────
def _issue_setup_token(affiliate_id: uuid.UUID) -> str:
    """30-min bridge between signup and TOTP verification. Same JWT tier as the
    session token but a distinct principal_type/stage, so it can never be
    presented to dashboard endpoints."""
    return security.create_access_token(
        subject=str(affiliate_id),
        mfa_completed=False,
        expires_delta=timedelta(minutes=30),
        token_tier=security.JWT_TIER_AFFILIATE,
        extra_claims={"principal_type": "affiliate_signup", "stage": "totp_setup"},
    )


def _decode_setup_token(token: str) -> uuid.UUID:
    try:
        payload = security.decode_access_token(
            token, expected_tiers=[security.JWT_TIER_AFFILIATE], allow_legacy_secret=False
        )
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired setup token") from exc
    if payload.get("principal_type") != "affiliate_signup" or payload.get("stage") != "totp_setup":
        raise HTTPException(status_code=401, detail="Invalid setup token type")
    try:
        return uuid.UUID(str(payload.get("sub")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Invalid setup token subject")


def _issue_session_token(affiliate: Affiliate) -> str:
    return security.create_access_token(
        subject=str(affiliate.id),
        mfa_completed=True,  # TOTP IS the login factor
        expires_delta=timedelta(hours=settings.AFFILIATE_ACCESS_TOKEN_EXPIRE_HOURS),
        token_tier=security.JWT_TIER_AFFILIATE,
        extra_claims={
            "principal_type": "affiliate",
            "affiliate_number": affiliate.affiliate_number,
        },
    )


async def get_current_affiliate(
    db: AsyncSession = Depends(deps.get_db),
    token: str = Depends(deps.oauth2_scheme),
) -> Affiliate:
    """Dashboard auth: affiliate-tier JWT + principal check + active status.
    Org/superadmin tokens fail signature verification against the derived
    affiliate secret; setup tokens fail the principal_type check."""
    try:
        payload = security.decode_access_token(
            token, expected_tiers=[security.JWT_TIER_AFFILIATE], allow_legacy_secret=False
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    if payload.get("principal_type") != "affiliate":
        raise HTTPException(status_code=403, detail="Not an affiliate session")
    try:
        affiliate_id = uuid.UUID(str(payload.get("sub")))
    except (TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    affiliate = await db.get(Affiliate, affiliate_id)
    if affiliate is None:
        raise HTTPException(status_code=401, detail="Could not validate credentials")
    if affiliate.status != "active":
        raise HTTPException(status_code=403, detail="This affiliate account is not active")
    return affiliate


def _me_payload(affiliate: Affiliate) -> dict:
    return {
        "id": str(affiliate.id),
        "affiliate_number": affiliate.affiliate_number,
        "full_name": affiliate.full_name,
        "email": affiliate.email,
        "status": affiliate.status,
        "kyc_verified": affiliate.kyc_verified_at is not None,
        "has_bank_details": bank_details_complete(affiliate),
        "bank": (
            {
                "account_holder": affiliate.bank_account_holder,
                "account_number_masked": mask_account_number(affiliate.bank_account_number),
                "ifsc": affiliate.bank_ifsc,
            }
            if bank_details_complete(affiliate)
            else None
        ),
        "created_at": affiliate.created_at.isoformat() if affiliate.created_at else None,
    }


# ─────────── public: signup → TOTP → login ───────────
@router.post("/signup")
@limiter.limit("5/hour")
async def affiliate_signup(
    request: Request,
    full_name: str = Form(...),
    date_of_birth: str = Form(...),
    email: str = Form(...),
    db: AsyncSession = Depends(deps.get_db),
):
    """Create (or reclaim) an affiliate in ``pending_totp`` and hand back the
    TOTP provisioning URI. The affiliate number is NOT revealed until the code
    is verified — an abandoned signup leaves nothing usable."""
    name = (full_name or "").strip()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="Enter your full name.")
    dob = _parse_dob_18_plus(date_of_birth)
    email_norm = normalize_email(email)
    try:
        extract_email_domain(email_norm)
    except ValueError:
        raise HTTPException(status_code=400, detail="Enter a valid email address.")

    existing = (
        await db.execute(select(Affiliate).where(Affiliate.email == email_norm))
    ).scalars().first()
    if existing is not None and existing.status != "pending_totp":
        raise HTTPException(
            status_code=409,
            detail="An affiliate account with this email already exists. Log in with your affiliate number.",
        )

    if existing is not None:
        # Reclaim an unverified signup (also the lost-phone recovery landing
        # after superadmin reset-totp): same id + number, fresh fields. The
        # identity details (name/DOB) may have changed, so the old KYC
        # approval no longer applies — clear it so payouts pause until the
        # operator re-verifies.
        #
        # KEEP the pending TOTP secret when one exists: rotating it here
        # silently invalidated a QR the user had already scanned (re-submitting
        # step 1 left two identically-named authenticator entries where only
        # the newest worked). The secret was never verified/used, so reusing it
        # for the same email is safe — and after superadmin reset-totp it was
        # already freshly rotated, so the lost phone still can't log in.
        affiliate = existing
        secret = None
        if affiliate.totp_secret_encrypted_v2:
            try:
                secret = decrypt_secret(affiliate.totp_secret_encrypted_v2)
            except Exception:
                secret = None  # undecryptable (key change) → issue a fresh one
        if not secret:
            secret = security.generate_totp_secret()
            affiliate.totp_secret_encrypted_v2 = encrypt_secret(secret)
        affiliate.full_name = name
        affiliate.date_of_birth = dob
        affiliate.kyc_verified_at = None
        affiliate.kyc_verified_by = None
    else:
        secret = security.generate_totp_secret()
        affiliate = Affiliate(
            id=uuid.uuid4(),
            affiliate_number=await allocate_affiliate_number(db),
            full_name=name,
            date_of_birth=dob,
            email=email_norm,
            totp_secret_encrypted_v2=encrypt_secret(secret),
            status="pending_totp",
        )

    db.add(affiliate)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        # Either a concurrent duplicate email (409) or a lottery-rare number
        # collision — retry the number once before giving up.
        dup_email = (
            await db.execute(select(Affiliate.id).where(Affiliate.email == email_norm))
        ).scalars().first()
        if dup_email is not None and existing is None:
            raise HTTPException(
                status_code=409,
                detail="An affiliate account with this email already exists. Log in with your affiliate number.",
            )
        affiliate.affiliate_number = await allocate_affiliate_number(db)
        db.add(affiliate)
        await db.commit()
    await db.refresh(affiliate)

    return {
        "setup_token": _issue_setup_token(affiliate.id),
        "totp_uri": pyotp.TOTP(secret).provisioning_uri(
            name=affiliate.email, issuer_name="NOKVO Affiliate"
        ),
        "secret": secret,
    }


class AffiliateTOTPVerifyRequest(BaseModel):
    setup_token: str
    code: str


@router.post("/signup/totp/verify")
@limiter.limit("20/hour")
async def affiliate_signup_totp_verify(
    request: Request,
    payload: AffiliateTOTPVerifyRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    affiliate_id = _decode_setup_token(payload.setup_token)
    affiliate = await db.get(Affiliate, affiliate_id)
    if affiliate is None or not affiliate.totp_secret_encrypted_v2:
        raise HTTPException(status_code=401, detail="Invalid setup token")
    if affiliate.status == "suspended":
        raise HTTPException(status_code=403, detail="This affiliate account is suspended.")
    if not security.verify_totp(decrypt_secret(affiliate.totp_secret_encrypted_v2), (payload.code or "").strip()):
        raise HTTPException(status_code=401, detail="That code didn't match — try the current code from your app.")
    affiliate.status = "active"
    affiliate.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(affiliate)
    return {
        "affiliate_number": affiliate.affiliate_number,
        "access_token": _issue_session_token(affiliate),
        "token_type": "bearer",
        "affiliate": _me_payload(affiliate),
    }


class AffiliateLoginRequest(BaseModel):
    affiliate_number: str
    code: str


@router.post("/login")
@limiter.limit("10/minute")
async def affiliate_login(
    request: Request,
    payload: AffiliateLoginRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    number = normalize_affiliate_number(payload.affiliate_number)
    affiliate = (
        await db.execute(select(Affiliate).where(Affiliate.affiliate_number == number))
    ).scalars().first()
    # Uniform 401 for unknown number / unverified signup / wrong code — no
    # affiliate-number enumeration oracle.
    invalid = HTTPException(status_code=401, detail="Invalid affiliate number or code")
    if affiliate is None or affiliate.status == "pending_totp" or not affiliate.totp_secret_encrypted_v2:
        raise invalid
    if affiliate.status == "suspended":
        raise HTTPException(status_code=403, detail="This affiliate account is suspended.")
    if not security.verify_totp(decrypt_secret(affiliate.totp_secret_encrypted_v2), (payload.code or "").strip()):
        raise invalid
    affiliate.last_login_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(affiliate)
    return {
        "access_token": _issue_session_token(affiliate),
        "token_type": "bearer",
        "affiliate": _me_payload(affiliate),
    }


@router.get("/check-code/{code}")
@limiter.limit("30/minute")
async def affiliate_check_code(request: Request, code: str, db: AsyncSession = Depends(deps.get_db)):
    """Public referral-code check for the APEX payment screen. Returns only a
    validity bit + first name — nothing enumerable."""
    affiliate = await resolve_active_affiliate_by_code(db, code)
    if affiliate is None:
        return {"valid": False, "display_name": None}
    first_name = (affiliate.full_name or "").strip().split(" ")[0]
    return {"valid": True, "display_name": first_name or None}


# ─────────── authenticated: dashboard ───────────
@router.get("/me")
async def affiliate_me(affiliate: Affiliate = Depends(get_current_affiliate)):
    return _me_payload(affiliate)


@router.get("/dashboard")
async def affiliate_dashboard(
    affiliate: Affiliate = Depends(get_current_affiliate),
    db: AsyncSession = Depends(deps.get_db),
):
    due_cutoff = datetime.now(timezone.utc) - timedelta(hours=settings.AFFILIATE_SETTLEMENT_DUE_HOURS)

    rows = (
        await db.execute(
            select(AffiliateCommission)
            .where(AffiliateCommission.affiliate_id == affiliate.id)
            .order_by(AffiliateCommission.created_at.desc())
        )
    ).scalars().all()

    accrued = pending = due = settled = 0.0
    for c in rows:
        amount = float(c.amount_rupees or 0)
        accrued += amount
        if c.settlement_id is not None:
            settled += amount
        else:
            pending += amount
            created = c.created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created is not None and created <= due_cutoff:
                due += amount

    # Referred customers: orgs attributed to this affiliate, privacy-masked, with
    # their per-org commission rollup. "payment_pending" = entered the code but
    # never completed payment (zero commission).
    orgs = (
        await db.execute(
            select(Organization)
            .where(Organization.affiliate_id == affiliate.id)
            .order_by(Organization.created_at.desc())
        )
    ).scalars().all()
    by_org: dict[uuid.UUID, list[AffiliateCommission]] = {}
    for c in rows:
        by_org.setdefault(c.organization_id, []).append(c)
    referred = []
    for org in orgs:
        org_rows = by_org.get(org.id, [])
        if org.status == "pending_payment":
            cust_status = "payment_pending"
        elif org.status == "suspended":
            cust_status = "cancelled"
        else:
            cust_status = "active"
        referred.append(
            {
                "joined_at": org.created_at.isoformat() if org.created_at else None,
                "name_masked": mask_org_name(org.name),
                "status": cust_status,
                "commission_count": len(org_rows),
                "total_commission_rupees": round(sum(float(c.amount_rupees or 0) for c in org_rows), 2),
            }
        )

    blocked_reason = None
    if affiliate.kyc_verified_at is None:
        blocked_reason = "kyc_pending"
    elif not bank_details_complete(affiliate):
        blocked_reason = "bank_details_missing"

    return {
        "totals": {
            "accrued_rupees": round(accrued, 2),
            "pending_rupees": round(pending, 2),
            "due_rupees": round(due, 2),
            "settled_rupees": round(settled, 2),
        },
        "kyc": {
            "verified": affiliate.kyc_verified_at is not None,
            "has_bank_details": bank_details_complete(affiliate),
            "settlement_blocked_reason": blocked_reason,
        },
        "ledger": [
            {
                "id": str(c.id),
                "created_at": c.created_at.isoformat() if c.created_at else None,
                "commission_type": c.commission_type,
                "rate": float(c.rate or 0),
                "billed_rupees": round((c.billed_paise or 0) / 100, 2),
                "amount_rupees": round(float(c.amount_rupees or 0), 2),
                "status": "settled" if c.settlement_id is not None else "pending",
                "customer": mask_org_name(next((o.name for o in orgs if o.id == c.organization_id), "")),
            }
            for c in rows[:100]
        ],
        "referred_customers": referred,
    }


class BankDetailsRequest(BaseModel):
    account_holder: str
    account_number: str
    ifsc: str


@router.put("/bank-details")
async def affiliate_bank_details(
    payload: BankDetailsRequest,
    affiliate: Affiliate = Depends(get_current_affiliate),
    db: AsyncSession = Depends(deps.get_db),
):
    holder = (payload.account_holder or "").strip()[:120]
    account = re.sub(r"\s+", "", payload.account_number or "")
    ifsc = (payload.ifsc or "").strip().upper()
    if not holder:
        raise HTTPException(status_code=400, detail="Enter the account holder's name.")
    if not account.isdigit() or not (9 <= len(account) <= 18):
        raise HTTPException(status_code=400, detail="Enter a valid account number (9–18 digits).")
    if not _IFSC_RE.match(ifsc):
        raise HTTPException(status_code=400, detail="Enter a valid IFSC code (e.g. HDFC0001234).")
    affiliate.bank_account_holder = holder
    affiliate.bank_account_number = account
    affiliate.bank_ifsc = ifsc
    await db.commit()
    await db.refresh(affiliate)
    return {
        "ok": True,
        "bank": {
            "account_holder": affiliate.bank_account_holder,
            "account_number_masked": mask_account_number(affiliate.bank_account_number),
            "ifsc": affiliate.bank_ifsc,
        },
    }
