from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
import pyotp
import asyncio
import json
from typing import AsyncGenerator

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api import deps
from app.core import security
from app.core.config import settings
from app.core.email_policy import extract_email_domain, normalize_email, validate_work_email
from app.core.rate_limit import limiter
from app.core.totp_crypto import (
    TOTPDecryptionError,
    decrypt_totp_secret,
    encrypt_totp_secret,
)
from app.models.email_verification import EmailVerification
from app.models.organization import Organization
from app.models.organization_session import OrganizationSession
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.schemas.nokvo_one import (
    NokvoOneBusinessTemplateOptionResponse,
    NokvoOneBusinessTemplateRequest,
    NokvoOneBusinessTemplateSaveResponse,
    NokvoOneBusinessSchemaUpdateRequest,
    NokvoOneCustomTabCreateRequest,
    NokvoOneCustomTabResponse,
    NokvoOneEmailVerifiedResponse,
    NokvoOneGoogleLoginRequest,
    NokvoOneLoginRequest,
    NokvoOneLoginTOTPRequest,
    NokvoOneOrganizationResponse,
    NokvoOnePostTotpResponse,
    NokvoOneProvisioningStepResponse,
    NokvoOneProvisioningSummary,
    NokvoOneSessionResponse,
    NokvoOneSignupRequest,
    NokvoOneSignupResponse,
    NokvoOneSignupSkipTOTPRequest,
    NokvoOneTOTPSetupRequest,
    NokvoOneTOTPSetupResponse,
    NokvoOneTOTPVerifyRequest,
    NokvoOneUserResponse,
)
from app.schemas.token import RefreshRequest, Token
from app.services.email_service import EmailService
from app.services.google_oauth_service import GoogleOAuthError, GoogleOAuthService
from app.services.nokvo_one_provisioning_service import (
    NokvoOneProvisioningError,
    NokvoOneProvisioningService,
)
from app.services.agent_runtime_bundle import invalidate as invalidate_runtime_bundle
from app.services.nokvo_one_business_templates import (
    apply_schema_overrides,
    business_type_config,
    business_type_options,
    custom_tabs_from_overrides,
    default_custom_tab_status_vocabulary,
    field_catalog_for,
    normalize_business_type,
    normalize_custom_tab_slug,
)
from app.services.tool_flow_questions import (
    _kind_for_field,
    _question_for_kind,
    ensure_tool_flow_questions,
)



def _safe_detail(exc: BaseException) -> str:
    """Return a user-safe error detail (forward RuntimeError/ValueError text;
    swallow internal exception messages and log them)."""
    import logging
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)
    logging.getLogger(__name__).exception("unexpected exception in request handler", exc_info=exc)
    return "Operation failed"


router = APIRouter()


# ─────────── Helpers ───────────


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _issue_setup_token(user_id: uuid.UUID, organization_id: uuid.UUID, stage: str, ttl_minutes: int = 30) -> str:
    """Short-lived JWT used to gate signup-stage flows (email-verified → TOTP setup)."""
    return security.create_setup_token(
        {
            "sub": str(user_id),
            "organization_id": str(organization_id),
            "stage": stage,
            "principal_type": "nokvo_one_signup",
        },
        expires_delta=timedelta(minutes=ttl_minutes),
    )


def _decode_setup_token(token: str, expected_stage: str) -> dict:
    try:
        payload = security.decode_setup_token(token)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=401, detail="Invalid or expired setup token") from exc
    if payload.get("principal_type") != "nokvo_one_signup":
        raise HTTPException(status_code=401, detail="Invalid setup token type")
    if payload.get("stage") != expected_stage:
        raise HTTPException(status_code=403, detail=f"Setup token stage must be '{expected_stage}'")
    return payload


def _issue_login_temp_token(user: OrganizationUser, organization_id: uuid.UUID) -> str:
    return security.create_access_token(
        subject=user.id,
        mfa_completed=False,
        expires_delta=timedelta(minutes=5),
        token_tier=security.JWT_TIER_ORGANIZATION,
        extra_claims={
            "principal_type": "organization_user",
            "organization_id": str(organization_id),
            "role": user.role,
            "nokvo_one_login": True,
        },
    )


def _organization_response(organization: Organization) -> NokvoOneOrganizationResponse:
    return NokvoOneOrganizationResponse(
        id=organization.id,
        name=organization.name,
        product_tier=organization.product_tier,
        status=organization.status,
        calling_enabled=organization.calling_enabled,
        admin_email=organization.admin_email,
        email_domain=organization.email_domain,
        environment=organization.environment,
        region=organization.region,
        industry=normalize_business_type(organization.industry),
    )


async def _tenant_resources_for_org(db: AsyncSession, organization_id: uuid.UUID) -> TenantResources:
    res = await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
    tenant_res = res.scalars().first()
    if tenant_res is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")
    return tenant_res


def _resolved_business_template(
    organization: Organization,
    tenant_res: TenantResources | None = None,
) -> NokvoOneBusinessTemplateOptionResponse:
    config = business_type_config(organization.industry)
    if config is None:
        raise HTTPException(status_code=409, detail="Business Type is not selected")
    provider_status = dict((tenant_res.provider_status if tenant_res else {}) or {})
    overrides = provider_status.get("business_template_schema_overrides") or {}
    return NokvoOneBusinessTemplateOptionResponse(**apply_schema_overrides(config, overrides))


