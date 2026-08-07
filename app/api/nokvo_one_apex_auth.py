"""NOKVO APEX auth — own signup + login + Google, ISOLATED from Nokvo One.

APEX is a separate product: an APEX account is its own ``Organization`` tagged
``product_tier="nokvo_apex"`` with its own users and data. Tokens carry
``product="apex"`` (see ``security.org_product_claim``) and the org deps
cross-check that tag, so an APEX session can't drive Nokvo One and vice-versa.

The SAME email may own both a Nokvo One AND an APEX account — the uniqueness
checks here are scoped to ``product_tier="nokvo_apex"`` (the DB constraint is
per-org, so two orgs can share an email).

This module deliberately REUSES the Nokvo One auth helpers (session minting, MFA,
signup quotas, provisioning, Google verification) — only the product tier + the
account-resolution scope differ. Mounted at ``/api/nokvo-one/apex``.
"""
from __future__ import annotations

import uuid
from datetime import timedelta

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.nokvo_one_auth import (
    _issue_full_session,
    _issue_login_temp_token,
    _issue_setup_token,
    _safe_detail,
)
from app.core import security
from app.core.config import settings
from app.core.email_policy import normalize_email
from app.core.rate_limit import limiter
from app.core.totp_crypto import TOTPDecryptionError, decrypt_totp_secret
from app.models.apex_access_request import (
    APEX_REQUEST_OPEN_STATUSES,
    ApexAccessRequest,
)
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.schemas.nokvo_one import (
    NokvoOneGoogleLoginRequest,
    NokvoOneLoginRequest,
    NokvoOneLoginTOTPRequest,
    NokvoOneSignupRequest,
)
from app.services.google_oauth_service import GoogleOAuthError, GoogleOAuthService

router = APIRouter()

APEX_TIER = "nokvo_apex"


async def _resolve_apex_user(db: AsyncSession, email: str) -> tuple[OrganizationUser, Organization] | None:
    """Find the (user, org) for ``email`` within the APEX product, or None.
    Scoped to ``product_tier="nokvo_apex"`` so a Nokvo One account with the same
    email is never matched. Deterministic (oldest seat wins) in the historical
    case where the same email got seats in two APEX orgs — the member-invite
    guard now prevents new ones, but an unordered ``.first()`` made which
    account you logged into arbitrary."""
    res = await db.execute(
        select(OrganizationUser, Organization)
        .join(Organization, Organization.id == OrganizationUser.organization_id)
        .where(OrganizationUser.email == email, Organization.product_tier == APEX_TIER)
        .order_by(OrganizationUser.created_at.asc())
        .limit(1)
    )
    row = res.first()
    if row is None:
        return None
    return row[0], row[1]


# ─────────── Request access (replaces self-serve signup) ───────────
class ApexAccessRequestPayload(BaseModel):
    company_name: str | None = None
    contact_name: str | None = None
    email: str
    phone: str | None = None
    requested_plan: str | None = None
    notes: str | None = None
    # Opaque CAPTCHA/verification token — only checked when APEX_REQUEST_CAPTCHA is on.
    captcha_token: str | None = None


@router.post("/apex/request-access")
@limiter.limit("5/hour")
async def apex_request_access(
    request: Request,
    payload: ApexAccessRequestPayload,
    db: AsyncSession = Depends(deps.get_db),
):
    """Public 'request access' form (APEX has no self-serve signup). Persists an
    ``ApexAccessRequest`` for SuperAdmin to review + create the account. Rate-limited,
    per-email deduped (one open request per email), with an optional CAPTCHA gate."""
    email = normalize_email(payload.email)
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid email is required")
    if settings.APEX_REQUEST_CAPTCHA and not (payload.captcha_token or "").strip():
        raise HTTPException(status_code=400, detail="Please complete the verification challenge")

    # Dedupe: if this email already has an OPEN request, don't stack another.
    existing = (
        await db.execute(
            select(ApexAccessRequest.id).where(
                ApexAccessRequest.email == email,
                ApexAccessRequest.status.in_(list(APEX_REQUEST_OPEN_STATUSES)),
            ).limit(1)
        )
    ).first()
    if existing is None:
        db.add(
            ApexAccessRequest(
                id=uuid.uuid4(),
                company_name=(payload.company_name or "").strip() or None,
                contact_name=(payload.contact_name or "").strip() or None,
                email=email,
                phone=(payload.phone or "").strip() or None,
                requested_plan=(payload.requested_plan or "").strip().lower() or None,
                notes=(payload.notes or "").strip()[:4000] or None,
                status="new",
            )
        )
        await db.commit()
    # Always the same response (don't leak whether an open request already exists).
    return {"code": "request_received", "message": "Thanks — our team will reach out shortly."}


