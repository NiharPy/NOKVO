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
from app.services.nokvo_one_business_templates import (
    apply_schema_overrides,
    business_type_config,
    business_type_options,
    custom_tabs_from_overrides,
    default_custom_tab_status_vocabulary,
    normalize_business_type,
    normalize_custom_tab_slug,
)
from app.services.tool_flow_questions import ensure_tool_flow_questions


router = APIRouter()


# ─────────── Helpers ───────────


def _hash_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _issue_setup_token(user_id: uuid.UUID, organization_id: uuid.UUID, stage: str, ttl_minutes: int = 30) -> str:
    """Short-lived JWT used to gate signup-stage flows (email-verified → TOTP setup)."""
    payload = {
        "sub": str(user_id),
        "organization_id": str(organization_id),
        "stage": stage,
        "principal_type": "nokvo_one_signup",
        "exp": datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes),
    }
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=settings.ALGORITHM)


def _decode_setup_token(token: str, expected_stage: str) -> dict:
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
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
    exotel = ps.get("exotel") or {}
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
    exotel = ps.get("exotel") or {}
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

    email_count_res = await db.execute(
        select(EmailVerification).where(
            EmailVerification.email == email,
            EmailVerification.created_at >= window_start,
        )
    )
    email_count = len(email_count_res.scalars().all())
    if email_count >= 3:
        raise HTTPException(
            status_code=429,
            detail="Too many signup attempts for this email in the last 24 hours",
        )

    # Domain quota: join through organization rows since EmailVerification stores the literal email.
    domain_res = await db.execute(
        select(EmailVerification).where(
            EmailVerification.email.like(f"%@{domain}"),
            EmailVerification.created_at >= window_start,
        )
    )
    if len(domain_res.scalars().all()) >= 10:
        raise HTTPException(
            status_code=429,
            detail="Too many signup attempts for this email domain in the last 24 hours",
        )


# ─────────── Signup ───────────


def _sse(event_type: str, data: dict) -> bytes:
    """Format an SSE event line. Each event is one JSON blob on a `data:` line."""
    payload = json.dumps({"event": event_type, **data}, default=str)
    return f"data: {payload}\n\n".encode("utf-8")


