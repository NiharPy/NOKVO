from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from urllib.parse import quote_plus
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse
import pyotp
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.config import settings
from app.core.email_policy import extract_email_domain, normalize_email, validate_work_email
from app.core import security
from app.models.organization import Organization
from app.models.organization_session import OrganizationSession
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.schemas.organization_auth import (
    OrganizationCRMConnectRequest,
    OrganizationCRMConnectResponse,
    OrganizationCRMProviderResponse,
    OrganizationCRMStatusResponse,
    OrganizationDatabaseConnectRequest,
    OrganizationDatabaseConnectResponse,
    OrganizationDatabaseIndexRequest,
    OrganizationDatabaseIndexResponse,
    OrganizationDatabaseProviderResponse,
    OrganizationDatabaseStatusResponse,
    GoogleOAuthLoginRequest,
    OrganizationLoginResponse,
    OrganizationMemberCreate,
    OrganizationMemberUpdate,
    OrganizationSummaryResponse,
    OrganizationTOTPSetupResponse,
    OrganizationTOTPVerifyRequest,
    OrganizationUserResponse,
)
from app.services.crm_integration_service import CRMIntegrationService
from app.schemas.token import RefreshRequest, Token
from app.services.database_integration_service import DatabaseIntegrationService
from app.services.google_oauth_service import GoogleOAuthError, GoogleOAuthService


router = APIRouter()


def _organization_option(organization: Organization) -> dict:
    return {
        "id": str(organization.id),
        "name": organization.name,
        "admin_email": organization.admin_email,
        "admin_name": organization.admin_name,
        "email_domain": organization.email_domain,
        "environment": organization.environment,
        "region": organization.region,
    }


def _build_zoho_oauth_state(organization_id: str, user_id: str) -> str:
    payload = {
        "organization_id": organization_id,
        "user_id": user_id,
        "exp": int((datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()),
    }
    payload_bytes = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = hmac.new(settings.SECRET_KEY.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    token = base64.urlsafe_b64encode(payload_bytes).decode("utf-8").rstrip("=")
    return f"{token}.{signature}"


def _parse_zoho_oauth_state(state: str) -> dict:
    try:
        encoded_payload, signature = state.split(".", 1)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Invalid Zoho OAuth state") from exc

    padded = encoded_payload + "=" * (-len(encoded_payload) % 4)
    try:
        payload_bytes = base64.urlsafe_b64decode(padded.encode("utf-8"))
    except Exception as exc:
        raise HTTPException(status_code=400, detail="Invalid Zoho OAuth state") from exc

    expected_signature = hmac.new(settings.SECRET_KEY.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected_signature):
        raise HTTPException(status_code=400, detail="Invalid Zoho OAuth state")

    payload = json.loads(payload_bytes.decode("utf-8"))
    if int(payload.get("exp", 0)) < int(datetime.now(timezone.utc).timestamp()):
        raise HTTPException(status_code=400, detail="Zoho OAuth state expired")
    return payload


def _crm_oauth_redirect_url(status_value: str, provider: str, message: str = "") -> str:
    base = settings.EXPECTED_ORIGIN.rstrip("/")
    query = f"crm_oauth={quote_plus(status_value)}&provider={quote_plus(provider)}"
    if message:
        query += f"&message={quote_plus(message)}"
    return f"{base}/?{query}"


def _build_org_access_token(user: OrganizationUser, session_id: str) -> str:
    return security.create_access_token(
        subject=user.id,
        mfa_completed=True,
        session_id=session_id,
        extra_claims={
            "principal_type": "organization_user",
            "organization_id": str(user.organization_id),
            "role": user.role,
        },
    )


def _build_org_temp_token(user: OrganizationUser) -> str:
    return security.create_access_token(
        subject=user.id,
        mfa_completed=False,
        expires_delta=timedelta(minutes=5),
        extra_claims={
            "principal_type": "organization_user",
            "organization_id": str(user.organization_id),
            "role": user.role,
            "email": user.email,
        },
    )


def _ensure_same_domain(email: str, organization: Organization) -> None:
    member_domain = extract_email_domain(email)
    if not organization.email_domain:
        raise HTTPException(status_code=409, detail="Organization work-email domain is not configured")
    if member_domain != organization.email_domain.lower():
        raise HTTPException(
            status_code=400,
            detail=f"Member email must use the organization work-email domain: {organization.email_domain}",
        )


async def _count_active_admins(db: AsyncSession, organization_id: uuid.UUID) -> int:
    result = await db.execute(
        select(func.count())
        .select_from(OrganizationUser)
        .where(
            OrganizationUser.organization_id == organization_id,
            OrganizationUser.role == "admin",
            OrganizationUser.status != "disabled",
        )
    )
    return int(result.scalar_one())


async def _get_organization_for_user(db: AsyncSession, user: OrganizationUser) -> Organization:
    result = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = result.scalars().first()
    if not organization:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


async def _get_tenant_resources_for_org(db: AsyncSession, organization_id: uuid.UUID) -> TenantResources:
    result = await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
    tenant_res = result.scalars().first()
    if not tenant_res:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")
    return tenant_res


async def _resolve_organization_for_identity(
    db: AsyncSession,
    requested_organization_id: uuid.UUID | None,
    email: str,
    hosted_domain: str | None,
) -> Organization:
    if requested_organization_id:
        organization_result = await db.execute(select(Organization).where(Organization.id == requested_organization_id))
        organization = organization_result.scalars().first()
        if not organization:
            raise HTTPException(status_code=404, detail="Organization not found")
        return organization

    domain = (hosted_domain or extract_email_domain(email)).lower()
    result = await db.execute(select(Organization).where(Organization.email_domain == domain))
    organizations = result.scalars().all()
    if not organizations:
        raise HTTPException(status_code=403, detail="No organization is configured for this Google workspace")
    if len(organizations) > 1:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "organization_selection_required",
                "message": "Multiple organizations use this work-email domain. Select your organization to continue.",
                "organizations": [_organization_option(organization) for organization in organizations],
            },
        )
    return organizations[0]