async def _issue_full_session(
    db: AsyncSession,
    request: Request,
    user: OrganizationUser,
    organization: Organization,
    *,
    mfa_completed: bool = True,
) -> NokvoOneSessionResponse:
    raw_refresh, token_hash = security.create_refresh_token()
    session = OrganizationSession(
        id=uuid.uuid4(),
        organization_user_id=user.id,
        refresh_token_hash=token_hash,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.REFRESH_TOKEN_EXPIRE_HOURS),
    )
    db.add(session)
    user.last_login_at = datetime.now(timezone.utc)
    if request.client and request.client.host:
        user.last_login_ip = request.client.host
    db.add(user)
    await db.commit()
    await db.refresh(user)

    access_token = security.create_access_token(
        subject=user.id,
        mfa_completed=mfa_completed,
        session_id=str(session.id),
        token_tier=security.JWT_TIER_ORGANIZATION,
        extra_claims={
            "principal_type": "organization_user",
            "organization_id": str(organization.id),
            "role": user.role,
            "product_tier": organization.product_tier or "nokvo_one",
        },
    )
    return NokvoOneSessionResponse(
        access_token=access_token,
        refresh_token=raw_refresh,
        token_type="bearer",
        user=NokvoOneUserResponse.model_validate(user),
        organization=_organization_response(organization),
    )


def _summary_from_provision_dict(provision: dict) -> NokvoOneProvisioningSummary:
    ps = dict(provision.get("provider_status") or {})
    exotel = ps.get("plivo") or {}  # telephony status (Plivo) — field name kept for API compat
    return NokvoOneProvisioningSummary(
        tenant_id=provision["tenant_id"],
        azure_resource_group_name=provision.get("azure_resource_group_name"),
        azure_region=provision.get("azure_region"),
        blob_prefix=provision.get("blob_prefix"),
        storage_account_name=provision.get("storage_account_name"),
        qdrant_collection_name=provision.get("qdrant_collection_name"),
        qdrant_url_ref=provision.get("qdrant_url_ref"),
        redis_namespace=provision.get("redis_namespace"),
        llm_provider=ps.get("llm_provider"),
        llm_model=ps.get("llm_model"),
        llm_deployment=ps.get("llm_deployment"),
        llm_endpoint=ps.get("llm_endpoint"),
        llm_region=ps.get("llm_region"),
        llm_status=ps.get("llm_status"),
        llm_api_key_present=bool(ps.get("llm_api_key_encrypted") or ps.get("llm_api_key_secret_ref")),
        key_vault_name=ps.get("key_vault_name"),
        key_vault_status=ps.get("key_vault_status"),
        llm_api_key_secret_ref=ps.get("llm_api_key_secret_ref"),
        exotel_status=exotel.get("status"),
        steps=[
            NokvoOneProvisioningStepResponse(**step)
            for step in (provision.get("provisioning_steps") or [])
        ],
        provisioning_status=provision.get("provisioning_status", "unknown"),
    )


def _summary_from_tenant_resources(tr: TenantResources) -> NokvoOneProvisioningSummary:
    ps = dict(tr.provider_status or {})
    exotel = ps.get("plivo") or {}  # telephony status (Plivo) — field name kept for API compat
    return NokvoOneProvisioningSummary(
        tenant_id=tr.tenant_id,
        azure_resource_group_name=tr.azure_resource_group_name,
        azure_region=tr.azure_region,
        blob_prefix=tr.blob_prefix,
        storage_account_name=tr.storage_account_name,
        qdrant_collection_name=tr.qdrant_collection_name,
        qdrant_url_ref=tr.qdrant_url_ref,
        redis_namespace=tr.redis_namespace,
        llm_provider=ps.get("llm_provider"),
        llm_model=ps.get("llm_model"),
        llm_deployment=ps.get("llm_deployment"),
        llm_endpoint=ps.get("llm_endpoint"),
        llm_region=ps.get("llm_region"),
        llm_status=ps.get("llm_status"),
        llm_api_key_present=bool(ps.get("llm_api_key_encrypted") or ps.get("llm_api_key_secret_ref")),
        key_vault_name=ps.get("key_vault_name"),
        key_vault_status=ps.get("key_vault_status"),
        llm_api_key_secret_ref=ps.get("llm_api_key_secret_ref"),
        exotel_status=exotel.get("status"),
        steps=[
            NokvoOneProvisioningStepResponse(**step)
            for step in (tr.provisioning_steps or [])
        ],
        provisioning_status=tr.provisioning_status or "unknown",
    )


async def _provision_or_503(
    organization_id: uuid.UUID, organization_name: str, region: str, environment: str = "staging"
) -> dict:
    """Run the all-or-nothing tenant provisioner. Raise HTTP 503 on failure so signup
    short-circuits before any Organization row is committed."""
    try:
        return await NokvoOneProvisioningService.provision_or_raise(
            organization_id=organization_id,
            organization_name=organization_name,
            environment=environment,
            region=region,
        )
    except NokvoOneProvisioningError as exc:
        raise HTTPException(
            status_code=503,
            detail={
                "code": "provisioning_failed",
                "step": exc.step,
                "message": exc.message,
            },
        ) from exc


async def _lookup_org_by_domain(db: AsyncSession, domain: str) -> Organization | None:
    res = await db.execute(
        select(Organization).where(
            Organization.email_domain == domain,
            Organization.product_tier == "nokvo_one",
        )
    )
    return res.scalars().first()