# ─────────── Signup (DISABLED unconditionally — APEX is request-gated) ───────────
@router.post("/apex/signup")
@limiter.limit("5/hour")
async def apex_signup(
    request: Request,
    payload: NokvoOneSignupRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    """Legacy self-serve APEX signup — CLOSED. APEX is request-gated (SuperAdmin creates
    accounts); the frontend routes to the request form. The close is UNCONDITIONAL: it used
    to be gated on ``ENABLE_APEX_PLANS``, which defaulted to OFF (and was unset in prod), so
    the gate never ran and this endpoint kept minting ``pending_payment`` orgs — an
    unregistered caller could reach the payment screen by hitting the API directly."""
    raise HTTPException(
        status_code=403,
        detail="Self-serve signup is closed. Please request access and our team will set you up.",
    )


# ─────────── Password login ───────────
@router.post("/apex/login")
@limiter.limit("10/minute")
async def apex_login(
    request: Request,
    payload: NokvoOneLoginRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    email = normalize_email(payload.email)
    resolved = await _resolve_apex_user(db, email)
    if resolved is None:
        raise HTTPException(status_code=401, detail="Invalid email or password")
    user, organization = resolved
    if not user.password_hash or not security.verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")
    if organization.status == "suspended":
        raise HTTPException(status_code=403, detail="Organization is suspended")
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="User account is disabled")

    # Resume from wherever the user left off. APEX payment is COMPULSORY — a
    # still-pending org ALWAYS returns to the payment screen (no dev auto-provision;
    # only the SuperAdmin bypass clears pending_payment without a charge).
    if organization.status == "pending_payment":
        return {
            "code": "payment_required",
            "payment_token": _issue_setup_token(user.id, organization.id, stage="payment", ttl_minutes=60),
            "organization_id": str(organization.id),
            "email": email,
            "org_status": organization.status,
        }

    # Paid but awaiting SuperAdmin activation (request-gated APEX). No session yet — the
    # account isn't usable until it's activated (within 6h; 24h for Free Trial).
    if organization.status == "pending_activation":
        return {
            "code": "pending_activation",
            "message": "Payment received — your account is being activated (within 6 hours).",
            "organization_id": str(organization.id),
            "email": email,
            "org_status": organization.status,
        }

    # Mid-onboarding → issue a session + tell the frontend which step to reopen
    # (don't flip the org to active until onboarding finishes).
    if organization.status == "onboarding":
        # A user who enrolled TOTP must still clear it mid-onboarding — don't let the
        # resume bypass the second factor. The verify endpoint resumes onboarding.
        if user.totp_secret_encrypted_v2:
            temp_access = _issue_login_temp_token(user, organization.id, product=security.PRODUCT_APEX)
            return {"access_token": temp_access, "refresh_token": "pending_totp", "token_type": "bearer", "mfa_pending": True}
        if user.status in {"invited", "pending_totp", "pending_payment"}:
            user.status = "active"
            db.add(user)
            await db.flush()
        session = await _issue_full_session(db, request, user, organization, mfa_completed=True)
        data = session.model_dump(mode="json")
        data["code"] = "onboarding_required"
        data["onboarding_step"] = organization.onboarding_step or "business_details"
        return data

    # No TOTP configured → issue a full session (deferred MFA).
    if not user.totp_secret_encrypted_v2:
        user.mfa_required = False
        if user.status in {"invited", "pending_totp", "pending_payment"}:
            user.status = "active"
        if organization.status == "pending_totp":
            organization.status = "active"
        db.add(user)
        db.add(organization)
        await db.flush()
        return await _issue_full_session(db, request, user, organization, mfa_completed=False)

    temp_access = _issue_login_temp_token(user, organization.id, product=security.PRODUCT_APEX)
    return {"access_token": temp_access, "refresh_token": "pending_totp", "token_type": "bearer", "mfa_pending": True}


@router.post("/apex/login/totp/verify")
@limiter.limit("20/minute")
async def apex_login_totp_verify(
    request: Request,
    payload: NokvoOneLoginTOTPRequest,
    db: AsyncSession = Depends(deps.get_db),
    token: str = Depends(deps.oauth2_scheme),
):
    try:
        claims = security.decode_access_token(token, expected_tiers=[security.JWT_TIER_ORGANIZATION])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired login token") from exc
    if not claims.get("nokvo_one_login") or claims.get("principal_type") != "organization_user":
        raise HTTPException(status_code=403, detail="Login temp token required")
    if claims.get("product") != security.PRODUCT_APEX:
        raise HTTPException(status_code=403, detail="APEX login token required")

    user_id = uuid.UUID(claims["sub"])
    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == user_id))
    user = user_res.scalars().first()
    if user is None or not user.totp_secret_encrypted_v2:
        raise HTTPException(status_code=404, detail="User or TOTP not found")
    try:
        secret = decrypt_totp_secret(user.totp_secret_encrypted_v2)
    except TOTPDecryptionError:
        raise HTTPException(status_code=500, detail="TOTP secret could not be decrypted")
    if not security.verify_totp(secret, payload.code):
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None or organization.product_tier != APEX_TIER:
        raise HTTPException(status_code=403, detail="Not an APEX organization")
    if user.status in {"invited", "pending_totp"}:
        user.status = "active"
        db.add(user)
        await db.flush()
    session = await _issue_full_session(db, request, user, organization)
    # Mid-onboarding (e.g. a TOTP-enrolled user routed here from the onboarding
    # resume) → tell the frontend to reopen the wizard, mirroring apex_login.
    if organization.status == "onboarding":
        data = session.model_dump(mode="json")
        data["code"] = "onboarding_required"
        data["onboarding_step"] = organization.onboarding_step or "business_details"
        return data
    return session