@router.get("/config")
async def get_organization_auth_config():
    return {
        "google_client_id": settings.GOOGLE_OAUTH_CLIENT_ID,
        "google_login_enabled": bool(settings.GOOGLE_OAUTH_CLIENT_ID),
    }


@router.get("/database/providers", response_model=list[OrganizationDatabaseProviderResponse])
async def get_organization_database_providers(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
):
    return [OrganizationDatabaseProviderResponse(**item) for item in DatabaseIntegrationService.provider_options()]


@router.get("/crm/providers", response_model=list[OrganizationCRMProviderResponse])
async def get_organization_crm_providers(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
):
    return [OrganizationCRMProviderResponse(**item) for item in CRMIntegrationService.provider_options()]


@router.get("/crm/zoho/authorize")
async def get_organization_zoho_authorize_url(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
):
    if not settings.ZOHO_CLIENT_ID or not settings.ZOHO_REDIRECT_URI or not settings.ZOHO_ACCOUNTS_URL:
        raise HTTPException(status_code=503, detail="Zoho OAuth is not configured")

    state = _build_zoho_oauth_state(str(current_user.organization_id), str(current_user.id))
    scopes = ",".join(
        [
            "ZohoCRM.modules.ALL",
            "ZohoCRM.settings.ALL",
            "ZohoCRM.org.READ",
            "ZohoSearch.securesearch.READ",
            "Desk.tickets.ALL",
            "Desk.basic.READ",
        ]
    )
    auth_url = (
        f"{settings.ZOHO_ACCOUNTS_URL.rstrip('/')}/oauth/v2/auth"
        f"?scope={quote_plus(scopes)}"
        f"&client_id={quote_plus(settings.ZOHO_CLIENT_ID)}"
        f"&response_type=code"
        f"&access_type=offline"
        f"&prompt=consent"
        f"&redirect_uri={quote_plus(settings.ZOHO_REDIRECT_URI)}"
        f"&state={quote_plus(state)}"
    )
    return {"provider": "zoho", "auth_url": auth_url}