async def _enforce_signup_attempt_quotas(db: AsyncSession, email: str, domain: str) -> None:
    """DB-backed per-email / per-domain abuse controls.

    Per-email: at most 3 unconsumed verifications issued in the last 24 hours.
    Per-domain: at most 10 verifications issued in the last 24 hours (counts all rows for the domain).
    """
    window_start = datetime.now(timezone.utc) - timedelta(hours=24)

    from sqlalchemy import func as sa_func
    email_count_scalar = await db.execute(
        select(sa_func.count(EmailVerification.id)).where(
            EmailVerification.email == email,
            EmailVerification.created_at >= window_start,
        )
    )
    email_count = email_count_scalar.scalar() or 0
    if email_count >= 3:
        raise HTTPException(
            status_code=429,
            detail="Too many signup attempts for this email in the last 24 hours",
        )

    # Domain quota — SKIPPED for personal/shared providers (gmail.com, etc.),
    # since those are shared by countless unrelated users and a per-domain cap
    # would throttle all of them together. The per-email cap above still guards
    # single-address abuse. Company domains keep the per-domain cap.
    from app.core.email_policy import PERSONAL_EMAIL_DOMAINS

    if domain not in PERSONAL_EMAIL_DOMAINS:
        domain_res = await db.execute(
            select(sa_func.count(EmailVerification.id)).where(
                EmailVerification.email.like(f"%@{domain}"),
                EmailVerification.created_at >= window_start,
            )
        )
        if (domain_res.scalar() or 0) >= 10:
            raise HTTPException(
                status_code=429,
                detail="Too many signup attempts for this email domain in the last 24 hours",
            )


# ─────────── Signup ───────────


def _sse(event_type: str, data: dict) -> bytes:
    """Format an SSE event line. Each event is one JSON blob on a `data:` line."""
    payload = json.dumps({"event": event_type, **data}, default=str)
    return f"data: {payload}\n\n".encode("utf-8")


@router.post("/signup", response_model=NokvoOneSignupResponse)
@limiter.limit("5/hour")
async def nokvo_one_signup(
    request: Request,
    payload: NokvoOneSignupRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    """Create the account ONLY — no resources are provisioned yet.

    The org lands in ``pending_payment``. The frontend then opens the Razorpay
    payment screen (authorized by the returned ``payment_token``); resources are
    provisioned after payment succeeds (see ``nokvo_one_payments``). Email
    verification + MFA continue as before, *after* payment.
    """
    email = normalize_email(payload.admin_email)
    domain = extract_email_domain(email)

    # No per-domain uniqueness — orgs are NOT tied to email domains (many can
    # share gmail.com). The account is unique by EMAIL only.
    await _enforce_signup_attempt_quotas(db, email, domain)
    existing_user = await db.execute(select(OrganizationUser).where(OrganizationUser.email == email))
    if existing_user.scalars().first() is not None:
        raise HTTPException(status_code=409, detail="An account with this email already exists")

    region = payload.region or "southindia"
    org_name = payload.org_name.strip()

    organization = Organization(
        id=uuid.uuid4(),
        name=org_name,
        admin_email=email,
        admin_name=payload.admin_name,
        email_domain=domain,
        region=region,
        environment="staging",
        call_type="inbound",
        language=payload.language or "en-IN",
        plan_type=None,
        product_tier="nokvo_one",
        status="pending_payment",
        calling_enabled=False,
        stores_pii=True,
        record_calls=False,
        create_resource_group=False,
        twilio_auto_provision=False,
        industry=None,
        country_code=payload.country_code,
    )
    db.add(organization)
    await db.flush()

    admin_user = OrganizationUser(
        id=uuid.uuid4(),
        organization_id=organization.id,
        email=email,
        full_name=payload.admin_name,
        role="admin",
        status="pending_payment",
        auth_provider="password",
        password_hash=security.get_password_hash(payload.password),
        mfa_required=True,
        email_verified=False,
    )
    db.add(admin_user)
    await db.commit()
    await db.refresh(organization)
    await db.refresh(admin_user)

    payment_token = _issue_setup_token(admin_user.id, organization.id, stage="payment", ttl_minutes=60)
    return NokvoOneSignupResponse(
        organization_id=organization.id,
        admin_user_id=admin_user.id,
        email=email,
        org_status=organization.status,
        payment_token=payment_token,
    )


@router.get("/signup/verify-email", response_model=NokvoOneEmailVerifiedResponse)
@limiter.limit("30/hour")
async def nokvo_one_verify_email(request: Request, token: str, db: AsyncSession = Depends(deps.get_db)):
    token_hash = _hash_token(token)
    res = await db.execute(select(EmailVerification).where(EmailVerification.token_hash == token_hash))
    verification = res.scalars().first()
    if verification is None:
        raise HTTPException(status_code=404, detail="Verification token not found")
    if verification.consumed_at is not None:
        raise HTTPException(status_code=410, detail="Verification token has already been used")
    if verification.expires_at < datetime.now(timezone.utc):
        raise HTTPException(status_code=410, detail="Verification token has expired")

    org_res = await db.execute(select(Organization).where(Organization.id == verification.organization_id))
    organization = org_res.scalars().first()
    if organization is None or organization.product_tier != "nokvo_one":
        raise HTTPException(status_code=404, detail="Organization not found")

    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == verification.organization_user_id))
    user = user_res.scalars().first()
    if user is None:
        raise HTTPException(status_code=404, detail="Organization user not found")

    verification.consumed_at = datetime.now(timezone.utc)
    user.email_verified = True
    if user.status == "pending_email_verification":
        user.status = "pending_totp"
    if organization.status == "pending_email_verification":
        organization.status = "pending_totp"
    db.add(verification)
    db.add(user)
    db.add(organization)
    await db.commit()
    await db.refresh(organization)

    setup_token = _issue_setup_token(user.id, organization.id, stage="totp_setup")

    return NokvoOneEmailVerifiedResponse(
        organization_id=organization.id,
        email=user.email,
        org_status=organization.status,
        setup_token=setup_token,
    )