@router.post("/signup")
@limiter.limit("5/hour")
async def nokvo_one_signup(
    request: Request,
    payload: NokvoOneSignupRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    """Streaming signup: each provisioning step is reported via Server-Sent Events.

    The HTTP response stays open for ~60-90s while Azure provisions resources.
    Events:
      step       — { name, status, message? }   status ∈ {running, success, failed, skipped_no_*}
      complete   — { provisioning: {...}, organization_id, admin_user_id, email, org_status }
      error      — { step, message }            sent on rollback; no DB row persisted
    """
    email = normalize_email(payload.admin_email)
    domain = extract_email_domain(email)

    # Pre-flight validation — runs BEFORE we open the stream so rejections come back
    # as proper HTTP errors instead of mid-stream events.
    existing_org_by_domain = await _lookup_org_by_domain(db, domain)
    if existing_org_by_domain is not None:
        raise HTTPException(
            status_code=409,
            detail="A Nokvo One organization already exists for this work-email domain",
        )
    await _enforce_signup_attempt_quotas(db, email, domain)
    existing_user = await db.execute(select(OrganizationUser).where(OrganizationUser.email == email))
    if existing_user.scalars().first() is not None:
        raise HTTPException(
            status_code=409,
            detail="An account with this email already exists",
        )

    new_org_id = uuid.uuid4()
    region = payload.region or "southindia"
    org_name = payload.org_name.strip()

    async def event_stream() -> AsyncGenerator[bytes, None]:
        queue: asyncio.Queue[dict] = asyncio.Queue()

        async def on_step(name: str, status_: str, message: str | None = None) -> None:
            await queue.put({"name": name, "status": status_, "message": message})

        async def runner() -> dict:
            return await NokvoOneProvisioningService.provision_or_raise(
                organization_id=new_org_id,
                organization_name=org_name,
                environment="staging",
                region=region,
                on_step=on_step,
            )

        task = asyncio.create_task(runner())

        # Stream step events until the provisioning task finishes.
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, task}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                event = getter.result()
                yield _sse("step", event)
            else:
                getter.cancel()
            if task in done:
                # Drain any remaining queued events emitted just before completion.
                while not queue.empty():
                    yield _sse("step", queue.get_nowait())
                break

        try:
            provision = task.result()
        except NokvoOneProvisioningError as exc:
            yield _sse("error", {"step": exc.step, "message": exc.message})
            return
        except Exception as exc:
            yield _sse("error", {"step": "unknown", "message": str(exc)})
            return

        # Provisioning succeeded — commit DB rows in one transaction.
        try:
            organization = Organization(
                id=new_org_id,
                name=org_name,
                admin_email=email,
                admin_name=payload.admin_name,
                email_domain=domain,
                region=region,
                environment="staging",
                call_type="inbound",
                language=payload.language or "en-IN",
                plan_type="pilot",
                product_tier="nokvo_one",
                status="pending_email_verification",
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
                status="pending_email_verification",
                auth_provider="password",
                password_hash=security.get_password_hash(payload.password),
                mfa_required=True,
                email_verified=False,
            )
            db.add(admin_user)
            await db.flush()

            tenant_resources = TenantResources(
                id=uuid.uuid4(),
                organization_id=organization.id,
                tenant_id=provision["tenant_id"],
                azure_resource_group_name=provision["azure_resource_group_name"],
                azure_region=provision["azure_region"],
                qdrant_collection_name=provision["qdrant_collection_name"],
                qdrant_url_ref=provision["qdrant_url_ref"],
                redis_namespace=provision["redis_namespace"],
                storage_account_name=provision["storage_account_name"],
                storage_container_name=provision["storage_container_name"],
                blob_prefix=provision["blob_prefix"],
                provider_status=provision["provider_status"],
                provisioning_status=provision["provisioning_status"],
                provisioning_steps=provision["provisioning_steps"],
                cleanup_required=False,
            )
            db.add(tenant_resources)
            await db.flush()

            raw_token = secrets.token_urlsafe(32)
            verification = EmailVerification(
                id=uuid.uuid4(),
                organization_id=organization.id,
                organization_user_id=admin_user.id,
                email=email,
                token_hash=_hash_token(raw_token),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=settings.NOKVO_ONE_EMAIL_TOKEN_TTL_HOURS),
            )
            db.add(verification)
            await db.commit()
            await db.refresh(organization)
            await db.refresh(admin_user)
        except Exception as exc:
            await db.rollback()
            yield _sse("error", {"step": "db_commit", "message": str(exc)})
            return

        # Fire-and-forget verification email — failure here does not undo signup.
        asyncio.create_task(
            EmailService.send_verification_email(email, payload.admin_name, organization.name, raw_token)
        )

        response = NokvoOneSignupResponse(
            organization_id=organization.id,
            admin_user_id=admin_user.id,
            email=email,
            org_status=organization.status,
            provisioning=_summary_from_provision_dict(provision),
        )
        yield _sse("complete", json.loads(response.model_dump_json()))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",  # disable nginx proxy buffering
        },
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

    user.status = "invited"  # promoted to active once superadmin approves the org
    org_res = await db.execute(select(Organization).where(Organization.id == org_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if organization.status == "pending_totp":
        organization.status = "pending_approval"
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
        organization.status = "pending_approval"
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
    if not user.email_verified:
        raise HTTPException(status_code=403, detail="Email is not yet verified")

    if not security.verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if not user.totp_secret_encrypted_v2:
        # Edge case: user has password but never finished TOTP setup. Re-issue setup token.
        setup_token = _issue_setup_token(user.id, organization.id, stage="totp_setup")
        raise HTTPException(
            status_code=403,
            detail={
                "code": "totp_setup_required",
                "message": "Complete TOTP setup to continue.",
                "setup_token": setup_token,
            },
        )

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
        claims = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
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
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    email = normalize_email(identity["email"])
    try:
        validate_work_email(email)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    domain = extract_email_domain(email)
    organization = await _lookup_org_by_domain(db, domain)

    if organization is None:
        # ── New org via Google signup ─────────────────────────────────────────
        await _enforce_signup_attempt_quotas(db, email, domain)

        new_org_id = uuid.uuid4()
        org_name = identity.get("full_name") or domain.split(".")[0].capitalize()

        # Fail-closed provisioning; if anything fails, no DB rows persist.
        provision = await _provision_or_503(
            organization_id=new_org_id,
            organization_name=org_name,
            region="southindia",
            environment="staging",
        )

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
            plan_type="pilot",
            product_tier="nokvo_one",
            status="pending_totp",
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
            status="pending_totp",
            auth_provider="google",
            mfa_required=True,
            email_verified=True,
        )
        db.add(user)
        await db.flush()

        tenant_resources = TenantResources(
            id=uuid.uuid4(),
            organization_id=organization.id,
            tenant_id=provision["tenant_id"],
            azure_resource_group_name=provision["azure_resource_group_name"],
            azure_region=provision["azure_region"],
            qdrant_collection_name=provision["qdrant_collection_name"],
            qdrant_url_ref=provision["qdrant_url_ref"],
            redis_namespace=provision["redis_namespace"],
            storage_account_name=provision["storage_account_name"],
            storage_container_name=provision["storage_container_name"],
            blob_prefix=provision["blob_prefix"],
            provider_status=provision["provider_status"],
            provisioning_status=provision["provisioning_status"],
            provisioning_steps=provision["provisioning_steps"],
            cleanup_required=False,
        )
        db.add(tenant_resources)
        await db.commit()
        await db.refresh(user)
        await db.refresh(organization)

        setup_token = _issue_setup_token(user.id, organization.id, stage="totp_setup")
        return {
            "code": "totp_setup_required",
            "message": "Nokvo One organization created. Set up TOTP to finish signup.",
            "setup_token": setup_token,
            "organization_id": str(organization.id),
            "email": email,
            "created_via_google": True,
            "provisioning": _summary_from_provision_dict(provision).model_dump(),
        }

    # ── Existing org path (sign-in) ───────────────────────────────────────────
    if organization.status == "suspended":
        raise HTTPException(status_code=403, detail="Organization is suspended")
    if identity.get("hosted_domain") and identity["hosted_domain"].lower() != (organization.email_domain or "").lower():
        raise HTTPException(status_code=403, detail="Google hosted domain does not match the organization")

    user_res = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.organization_id == organization.id,
            OrganizationUser.email == email,
        )
    )
    user = user_res.scalars().first()
    if user is None:
        raise HTTPException(
            status_code=403,
            detail=(
                "A Nokvo One organization already exists for this domain, but this Google account is "
                "not provisioned. Ask an organization admin to invite you."
            ),
        )
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="User account is disabled")

    user.email_verified = True
    if not user.totp_secret_encrypted_v2:
        setup_token = _issue_setup_token(user.id, organization.id, stage="totp_setup")
        db.add(user)
        await db.commit()
        return {
            "code": "totp_setup_required",
            "message": "Complete TOTP setup to continue.",
            "setup_token": setup_token,
            "organization_id": str(organization.id),
            "email": email,
            "created_via_google": False,
        }

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
    token_hash = hashlib.sha256(payload.refresh_token.encode()).hexdigest()
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
        mfa_completed=True,
        session_id=str(new_session.id),
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
            allowed_statuses=["pending_approval", "active", "suspended"]
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
            allowed_statuses=["pending_approval", "active", "suspended"]
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
            allowed_statuses=["pending_approval", "active", "suspended"],
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
    return NokvoOneBusinessTemplateSaveResponse(
        organization=_organization_response(organization),
        business_template=_resolved_business_template(organization, tenant_res),
    )


@router.patch("/business-template/schemas/{schema_key}", response_model=NokvoOneBusinessTemplateOptionResponse)
async def nokvo_one_update_business_template_schema(
    schema_key: str,
    payload: NokvoOneBusinessSchemaUpdateRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active", "suspended"],
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

    return _resolved_business_template(organization, tenant_res)


# ─────────── Custom tabs ───────────


def _custom_tab_admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["pending_approval", "active", "suspended"],
        allowed_roles=["admin", "manager"],
    )


def _custom_tab_read_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["pending_approval", "active", "suspended"],
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
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    return [NokvoOneCustomTabResponse(**spec) for spec in custom_tabs_from_overrides(tenant_res.provider_status)]


@router.get("/me/provisioning", response_model=NokvoOneProvisioningSummary)
async def nokvo_one_provisioning_state(
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active", "suspended"]
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_resources_for_org(db, user.organization_id)
    return _summary_from_tenant_resources(tr)