@router.post("/google/login", response_model=OrganizationLoginResponse)
async def google_login(
    request: Request,
    payload: GoogleOAuthLoginRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    try:
        identity = await GoogleOAuthService.verify_id_token(payload.id_token)
    except GoogleOAuthError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc

    email = normalize_email(identity["email"])
    try:
        validate_work_email(email)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc

    organization = await _resolve_organization_for_identity(
        db,
        payload.organization_id,
        email,
        identity.get("hosted_domain"),
    )
    if not organization.admin_email or not organization.email_domain:
        raise HTTPException(status_code=409, detail="Organization is not configured for Google OAuth login")

    if identity.get("hosted_domain") and identity["hosted_domain"].lower() != organization.email_domain.lower():
        raise HTTPException(status_code=403, detail="Google account hosted domain does not match the organization")

    result = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.organization_id == organization.id,
            OrganizationUser.email == email,
        )
    )
    user = result.scalars().first()
    if not user:
        raise HTTPException(status_code=403, detail="This Google account is not provisioned for the organization")
    if user.status == "disabled":
        raise HTTPException(status_code=403, detail="Organization user account is disabled")

    _ensure_same_domain(email, organization)

    user.full_name = user.full_name or identity.get("full_name")
    user.email_verified = True
    db.add(user)
    await db.commit()
    await db.refresh(user)

    return OrganizationLoginResponse(
        access_token=_build_org_temp_token(user),
        refresh_token="pending_mfa",
        token_type="bearer",
        user=OrganizationUserResponse.model_validate(user),
        organization=OrganizationSummaryResponse.model_validate(organization),
    )


@router.post("/mfa/totp/setup", response_model=OrganizationTOTPSetupResponse)
async def setup_organization_totp(
    current_user: OrganizationUser = Depends(deps.get_current_organization_user),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization_for_user(db, current_user)
    _ensure_same_domain(current_user.email, organization)
    if current_user.totp_secret_encrypted:
        raise HTTPException(status_code=400, detail="TOTP already set up")

    secret = security.generate_totp_secret()
    current_user.totp_secret_encrypted = secret
    db.add(current_user)
    await db.commit()

    return OrganizationTOTPSetupResponse(
        email=current_user.email,
        secret=secret,
        uri=pyotp.totp.TOTP(secret).provisioning_uri(
            name=current_user.email,
            issuer_name=f"NOKVO {organization.name}",
        ),
    )


@router.post("/mfa/totp/verify", response_model=OrganizationLoginResponse)
async def verify_organization_totp(
    request: Request,
    payload: OrganizationTOTPVerifyRequest,
    current_user: OrganizationUser = Depends(deps.get_current_organization_user),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization_for_user(db, current_user)
    _ensure_same_domain(current_user.email, organization)
    if not current_user.totp_secret_encrypted:
        raise HTTPException(status_code=400, detail="TOTP not set up")
    if not security.verify_totp(current_user.totp_secret_encrypted, payload.token):
        raise HTTPException(status_code=401, detail="Invalid TOTP token")

    raw_refresh, token_hash = security.create_refresh_token()
    session = OrganizationSession(
        id=uuid.uuid4(),
        organization_user_id=current_user.id,
        refresh_token_hash=token_hash,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
    )

    if current_user.status == "invited":
        current_user.status = "active"
    current_user.email_verified = True
    current_user.last_login_at = datetime.now(timezone.utc)
    current_user.last_login_ip = request.client.host

    db.add(session)
    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)

    return OrganizationLoginResponse(
        access_token=_build_org_access_token(current_user, str(session.id)),
        refresh_token=raw_refresh,
        token_type="bearer",
        user=OrganizationUserResponse.model_validate(current_user),
        organization=OrganizationSummaryResponse.model_validate(organization),
    )


@router.post("/refresh", response_model=Token)
async def refresh_organization_token(
    request: Request,
    data: RefreshRequest,
    db: AsyncSession = Depends(deps.get_db),
):
    provided_hash = security.hashlib.sha256(data.refresh_token.encode()).hexdigest()
    result = await db.execute(
        select(OrganizationSession).where(
            OrganizationSession.refresh_token_hash == provided_hash,
            OrganizationSession.revoked_at == None,
            OrganizationSession.expires_at > datetime.now(timezone.utc),
        )
    )
    session = result.scalars().first()
    if not session:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")

    user_result = await db.execute(select(OrganizationUser).where(OrganizationUser.id == session.organization_user_id))
    user = user_result.scalars().first()
    if not user or user.status == "disabled":
        raise HTTPException(status_code=401, detail="Organization user account is unavailable")

    session.revoked_at = datetime.now(timezone.utc)
    session.revoke_reason = "rotated"
    db.add(session)

    raw_refresh, token_hash = security.create_refresh_token()
    new_session = OrganizationSession(
        id=uuid.uuid4(),
        organization_user_id=user.id,
        refresh_token_hash=token_hash,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=4),
    )
    db.add(new_session)
    await db.commit()

    return {
        "access_token": _build_org_access_token(user, str(new_session.id)),
        "refresh_token": raw_refresh,
        "token_type": "bearer",
    }