# ─────────── Signup TOTP ───────────


@router.post("/signup/totp/setup", response_model=NokvoOneTOTPSetupResponse)
@limiter.limit("10/hour")
async def nokvo_one_signup_totp_setup(
    request: Request,
    payload: NokvoOneTOTPSetupRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    claims = _decode_setup_token(payload.setup_token, expected_stage="totp_setup")
    user_id = uuid.UUID(claims["sub"])
    org_id = uuid.UUID(claims["organization_id"])

    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == user_id))
    user = user_res.scalars().first()
    if user is None or user.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Organization user not found")
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Email must be verified first")
    if user.totp_secret_encrypted_v2:
        raise HTTPException(status_code=409, detail="TOTP is already configured for this account")

    secret = security.generate_totp_secret()
    user.totp_secret_encrypted_v2 = encrypt_totp_secret(secret)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Nokvo One")
    return NokvoOneTOTPSetupResponse(
        email=user.email,
        secret=secret,
        uri=provisioning_uri,
        setup_token=payload.setup_token,
    )


@router.post("/signup/totp/verify", response_model=NokvoOnePostTotpResponse)
@limiter.limit("20/hour")
async def nokvo_one_signup_totp_verify(
    request: Request,
    payload: NokvoOneTOTPVerifyRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    claims = _decode_setup_token(payload.setup_token, expected_stage="totp_setup")
    user_id = uuid.UUID(claims["sub"])
    org_id = uuid.UUID(claims["organization_id"])

    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == user_id))
    user = user_res.scalars().first()
    if user is None or user.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Organization user not found")
    if not user.totp_secret_encrypted_v2:
        raise HTTPException(status_code=400, detail="TOTP has not been initialised")

    try:
        secret = decrypt_totp_secret(user.totp_secret_encrypted_v2)
    except TOTPDecryptionError as exc:
        raise HTTPException(status_code=500, detail="TOTP secret could not be decrypted") from exc

    if not security.verify_totp(secret, payload.code):
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    user.status = "active"  # org is usable immediately — no superadmin approval gate
    org_res = await db.execute(select(Organization).where(Organization.id == org_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if organization.status == "pending_totp":
        organization.status = "active"
    db.add(user)
    db.add(organization)
    await db.commit()
    await db.refresh(organization)

    return NokvoOnePostTotpResponse(organization_id=organization.id, org_status=organization.status)


@router.post("/signup/skip-totp", response_model=NokvoOneSessionResponse)
@limiter.limit("10/hour")
async def nokvo_one_signup_skip_totp(
    request: Request,
    payload: NokvoOneSignupSkipTOTPRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    """Onboarding v2: defer MFA for the founding admin.

    Promotes the user to active locally without forcing TOTP setup, mints a
    session with mfa_completed=False, and flips user.mfa_required=False so
    subsequent logins also succeed without MFA. Sensitive actions stay
    blocked by RequireMFACompleted until the user completes MFA from the
    dashboard banner.

    Only available when NOKVO_ONBOARDING_V2 is enabled. Member invites and
    privileged actions still require MFA.
    """
    if not settings.NOKVO_ONBOARDING_V2:
        raise HTTPException(status_code=404, detail="Endpoint not available")

    claims = _decode_setup_token(payload.setup_token, expected_stage="totp_setup")
    user_id = uuid.UUID(claims["sub"])
    org_id = uuid.UUID(claims["organization_id"])

    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == user_id))
    user = user_res.scalars().first()
    if user is None or user.organization_id != org_id:
        raise HTTPException(status_code=404, detail="Organization user not found")
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Email must be verified first")
    # Founding admin only — invited members must complete MFA on first login.
    if user.role != "admin" or user.invited_by is not None:
        raise HTTPException(status_code=403, detail="MFA cannot be deferred for invited members")
    # If TOTP is already configured, the user must complete the normal flow.
    if user.totp_secret_encrypted_v2:
        raise HTTPException(status_code=409, detail="TOTP is already configured for this account")

    org_res = await db.execute(select(Organization).where(Organization.id == org_id))
    organization = org_res.scalars().first()
    if organization is None or organization.product_tier != "nokvo_one":
        raise HTTPException(status_code=404, detail="Organization not found")

    user.mfa_required = False
    user.status = "active"
    if organization.status == "pending_totp":
        organization.status = "active"
    db.add(user)
    db.add(organization)
    await db.flush()

    return await _issue_full_session(
        db, request, user, organization, mfa_completed=False
    )


# ─────────── Password login ───────────


@router.post("/login")
@limiter.limit("10/minute")
async def nokvo_one_login(
    request: Request,
    payload: NokvoOneLoginRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    email = normalize_email(payload.email)
    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.email == email))
    user = user_res.scalars().first()

    if user is None or not user.password_hash:
        raise HTTPException(status_code=401, detail="Invalid email or password")

    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None or organization.product_tier != "nokvo_one":
        raise HTTPException(status_code=403, detail="This account is not associated with a Nokvo One organization")
    if organization.status == "suspended":
        raise HTTPException(status_code=403, detail="Organization is suspended")
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="User account is disabled")
    if not security.verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    # ── Resume unfinished onboarding from wherever the user left off ──────────
    # The org status is the source of truth for the onboarding stage.
    if organization.status == "pending_payment":
        return {
            "code": "payment_required",
            "payment_token": _issue_setup_token(user.id, organization.id, stage="payment", ttl_minutes=60),
            "organization_id": str(organization.id),
            "email": email,
            "org_status": organization.status,
        }
    if organization.status == "onboarding":
        # Resume the post-payment onboarding wizard. No email/MFA gate — issue a
        # full session and tell the frontend which step to reopen.
        session = await _issue_full_session(db, request, user, organization, mfa_completed=True)
        data = session.model_dump()
        data["code"] = "onboarding_required"
        data["onboarding_step"] = organization.onboarding_step or "business_details"
        return data
    # Legacy email-verification gate (kept for orgs created before the onboarding
    # wizard; new password signups never set this status).
    if organization.status == "pending_email_verification":
        return {
            "code": "email_verification_required",
            "organization_id": str(organization.id),
            "email": email,
            "org_status": organization.status,
        }

    if not user.totp_secret_encrypted_v2:
        user.mfa_required = False
        if user.status in {"invited", "pending_totp"}:
            user.status = "active"
        if organization.status == "pending_totp":
            organization.status = "active"
        db.add(user)
        db.add(organization)
        await db.flush()
        return await _issue_full_session(db, request, user, organization, mfa_completed=False)

    temp_access = _issue_login_temp_token(user, organization.id)
    return {
        "access_token": temp_access,
        "refresh_token": "pending_totp",
        "token_type": "bearer",
        "mfa_pending": True,
    }