# ─────────── Google ───────────
@router.post("/apex/google/login")
@limiter.limit("10/minute")
async def apex_google_login(
    request: Request,
    payload: NokvoOneGoogleLoginRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    """Google OAuth for APEX — sign-in or sign-up into an APEX org. Reuses the
    shared Google client; the account is resolved/created within product_tier=
    nokvo_apex, so the same Google email can also have a Nokvo One account."""
    try:
        identity = await GoogleOAuthService.verify_id_token(payload.id_token)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=401, detail=_safe_detail(exc)) from exc

    email = normalize_email(identity["email"])
    resolved = await _resolve_apex_user(db, email)

    if resolved is None:
        # APEX is request-gated: accounts are created by the SuperAdmin after an access
        # request. Google may only LOG IN an existing account — it must NEVER create one.
        # Creating one here handed an unregistered user a payment token and dropped them
        # straight onto the payment screen. Send them to the request-access form instead.
        # Unconditional: this must NOT depend on ENABLE_APEX_PLANS. That flag defaulted to
        # OFF (and was unset in prod), which is precisely why the gate never ran.
        return {
            "code": "request_access_required",
            "email": email,
            "full_name": identity.get("full_name"),
            "message": (
                "No APEX account found for this Google email. "
                "Please request access and our team will set you up."
            ),
        }

    # Existing APEX account → sign in (or back to payment if still pending).
    user, organization = resolved
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="User account is disabled")
    if organization.status == "pending_payment":
        return {
            "code": "payment_required",
            "created_via_google": True,
            "payment_token": _issue_setup_token(user.id, organization.id, stage="payment", ttl_minutes=60),
            "organization_id": str(organization.id),
            "email": email,
            "org_status": organization.status,
        }
    # A user who enrolled TOTP must clear it even via Google. Google proves control
    # of the email, but it must NOT bypass a second factor the user explicitly set
    # up — otherwise whoever controls the Google email skips password + TOTP. Route
    # to the TOTP step (the verify endpoint resumes onboarding when relevant).
    if user.totp_secret_encrypted_v2:
        temp_access = _issue_login_temp_token(user, organization.id, product=security.PRODUCT_APEX)
        return {
            "access_token": temp_access,
            "refresh_token": "pending_totp",
            "token_type": "bearer",
            "mfa_pending": True,
            "created_via_google": True,
        }
    # Mid-onboarding → issue a session but send the frontend to onboarding (NOT the
    # dashboard) until business-details + documents are done. Mirrors apex_login.
    if organization.status == "onboarding":
        session = await _issue_full_session(db, request, user, organization, mfa_completed=True)
        data = session.model_dump(mode="json")
        data["created_via_google"] = True
        data["code"] = "onboarding_required"
        data["onboarding_step"] = organization.onboarding_step or "business_details"
        return data
    session = await _issue_full_session(db, request, user, organization, mfa_completed=True)
    data = session.model_dump(mode="json")
    data["created_via_google"] = True
    data["code"] = "ready"
    return data


# ─────────── Me ───────────
@router.get("/apex/me")
async def apex_me(
    user: OrganizationUser = Depends(deps.RequireApexOrganization(allowed_statuses=["active", "onboarding"])),
    db: AsyncSession = Depends(deps.get_db),
):
    org = (
        await db.execute(select(Organization).where(Organization.id == user.organization_id))
    ).scalars().first()
    # Onboarding is a HARD gate: an org that paid but hasn't finished
    # business-details + documents is still ``status="onboarding"`` (only the
    # documents step flips it to "active"). Report that so the frontend forces the
    # onboarding screen on resume / deep-link and never lets it into the dashboard.
    org_status = org.status if org else None
    onboarding_required = org_status != "active"
    return {
        "id": str(user.id),
        "email": user.email,
        "name": user.full_name,
        "role": user.role,
        "organization_id": str(user.organization_id),
        "organization_name": org.name if org else None,
        "product": security.PRODUCT_APEX,
        "status": org_status,
        "onboarding_required": onboarding_required,
        "onboarding_step": (org.onboarding_step or "business_details") if onboarding_required else None,
    }