@router.post("/logout")
async def logout_organization_user(
    current_user: OrganizationUser = Depends(deps.get_current_active_organization_user),
    session_id: str | None = Depends(deps.get_current_org_session_id),
    db: AsyncSession = Depends(deps.get_db),
):
    if session_id:
        result = await db.execute(select(OrganizationSession).where(OrganizationSession.id == session_id))
        session = result.scalars().first()
        if session and session.organization_user_id == current_user.id:
            session.revoked_at = datetime.now(timezone.utc)
            session.revoke_reason = "user_logout"
            db.add(session)
            await db.commit()
    return {"status": "success"}


@router.get("/me")
async def get_current_organization_user_profile(
    current_user: OrganizationUser = Depends(deps.get_current_active_organization_user),
    organization: Organization = Depends(deps.get_current_organization),
):
    return {
        "user": OrganizationUserResponse.model_validate(current_user),
        "organization": OrganizationSummaryResponse.model_validate(organization),
    }


@router.get("/database/status", response_model=OrganizationDatabaseStatusResponse)
async def get_organization_database_status(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = tenant_res.provider_status or {}
    secret_ref = ((tenant_res.secret_refs or {}).get("db_connection_string") or {}).get("secret_name")
    return OrganizationDatabaseStatusResponse(
        provider=provider_status.get("db_provider"),
        status=provider_status.get("db_status", "not_connected"),
        secret_ref=secret_ref,
        database_name=provider_status.get("db_database_name"),
        selected_sources=provider_status.get("db_selected_sources", []),
        indexed_points=int(provider_status.get("db_indexed_points", 0) or 0),
    )


@router.get("/crm/status", response_model=OrganizationCRMStatusResponse)
async def get_organization_crm_status(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = tenant_res.provider_status or {}
    secret_ref = ((tenant_res.secret_refs or {}).get("crm_connection") or {}).get("secret_name")
    return OrganizationCRMStatusResponse(
        provider=provider_status.get("crm_provider"),
        status=provider_status.get("crm_status", "not_connected"),
        secret_ref=secret_ref,
        account_name=provider_status.get("crm_account_name"),
        indexed_points=int(provider_status.get("crm_indexed_points", 0) or 0),
        module_count=int(provider_status.get("crm_module_count", 0) or 0),
        action_count=int(provider_status.get("crm_action_count", 0) or 0),
        folder_path=provider_status.get("crm_folder_path"),
    )


@router.post("/database/connect", response_model=OrganizationDatabaseConnectResponse)
async def connect_organization_database(
    payload: OrganizationDatabaseConnectRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)

    try:
        scan_result = await DatabaseIntegrationService.scan_schema(payload.provider, payload.connection_string)
        secret_ref = await DatabaseIntegrationService.store_connection_secret(
            tenant_res,
            scan_result.provider,
            payload.connection_string,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider_status = dict(tenant_res.provider_status or {})
    provider_status.update(
        {
            "db_status": "schema_scanned",
            "db_provider": scan_result.provider,
            "db_database_name": scan_result.database_name,
            "db_schema_snapshot": scan_result.schema,
            "db_connected_at": datetime.now(timezone.utc).isoformat(),
            "db_last_error": None,
        }
    )
    tenant_res.provider_status = provider_status
    db.add(tenant_res)
    await db.commit()

    return OrganizationDatabaseConnectResponse(
        provider=scan_result.provider,
        database_name=scan_result.database_name,
        secret_ref=secret_ref,
        status="schema_scanned",
        schema_snapshot=scan_result.schema,
    )


@router.post("/crm/connect", response_model=OrganizationCRMConnectResponse)
async def connect_organization_crm(
    payload: OrganizationCRMConnectRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    credentials = {
        "account_url": payload.account_url,
        "api_domain": payload.api_domain,
        "access_token": payload.access_token,
        "refresh_token": payload.refresh_token,
    }

    try:
        scan_result = await CRMIntegrationService.scan_schema(payload.provider, credentials)
        secret_ref = await CRMIntegrationService.store_connection_secret(
            tenant_res,
            scan_result.provider,
            credentials,
        )
        index_result = await CRMIntegrationService.index_schema_embeddings(tenant_res, scan_result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider_status = dict(tenant_res.provider_status or {})
    provider_status.update(
        {
            "crm_status": "indexed",
            "crm_provider": scan_result.provider,
            "crm_account_name": scan_result.account_name,
            "crm_module_count": len(scan_result.modules),
            "crm_action_count": len(scan_result.actions),
            "crm_schema_snapshot": scan_result.modules,
            "crm_action_snapshot": scan_result.actions,
            "crm_folder_path": scan_result.folder_path,
            "crm_indexed_points": index_result["indexed_points"],
            "crm_connected_at": datetime.now(timezone.utc).isoformat(),
            "crm_last_error": None,
        }
    )
    tenant_res.provider_status = provider_status
    db.add(tenant_res)
    await db.commit()

    return OrganizationCRMConnectResponse(
        provider=scan_result.provider,
        account_name=scan_result.account_name,
        status="indexed",
        secret_ref=secret_ref,
        folder_path=scan_result.folder_path,
        indexed_points=index_result["indexed_points"],
        module_count=len(scan_result.modules),
        action_count=len(scan_result.actions),
        modules=scan_result.modules,
        actions=scan_result.actions,
    )


@router.get("/crm/zoho/callback")
async def zoho_crm_oauth_callback(
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(deps.get_db),
):
    print(
        "[Zoho OAuth] callback received",
        {
            "has_code": bool(code),
            "has_state": bool(state),
            "error": error,
        },
    )
    if error:
        redirect_url = _crm_oauth_redirect_url("error", "zoho", error)
        print("[Zoho OAuth] redirecting with error", {"redirect_url": redirect_url})
        return RedirectResponse(redirect_url, status_code=302)
    if not code or not state:
        redirect_url = _crm_oauth_redirect_url("error", "zoho", "missing_code_or_state")
        print("[Zoho OAuth] redirecting with missing code/state", {"redirect_url": redirect_url})
        return RedirectResponse(redirect_url, status_code=302)

    try:
        state_payload = _parse_zoho_oauth_state(state)
        print("[Zoho OAuth] parsed state", state_payload)
        organization_id = uuid.UUID(state_payload["organization_id"])
        tenant_res = await _get_tenant_resources_for_org(db, organization_id)

        token_payload = await CRMIntegrationService.exchange_zoho_authorization_code(code)
        credentials = {
            "access_token": token_payload.get("access_token"),
            "refresh_token": token_payload.get("refresh_token"),
            "api_domain": token_payload.get("api_domain"),
        }
        scan_result = await CRMIntegrationService.scan_schema("zoho", credentials)
        secret_ref = await CRMIntegrationService.store_connection_secret(tenant_res, "zoho", credentials)
        index_result = await CRMIntegrationService.index_schema_embeddings(tenant_res, scan_result)

        provider_status = dict(tenant_res.provider_status or {})
        provider_status.update(
            {
                "crm_status": "indexed",
                "crm_provider": "zoho",
                "crm_account_name": scan_result.account_name,
                "crm_module_count": len(scan_result.modules),
                "crm_action_count": len(scan_result.actions),
                "crm_schema_snapshot": scan_result.modules,
                "crm_action_snapshot": scan_result.actions,
                "crm_folder_path": scan_result.folder_path,
                "crm_indexed_points": index_result["indexed_points"],
                "crm_secret_ref": secret_ref,
                "crm_connected_at": datetime.now(timezone.utc).isoformat(),
                "crm_last_error": None,
            }
        )
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
    except Exception as exc:
        redirect_url = _crm_oauth_redirect_url("error", "zoho", str(exc))
        print("[Zoho OAuth] callback failed", {"error": str(exc), "redirect_url": redirect_url})
        return RedirectResponse(redirect_url, status_code=302)

    redirect_url = _crm_oauth_redirect_url("success", "zoho")
    print("[Zoho OAuth] callback succeeded", {"redirect_url": redirect_url})
    return RedirectResponse(redirect_url, status_code=302)


@router.post("/database/index", response_model=OrganizationDatabaseIndexResponse)
async def index_organization_database_selection(
    payload: OrganizationDatabaseIndexRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)

    try:
        stored_provider, connection_string = await DatabaseIntegrationService.load_connection_secret(tenant_res)
        provider = payload.provider or stored_provider
        serialized_selections = [selection.model_dump(by_alias=True) for selection in payload.selections]
        result = await DatabaseIntegrationService.index_selected_columns(
            tenant_res,
            provider=provider,
            connection_string=connection_string,
            selections=serialized_selections,
            row_limit=payload.row_limit,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider_status = dict(tenant_res.provider_status or {})
    provider_status.update(
        {
            "db_status": "indexed",
            "db_provider": payload.provider or stored_provider,
            "db_selected_sources": serialized_selections,
            "db_indexed_points": result["indexed_points"],
            "db_last_indexed_at": datetime.now(timezone.utc).isoformat(),
            "db_last_error": None,
        }
    )
    tenant_res.provider_status = provider_status
    db.add(tenant_res)
    await db.commit()

    return OrganizationDatabaseIndexResponse(
        status="indexed",
        provider=payload.provider or stored_provider,
        indexed_points=result["indexed_points"],
        tables=result["tables"],
        column_value_count=result["column_value_count"],
        row_limit=result["row_limit"],
    )


@router.get("/members", response_model=list[OrganizationUserResponse])
async def list_organization_members(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    result = await db.execute(
        select(OrganizationUser)
        .where(OrganizationUser.organization_id == current_user.organization_id)
        .order_by(OrganizationUser.created_at.asc())
    )
    return [OrganizationUserResponse.model_validate(member) for member in result.scalars().all()]


@router.post("/members", response_model=OrganizationUserResponse, status_code=status.HTTP_201_CREATED)
async def create_organization_member(
    payload: OrganizationMemberCreate,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    organization: Organization = Depends(deps.get_current_organization),
    db: AsyncSession = Depends(deps.get_db),
):
    email = normalize_email(payload.email)
    _ensure_same_domain(email, organization)

    existing_result = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.organization_id == current_user.organization_id,
            OrganizationUser.email == email,
        )
    )
    existing_user = existing_result.scalars().first()
    if existing_user:
        raise HTTPException(status_code=409, detail="Organization member already exists for this email")

    user = OrganizationUser(
        id=uuid.uuid4(),
        organization_id=current_user.organization_id,
        invited_by=current_user.id,
        email=email,
        full_name=payload.full_name,
        role=payload.role,
        status="invited",
        auth_provider="google",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return OrganizationUserResponse.model_validate(user)


@router.patch("/members/{member_id}", response_model=OrganizationUserResponse)
async def update_organization_member(
    member_id: uuid.UUID,
    payload: OrganizationMemberUpdate,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    result = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.id == member_id,
            OrganizationUser.organization_id == current_user.organization_id,
        )
    )
    member = result.scalars().first()
    if not member:
        raise HTTPException(status_code=404, detail="Organization member not found")

    new_role = payload.role if payload.role is not None else member.role
    new_status = payload.status if payload.status is not None else member.status
    would_remove_admin = member.role == "admin" and (new_role != "admin" or new_status == "disabled")
    if would_remove_admin:
        active_admins = await _count_active_admins(db, current_user.organization_id)
        if active_admins <= 1:
            raise HTTPException(status_code=400, detail="Organization must retain at least one active admin")

    if payload.full_name is not None:
        member.full_name = payload.full_name
    if payload.role is not None:
        member.role = payload.role
    if payload.status is not None:
        member.status = payload.status

    db.add(member)
    await db.commit()
    await db.refresh(member)
    return OrganizationUserResponse.model_validate(member)