@router.post("/login/totp/verify", response_model=NokvoOneSessionResponse)
@limiter.limit("20/minute")
async def nokvo_one_login_totp_verify(
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
    if organization is None or organization.product_tier != "nokvo_one":
        raise HTTPException(status_code=403, detail="Not a Nokvo One organization")

    # First successful TOTP login after signup promotes the invited admin to active locally,
    # while organization activation still requires superadmin approval.
    if user.status in {"invited", "pending_totp"}:
        user.status = "active"
        db.add(user)
        await db.flush()

    return await _issue_full_session(db, request, user, organization)


# ─────────── Session MFA setup / step-up ───────────


@router.post("/mfa/totp/setup", response_model=NokvoOneTOTPSetupResponse)
@limiter.limit("10/hour")
async def nokvo_one_session_totp_setup(
    request: Request,
    user: OrganizationUser = Depends(deps.get_current_organization_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Start TOTP setup from an already authenticated deferred-MFA session."""
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None or organization.product_tier != "nokvo_one":
        raise HTTPException(status_code=403, detail="Not a Nokvo One organization")
    if organization.status not in {"active", "suspended"}:
        raise HTTPException(
            status_code=403,
            detail=f"Organization status '{organization.status}' is not permitted on this endpoint",
        )
    if user.totp_secret_encrypted_v2:
        raise HTTPException(status_code=409, detail="TOTP is already configured for this account")
    secret = security.generate_totp_secret()
    user.totp_secret_encrypted_v2 = encrypt_totp_secret(secret)
    db.add(user)
    await db.commit()
    await db.refresh(user)

    setup_token = _issue_setup_token(user.id, user.organization_id, stage="totp_setup")
    provisioning_uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name="Nokvo One")
    return NokvoOneTOTPSetupResponse(
        email=user.email,
        secret=secret,
        uri=provisioning_uri,
        setup_token=setup_token,
    )


@router.post("/mfa/totp/verify", response_model=NokvoOneSessionResponse)
@limiter.limit("20/hour")
async def nokvo_one_session_totp_verify(
    request: Request,
    payload: NokvoOneLoginTOTPRequest,
    user: OrganizationUser = Depends(deps.get_current_organization_user),
    db: AsyncSession = Depends(deps.get_db),
):
    """Verify TOTP for the current session and return an MFA-elevated token."""
    if not user.totp_secret_encrypted_v2:
        raise HTTPException(status_code=400, detail="TOTP has not been configured")
    try:
        secret = decrypt_totp_secret(user.totp_secret_encrypted_v2)
    except TOTPDecryptionError as exc:
        raise HTTPException(status_code=500, detail="TOTP secret could not be decrypted") from exc
    if not security.verify_totp(secret, payload.code):
        raise HTTPException(status_code=401, detail="Invalid TOTP code")

    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None or organization.product_tier != "nokvo_one":
        raise HTTPException(status_code=403, detail="Not a Nokvo One organization")
    if organization.status == "pending_totp":
        organization.status = "active"
        db.add(organization)
    if user.status in {"invited", "pending_totp"}:
        user.status = "active"
    user.mfa_required = True
    db.add(user)
    await db.flush()
    return await _issue_full_session(db, request, user, organization, mfa_completed=True)


# ─────────── Google auth (sign-in AND sign-up) ───────────


@router.get("/config")
async def nokvo_one_config():
    ambience_urls: list[str] = []
    if settings.NOKVO_CALL_CENTER_AMBIENCE_ENABLED:
        try:
            from pathlib import Path

            ambience_dir = (
                Path(__file__).resolve().parents[1]
                / "assets"
                / "audio"
                / "call_center_ambience"
            )
            if ambience_dir.exists():
                ambience_urls = sorted(
                    f"/assets/audio/call_center_ambience/{p.name}"
                    for p in ambience_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in {".mp3", ".ogg", ".wav"}
                )
        except Exception:
            ambience_urls = []

    return {
        "google_client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "google_login_enabled": bool(settings.GOOGLE_OAUTH_CLIENT_ID),
        "onboarding_v2_enabled": bool(settings.NOKVO_ONBOARDING_V2),
        "nokvo_connect_enabled": bool(settings.NOKVO_CONNECT_ENABLED),
        "kb_document_upload_enabled": bool(settings.NOKVO_KB_DOCUMENT_UPLOAD_ENABLED),
        "call_center_ambience": {
            "enabled": bool(settings.NOKVO_CALL_CENTER_AMBIENCE_ENABLED) and bool(ambience_urls),
            "volume": float(settings.NOKVO_CALL_CENTER_AMBIENCE_VOLUME),
            "urls": ambience_urls,
        },
    }


@router.post("/google/login")
@limiter.limit("10/minute")
async def nokvo_one_google_login(
    request: Request,
    payload: NokvoOneGoogleLoginRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    """
    Google OAuth for Nokvo One — handles both sign-in and sign-up.

    - If a Nokvo One org exists for the domain and the user is provisioned, this signs them in.
    - If no Nokvo One org exists for the domain, this creates one (Google identity acts as the
      verified admin signup). Org status starts at pending_totp; Nokvo activation still required
      before calling unlocks.
    - If the org exists but the Google account is not provisioned, returns 403 (ask admin to invite).
    """
    try:
        identity = await GoogleOAuthService.verify_id_token(payload.id_token)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=401, detail=_safe_detail(exc)) from exc

    email = normalize_email(identity["email"])
    try:
        validate_work_email(email)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=_safe_detail(exc)) from exc

    domain = extract_email_domain(email)

    # Identify the account by EMAIL (globally unique), NOT by domain. Multiple
    # organizations may share an email domain (e.g. gmail.com), so a domain match
    # must never route a new Google user into someone else's org or block them.
    user = (
        await db.execute(select(OrganizationUser).where(OrganizationUser.email == email))
    ).scalars().first()

    if user is None:
        # ── New org via Google signup ─────────────────────────────────────────
        await _enforce_signup_attempt_quotas(db, email, domain)

        new_org_id = uuid.uuid4()
        org_name = identity.get("full_name") or domain.split(".")[0].capitalize()

        # NO provisioning here — the org is created in ``pending_payment`` and
        # resources are provisioned after the Razorpay payment succeeds.
        organization = Organization(
            id=new_org_id,
            name=org_name,
            admin_email=email,
            admin_name=identity.get("full_name"),
            email_domain=domain,
            region="southindia",
            environment="staging",
            call_type="inbound",
            language="en-IN",
            plan_type=None,
            product_tier="nokvo_one",
            status="pending_payment",
            calling_enabled=False,
            stores_pii=True,
            record_calls=False,
            create_resource_group=False,
            twilio_auto_provision=False,
            industry=None,
            country_code="IN",
        )
        db.add(organization)
        await db.flush()

        user = OrganizationUser(
            id=uuid.uuid4(),
            organization_id=organization.id,
            email=email,
            full_name=identity.get("full_name"),
            role="admin",
            status="active",
            auth_provider="google",
            mfa_required=False,
            email_verified=True,
        )
        db.add(user)
        await db.commit()
        await db.refresh(organization)
        await db.refresh(user)

        # Google admins are MFA-exempt, so we still mint a session (the dashboard
        # stays gated because the org is ``pending_payment`` — not an allowed
        # status), plus a payment_token so the frontend can run the payment step.
        # ``activate_and_provision`` flips the org to ``active`` after payment.
        session = await _issue_full_session(db, request, user, organization, mfa_completed=False)
        session_payload = session.model_dump(mode="json")
        session_payload["created_via_google"] = True
        session_payload["payment_required"] = True
        session_payload["organization_id"] = str(organization.id)
        session_payload["payment_token"] = _issue_setup_token(
            user.id, organization.id, stage="payment", ttl_minutes=60
        )
        return session_payload

    # ── Existing account → sign in (resolved by email, above) ─────────────────
    organization = (
        await db.execute(select(Organization).where(Organization.id == user.organization_id))
    ).scalars().first()
    if organization is None or organization.product_tier != "nokvo_one":
        raise HTTPException(status_code=404, detail="Organization not found")
    if organization.status == "suspended":
        raise HTTPException(status_code=403, detail="Organization is suspended")
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="User account is disabled")

    # Resume: a returning Google user who hasn't finished paying → back to payment.
    if organization.status == "pending_payment":
        session = await _issue_full_session(db, request, user, organization, mfa_completed=False)
        session_payload = session.model_dump(mode="json")
        session_payload["payment_required"] = True
        session_payload["organization_id"] = str(organization.id)
        session_payload["payment_token"] = _issue_setup_token(
            user.id, organization.id, stage="payment", ttl_minutes=60
        )
        return session_payload

    # Resume: paid but mid-onboarding → reopen the wizard at the saved step.
    if organization.status == "onboarding":
        session = await _issue_full_session(db, request, user, organization, mfa_completed=True)
        session_payload = session.model_dump(mode="json")
        session_payload["code"] = "onboarding_required"
        session_payload["onboarding_step"] = organization.onboarding_step or "business_details"
        return session_payload

    user.email_verified = True
    if not user.totp_secret_encrypted_v2:
        user.full_name = user.full_name or identity.get("full_name")
        user.mfa_required = False
        if user.status in {"invited", "pending_totp"}:
            user.status = "active"
        if organization.status == "pending_totp":
            organization.status = "active"
        db.add(user)
        db.add(organization)
        await db.flush()
        return await _issue_full_session(db, request, user, organization, mfa_completed=False)

    user.full_name = user.full_name or identity.get("full_name")
    db.add(user)
    await db.commit()

    temp_access = _issue_login_temp_token(user, organization.id)
    return {
        "code": "totp_verify_required",
        "access_token": temp_access,
        "refresh_token": "pending_totp",
        "token_type": "bearer",
        "mfa_pending": True,
        "email": email,
    }


# ─────────── Refresh / logout / me ───────────


@router.post("/refresh", response_model=Token)
async def nokvo_one_refresh(
    request: Request, payload: RefreshRequest, db: AsyncSession = Depends(deps.get_db)
):
    token_hash = security.hash_refresh_token(payload.refresh_token)
    session_res = await db.execute(
        select(OrganizationSession).where(
            OrganizationSession.refresh_token_hash == token_hash,
            OrganizationSession.revoked_at.is_(None),
            OrganizationSession.expires_at > datetime.now(timezone.utc),
        )
    )
    session = session_res.scalars().first()
    if session is None:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == session.organization_user_id))
    user = user_res.scalars().first()
    if user is None:
        raise HTTPException(status_code=401, detail="User not found")
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None or organization.product_tier != "nokvo_one":
        raise HTTPException(status_code=403, detail="Not a Nokvo One organization")

    session.revoked_at = datetime.now(timezone.utc)
    session.revoke_reason = "rotated"
    db.add(session)

    raw_refresh, new_hash = security.create_refresh_token()
    new_session = OrganizationSession(
        id=uuid.uuid4(),
        organization_user_id=user.id,
        refresh_token_hash=new_hash,
        ip_address=request.client.host if request.client else None,
        user_agent=request.headers.get("user-agent"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.REFRESH_TOKEN_EXPIRE_HOURS),
    )
    db.add(new_session)
    await db.commit()

    access_token = security.create_access_token(
        subject=user.id,
        mfa_completed=False,
        session_id=str(new_session.id),
        token_tier=security.JWT_TIER_ORGANIZATION,
        extra_claims={
            "principal_type": "organization_user",
            "organization_id": str(organization.id),
            "role": user.role,
            "product_tier": organization.product_tier,
        },
    )
    return Token(access_token=access_token, refresh_token=raw_refresh, token_type="bearer")


@router.post("/logout")
async def nokvo_one_logout(
    user: OrganizationUser = Depends(deps.get_current_active_organization_user),
    session_id: str | None = Depends(deps.get_current_org_session_id),
    db: AsyncSession = Depends(deps.get_db),
):
    if not session_id:
        return {"status": "success"}
    res = await db.execute(select(OrganizationSession).where(OrganizationSession.id == session_id))
    session = res.scalars().first()
    if session is not None:
        session.revoked_at = datetime.now(timezone.utc)
        session.revoke_reason = "user_logout"
        db.add(session)
        await db.commit()
    return {"status": "success"}


@router.get("/me", response_model=NokvoOneSessionResponse)
async def nokvo_one_me(
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["onboarding", "active", "suspended"]
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
    token: str = Depends(deps.oauth2_scheme),
):
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return NokvoOneSessionResponse(
        access_token=token,
        refresh_token="",
        token_type="bearer",
        user=NokvoOneUserResponse.model_validate(user),
        organization=_organization_response(organization),
    )


@router.get("/business-template/options", response_model=list[NokvoOneBusinessTemplateOptionResponse])
async def nokvo_one_business_template_options():
    return [NokvoOneBusinessTemplateOptionResponse(**item) for item in business_type_options()]


@router.get("/business-template", response_model=NokvoOneBusinessTemplateOptionResponse)
async def nokvo_one_current_business_template(
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["active", "suspended"]
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    tenant_res = await _tenant_resources_for_org(db, organization.id)
    return _resolved_business_template(organization, tenant_res)


@router.post("/business-template", response_model=NokvoOneBusinessTemplateSaveResponse)
async def nokvo_one_save_business_template(
    payload: NokvoOneBusinessTemplateRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["active", "suspended"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    organization.industry = payload.business_type
    db.add(organization)
    await db.commit()
    await db.refresh(organization)

    tenant_res = await _tenant_resources_for_org(db, organization.id)
    provider_status, questions_changed = ensure_tool_flow_questions(tenant_res.provider_status, organization.industry)
    if questions_changed:
        tenant_res.provider_status = provider_status
        flag_modified(tenant_res, "provider_status")
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
    invalidate_runtime_bundle(tenant_res.tenant_id)
    return NokvoOneBusinessTemplateSaveResponse(
        organization=_organization_response(organization),
        business_template=_resolved_business_template(organization, tenant_res),
    )


@router.get("/business-template/field-catalog/{schema_key}")
async def nokvo_one_get_field_catalog(
    schema_key: str,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["active", "suspended"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    """The selectable field palette for a record (tab): each field with the agent's
    question + whether it's currently selected. The admin picks a subset; the agent
    asks EXACTLY the selected fields. Used by the onboarding / Leads / Site-Visit pickers."""
    schema_key = schema_key.strip().lower()
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    config = business_type_config(organization.industry)
    if config is None:
        raise HTTPException(status_code=409, detail="Business Type is not selected")
    if schema_key not in (config.get("schemas") or {}):
        raise HTTPException(status_code=400, detail="This field group is not available for the selected Business Type")

    tenant_res = await _tenant_resources_for_org(db, organization.id)
    overrides = dict((tenant_res.provider_status or {}).get("business_template_schema_overrides") or {})
    effective = overrides.get(schema_key) or (config.get("schemas") or {}).get(schema_key) or []
    selected = {str(f.get("key")): f for f in effective if isinstance(f, dict)}

    palette = field_catalog_for(organization.industry, schema_key)
    palette_keys = {f["key"] for f in palette}
    # Surface any custom fields the admin already added that aren't in the palette,
    # so the picker shows the full current selection.
    for key, f in selected.items():
        if key not in palette_keys:
            palette.append({"key": key, "label": f.get("label") or key, "type": f.get("type") or "text", "required": bool(f.get("required"))})

    fields = []
    for f in palette:
        key = f["key"]
        is_sel = key in selected
        label = (selected.get(key, {}).get("label")) or f["label"]
        kind = _kind_for_field(f)
        fields.append({
            "key": key,
            "label": label,
            "type": f.get("type") or "text",
            "kind": kind,
            "agent_question": _question_for_kind(kind, label, "en"),
            "selected": is_sel,
            "required": bool(selected[key].get("required")) if is_sel else bool(f.get("required")),
            "custom": key not in palette_keys,
        })
    return {"schema_key": schema_key, "fields": fields}


@router.patch("/business-template/schemas/{schema_key}", response_model=NokvoOneBusinessTemplateOptionResponse)
async def nokvo_one_update_business_template_schema(
    schema_key: str,
    payload: NokvoOneBusinessSchemaUpdateRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["active", "suspended"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    schema_key = schema_key.strip().lower()
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    config = business_type_config(organization.industry)
    if config is None:
        raise HTTPException(status_code=409, detail="Business Type is not selected")
    if schema_key not in (config.get("schemas") or {}):
        raise HTTPException(status_code=400, detail="This field group is not available for the selected Business Type")

    tenant_res = await _tenant_resources_for_org(db, organization.id)
    provider_status = dict(tenant_res.provider_status or {})
    overrides = dict(provider_status.get("business_template_schema_overrides") or {})
    overrides[schema_key] = [field.model_dump() for field in payload.fields]
    provider_status["business_template_schema_overrides"] = overrides
    provider_status, _ = ensure_tool_flow_questions(provider_status, organization.industry)
    tenant_res.provider_status = provider_status
    flag_modified(tenant_res, "provider_status")
    db.add(tenant_res)
    await db.commit()
    await db.refresh(tenant_res)
    invalidate_runtime_bundle(tenant_res.tenant_id)

    return _resolved_business_template(organization, tenant_res)


# ─────────── Custom tabs ───────────


def _custom_tab_admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["active", "suspended"],
        allowed_roles=["admin", "manager"],
    )


def _custom_tab_read_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["active", "suspended"],
    )


@router.get(
    "/business-template/custom-tabs",
    response_model=list[NokvoOneCustomTabResponse],
)
async def nokvo_one_list_custom_tabs(
    user: OrganizationUser = Depends(_custom_tab_read_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _tenant_resources_for_org(db, user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    return [NokvoOneCustomTabResponse(**spec) for spec in custom_tabs_from_overrides(provider_status)]


@router.post(
    "/business-template/custom-tabs",
    response_model=list[NokvoOneCustomTabResponse],
    status_code=status.HTTP_201_CREATED,
)
async def nokvo_one_create_custom_tab(
    payload: NokvoOneCustomTabCreateRequest,
    user: OrganizationUser = Depends(_custom_tab_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    try:
        slug = normalize_custom_tab_slug(payload.slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc

    tenant_res = await _tenant_resources_for_org(db, user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    existing = custom_tabs_from_overrides(provider_status)
    if any(entry["slug"] == slug for entry in existing):
        raise HTTPException(status_code=409, detail="A custom tab with this slug already exists")
    if len(existing) >= 8:
        raise HTTPException(status_code=400, detail="Custom-tab limit reached (max 8 per org)")

    vocab = (
        payload.status_vocabulary.model_dump() if payload.status_vocabulary
        else default_custom_tab_status_vocabulary()
    )
    new_entry = {
        "slug": slug,
        "label": payload.label.strip(),
        "fields": [field.model_dump() for field in payload.fields],
        "status_vocabulary": vocab,
        "search_keys": list(payload.search_keys or []),
    }
    next_tabs = list(existing) + [new_entry]
    provider_status["business_template_custom_tabs"] = next_tabs
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    provider_status, _ = ensure_tool_flow_questions(
        provider_status,
        organization.industry if organization else None,
    )
    tenant_res.provider_status = provider_status
    flag_modified(tenant_res, "provider_status")
    db.add(tenant_res)
    await db.commit()
    await db.refresh(tenant_res)
    invalidate_runtime_bundle(tenant_res.tenant_id)

    return [NokvoOneCustomTabResponse(**spec) for spec in custom_tabs_from_overrides(tenant_res.provider_status)]


@router.delete(
    "/business-template/custom-tabs/{slug}",
    response_model=list[NokvoOneCustomTabResponse],
)
async def nokvo_one_delete_custom_tab(
    slug: str,
    user: OrganizationUser = Depends(_custom_tab_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    try:
        slug = normalize_custom_tab_slug(slug)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc

    tenant_res = await _tenant_resources_for_org(db, user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    existing = custom_tabs_from_overrides(provider_status)
    next_tabs = [entry for entry in existing if entry["slug"] != slug]
    if len(next_tabs) == len(existing):
        raise HTTPException(status_code=404, detail="Custom tab not found")
    provider_status["business_template_custom_tabs"] = next_tabs
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    provider_status, _ = ensure_tool_flow_questions(
        provider_status,
        organization.industry if organization else None,
    )
    tenant_res.provider_status = provider_status
    flag_modified(tenant_res, "provider_status")
    db.add(tenant_res)
    await db.commit()
    await db.refresh(tenant_res)
    invalidate_runtime_bundle(tenant_res.tenant_id)

    return [NokvoOneCustomTabResponse(**spec) for spec in custom_tabs_from_overrides(tenant_res.provider_status)]


@router.get("/me/provisioning", response_model=NokvoOneProvisioningSummary)
async def nokvo_one_provisioning_state(
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["active", "suspended"]
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_resources_for_org(db, user.organization_id)
    return _summary_from_tenant_resources(tr)
