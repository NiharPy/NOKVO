from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
from urllib.parse import quote_plus
import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile, WebSocket, status
from fastapi.responses import PlainTextResponse, RedirectResponse
import jwt
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
from app.models.mcp_tool_registry import MCPToolRegistryEntry
from app.models.tenant_resources import TenantResources
from app.schemas.organization_auth import (
    OrganizationCRMConnectRequest,
    OrganizationCRMConnectResponse,
    OrganizationCRMProviderResponse,
    OrganizationCRMStatusResponse,
    OrganizationERPConnectRequest,
    OrganizationERPConnectResponse,
    OrganizationERPProviderResponse,
    OrganizationERPStatusResponse,
    OrganizationShippingConnectRequest,
    OrganizationShippingConnectResponse,
    OrganizationShippingProviderResponse,
    OrganizationShippingStatusResponse,
    OrganizationShiprocketAPIResponse,
    OrganizationShiprocketAssignAWBRequest,
    OrganizationShiprocketCreateOrderRequest,
    OrganizationShiprocketPickupRequest,
    OrganizationShiprocketServiceabilityRequest,
    OrganizationShiprocketTrackRequest,
    OrganizationTallyXMLRequest,
    OrganizationTallyXMLResponse,
    OrganizationToolkitDraftResponse,
    OrganizationToolkitBuilderResponse,
    OrganizationToolkitBuildersResponse,
    OrganizationToolkitGenerateRequest,
    OrganizationToolkitRegistryResponse,
    OrganizationToolkitReviewRequest,
    OrganizationAgentDocumentResponse,
    OrganizationAgentDocumentReviewRequest,
    OrganizationAgentDocumentUploadRequest,
    OrganizationAgentDocumentsResponse,
    OrganizationAgentTestAnswerResponse,
    OrganizationAgentTestQueryRequest,
    OrganizationAgentTestRetrievalResponse,
    OrganizationAgentRuntimeChatRequest,
    OrganizationAgentRuntimeChatResponse,
    OrganizationAgentRuntimeStatusResponse,
    OrganizationAgentPhoneLinkRequest,
    OrganizationAgentPhoneLinkResponse,
    OrganizationAgentLatencyTestRequest,
    OrganizationAgentLatencyTestResponse,
    OrganizationZohoDeskConnectResponse,
    OrganizationZohoDeskStatusResponse,
    OrganizationZohoDeskTicketCreateRequest,
    OrganizationZohoDeskTicketResponse,
    OrganizationZohoDeskTicketUpdateRequest,
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
from app.services.erp_integration_service import ERPIntegrationService
from app.services.zoho_desk_service import ZohoDeskService
from app.schemas.token import RefreshRequest, Token
from app.services.database_integration_service import DatabaseIntegrationService
from app.services.google_oauth_service import GoogleOAuthError, GoogleOAuthService
from app.services.shipping_integration_service import ShippingIntegrationService
from app.services.toolkit_generator_service import ToolkitGeneratorService
from app.services.qdrant_service import QdrantService
from app.services.agent_knowledge_service import AgentKnowledgeService
from app.schemas.outbound_campaign import CampaignDetailOut, CampaignOut
from app.services.exotel_bridge_service import ExotelBridgeService, ExotelWebSocketAdapter
from app.services.exotel_service import ExotelService
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline as AgentRuntimeService
from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
from app.services.outbound_campaign_service import OutboundCampaignService


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


def _toolkit_state(provider_status: dict) -> tuple[dict, list]:
    registry = dict(provider_status.get("mcp_tool_registry") or {})
    drafts = list(provider_status.get("toolkit_drafts") or [])
    return registry, drafts


def _toolkit_matches(item: dict, integration_type: str | None, provider: str | None) -> bool:
    if integration_type and item.get("integration_type") != integration_type:
        return False
    if provider and item.get("provider") != provider:
        return False
    return True


def _toolkit_integration_connected(provider_status: dict, integration_type: str, provider: str) -> bool:
    return ToolkitGeneratorService.is_integration_connected(provider_status, integration_type, provider)


def _append_toolkit_audit_event(
    provider_status: dict,
    action: str,
    user_id: uuid.UUID,
    integration_type: str,
    provider: str,
    tool_name: str | None = None,
    draft_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    events = list(provider_status.get("toolkit_audit_events") or [])
    events.append(
        {
            "action": action,
            "user_id": str(user_id),
            "integration_type": integration_type,
            "provider": provider,
            "tool_name": tool_name,
            "draft_id": draft_id,
            "metadata": metadata or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
    )
    provider_status["toolkit_audit_events"] = events[-200:]


async def _get_registered_tool_definitions(
    db: AsyncSession,
    organization_id: uuid.UUID,
    integration_type: str,
    provider: str,
    provider_status: dict | None = None,
) -> list[dict]:
    result = await db.execute(
        select(MCPToolRegistryEntry)
        .where(
            MCPToolRegistryEntry.organization_id == organization_id,
            MCPToolRegistryEntry.integration_type == integration_type,
            MCPToolRegistryEntry.provider == provider,
            MCPToolRegistryEntry.status == "active",
        )
        .order_by(MCPToolRegistryEntry.tool_name.asc())
    )
    entries = result.scalars().all()
    tools: list[dict] = []
    for entry in entries:
        tool = dict(entry.tool_definition or {})
        tool.setdefault("registry_id", str(entry.id))
        tool.setdefault("approved_at", entry.approved_at.isoformat() if entry.approved_at else None)
        tool.setdefault("approved_by", str(entry.approved_by) if entry.approved_by else None)
        tool.setdefault("version", entry.version)
        tools.append(tool)
    if tools:
        return tools

    legacy_registry = (provider_status or {}).get("mcp_tool_registry") or {}
    key = ToolkitGeneratorService.integration_registry_key(integration_type, provider)
    return list(legacy_registry.get(key, [])) if isinstance(legacy_registry, dict) else []


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


def _agent_phone_link_response(tenant_res: TenantResources) -> dict:
    return ExotelService.phone_link_response(tenant_res)


async def _get_tenant_resources_by_agent_phone_link(db: AsyncSession, link_id: str) -> TenantResources | None:
    result = await db.execute(select(TenantResources))
    for tenant_res in result.scalars().all():
        link = dict((tenant_res.provider_status or {}).get("agent_phone_link") or {})
        if link.get("link_id") == link_id and link.get("status") == "linked":
            return tenant_res
    return None


async def _get_tenant_resources_by_tenant_id(db: AsyncSession, tenant_id: str) -> TenantResources | None:
    result = await db.execute(select(TenantResources).where(TenantResources.tenant_id == tenant_id))
    return result.scalars().first()


async def _get_websocket_organization_user(websocket: WebSocket, db: AsyncSession) -> OrganizationUser | None:
    token = websocket.query_params.get("token") or ""
    auth_header = websocket.headers.get("authorization") or ""
    if not token and auth_header.lower().startswith("bearer "):
        token = auth_header.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
        if payload.get("principal_type") != "organization_user":
            return None
        user_id = payload.get("sub")
        organization_id = payload.get("organization_id")
        if not user_id or not organization_id:
            return None
    except jwt.PyJWTError:
        return None

    result = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.id == user_id,
            OrganizationUser.organization_id == organization_id,
            OrganizationUser.status != "disabled",
        )
    )
    return result.scalars().first()


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


@router.get("/erp/providers", response_model=list[OrganizationERPProviderResponse])
async def get_organization_erp_providers(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
):
    return [OrganizationERPProviderResponse(**item) for item in ERPIntegrationService.provider_options()]


@router.get("/shipping/providers", response_model=list[OrganizationShippingProviderResponse])
async def get_organization_shipping_providers(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
):
    return [OrganizationShippingProviderResponse(**item) for item in ShippingIntegrationService.provider_options()]


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
            "Desk.settings.READ",
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


@router.get("/erp/status", response_model=OrganizationERPStatusResponse)
async def get_organization_erp_status(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = tenant_res.provider_status or {}
    secret_ref = ((tenant_res.secret_refs or {}).get("erp_connection") or {}).get("secret_name")
    return OrganizationERPStatusResponse(
        provider=provider_status.get("erp_provider"),
        status=provider_status.get("erp_status", "not_connected"),
        secret_ref=secret_ref,
        account_name=provider_status.get("erp_account_name"),
        indexed_points=int(provider_status.get("erp_indexed_points", 0) or 0),
        module_count=int(provider_status.get("erp_module_count", 0) or 0),
        action_count=int(provider_status.get("erp_action_count", 0) or 0),
        folder_path=provider_status.get("erp_folder_path"),
        last_error=provider_status.get("erp_last_error"),
    )


@router.get("/shipping/status", response_model=OrganizationShippingStatusResponse)
async def get_organization_shipping_status(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = tenant_res.provider_status or {}
    secret_ref = ((tenant_res.secret_refs or {}).get("shipping_connection") or {}).get("secret_name")
    return OrganizationShippingStatusResponse(
        provider=provider_status.get("shipping_provider"),
        status=provider_status.get("shipping_status", "not_connected"),
        secret_ref=secret_ref,
        account_name=provider_status.get("shipping_account_name"),
        indexed_points=int(provider_status.get("shipping_indexed_points", 0) or 0),
        module_count=int(provider_status.get("shipping_module_count", 0) or 0),
        action_count=int(provider_status.get("shipping_action_count", 0) or 0),
        folder_path=provider_status.get("shipping_folder_path"),
        last_error=provider_status.get("shipping_last_error"),
    )


@router.get("/crm/zoho-desk/status", response_model=OrganizationZohoDeskStatusResponse)
async def get_organization_zoho_desk_status(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = tenant_res.provider_status or {}
    return OrganizationZohoDeskStatusResponse(
        status=provider_status.get("zoho_desk_status", "not_connected"),
        account_name=provider_status.get("zoho_desk_account_name"),
        org_id=provider_status.get("zoho_desk_org_id"),
        indexed_points=int(provider_status.get("zoho_desk_indexed_points", 0) or 0),
        module_count=int(provider_status.get("zoho_desk_module_count", 0) or 0),
        action_count=int(provider_status.get("zoho_desk_action_count", 0) or 0),
        folder_path=provider_status.get("zoho_desk_folder_path"),
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
        await QdrantService.delete_points_by_filter(tenant_res, {"integration_type": "database"})
        await QdrantService.delete_points_by_filter(tenant_res, {"source_type": "database_schema_selection"})
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


@router.post("/erp/connect", response_model=OrganizationERPConnectResponse)
async def connect_organization_erp(
    payload: OrganizationERPConnectRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    credentials = {
        "base_url": payload.base_url,
        "company_name": payload.company_name,
        "timeout_seconds": payload.timeout_seconds,
        "max_items_per_module": payload.max_items_per_module,
    }

    try:
        scan_result = await ERPIntegrationService.scan_schema(payload.provider, credentials)
        secret_ref = await ERPIntegrationService.store_connection_secret(
            tenant_res,
            scan_result.provider,
            credentials,
        )
        index_result = await ERPIntegrationService.index_schema_embeddings(tenant_res, scan_result)
    except Exception as exc:
        provider_status = dict(tenant_res.provider_status or {})
        provider_status.update(
            {
                "erp_status": "error",
                "erp_provider": payload.provider,
                "erp_last_error": str(exc),
            }
        )
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider_status = dict(tenant_res.provider_status or {})
    provider_status.update(
        {
            "erp_status": "indexed",
            "erp_provider": scan_result.provider,
            "erp_account_name": scan_result.account_name,
            "erp_module_count": len(scan_result.modules),
            "erp_action_count": len(scan_result.actions),
            "erp_schema_snapshot": scan_result.modules,
            "erp_action_snapshot": scan_result.actions,
            "erp_folder_path": scan_result.folder_path,
            "erp_indexed_points": index_result["indexed_points"],
            "erp_connected_at": datetime.now(timezone.utc).isoformat(),
            "erp_last_error": None,
        }
    )
    tenant_res.provider_status = provider_status
    db.add(tenant_res)
    await db.commit()

    return OrganizationERPConnectResponse(
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


@router.post("/shipping/connect", response_model=OrganizationShippingConnectResponse)
async def connect_organization_shipping(
    payload: OrganizationShippingConnectRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    credentials = {
        "email": str(payload.email),
        "password": payload.password,
        "base_url": payload.base_url,
    }

    try:
        scan_result = await ShippingIntegrationService.scan_schema(payload.provider, credentials)
        secret_ref = await ShippingIntegrationService.store_connection_secret(
            tenant_res,
            scan_result.provider,
            credentials,
        )
        index_result = await ShippingIntegrationService.index_schema_embeddings(tenant_res, scan_result)
    except Exception as exc:
        provider_status = dict(tenant_res.provider_status or {})
        provider_status.update(
            {
                "shipping_status": "error",
                "shipping_provider": payload.provider,
                "shipping_last_error": str(exc),
            }
        )
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider_status = dict(tenant_res.provider_status or {})
    provider_status.update(
        {
            "shipping_status": "indexed",
            "shipping_provider": scan_result.provider,
            "shipping_account_name": scan_result.account_name,
            "shipping_module_count": len(scan_result.modules),
            "shipping_action_count": len(scan_result.actions),
            "shipping_schema_snapshot": scan_result.modules,
            "shipping_action_snapshot": scan_result.actions,
            "shipping_folder_path": scan_result.folder_path,
            "shipping_indexed_points": index_result["indexed_points"],
            "shipping_connected_at": datetime.now(timezone.utc).isoformat(),
            "shipping_last_error": None,
        }
    )
    tenant_res.provider_status = provider_status
    db.add(tenant_res)
    await db.commit()

    return OrganizationShippingConnectResponse(
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


@router.post("/crm/zoho-desk/connect", response_model=OrganizationZohoDeskConnectResponse)
async def connect_organization_zoho_desk(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)

    try:
        provider, credentials = await CRMIntegrationService.load_connection_secret(tenant_res)
        if provider != "zoho":
            raise RuntimeError("Zoho CRM must be connected before Zoho Desk can be integrated")
        _, scan_result = await ZohoDeskService.connect_and_scan(credentials)
        index_result = await ZohoDeskService.index_desk_embeddings(tenant_res, scan_result)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    provider_status = dict(tenant_res.provider_status or {})
    provider_status.update(
        {
            "zoho_desk_status": "indexed",
            "zoho_desk_account_name": scan_result.account_name,
            "zoho_desk_org_id": scan_result.org_id,
            "zoho_desk_module_count": len(scan_result.modules),
            "zoho_desk_action_count": len(scan_result.actions),
            "zoho_desk_folder_path": scan_result.folder_path,
            "zoho_desk_indexed_points": index_result["indexed_points"],
            "zoho_desk_connected_at": datetime.now(timezone.utc).isoformat(),
            "zoho_desk_last_error": None,
        }
    )
    tenant_res.provider_status = provider_status
    db.add(tenant_res)
    await db.commit()

    return OrganizationZohoDeskConnectResponse(
        status="indexed",
        account_name=scan_result.account_name,
        org_id=scan_result.org_id,
        indexed_points=index_result["indexed_points"],
        module_count=len(scan_result.modules),
        action_count=len(scan_result.actions),
        folder_path=scan_result.folder_path,
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
        zoho_desk_scan_result = None
        zoho_desk_index_result = None
        zoho_desk_last_error = None
        try:
            _, zoho_desk_scan_result = await ZohoDeskService.connect_and_scan(credentials)
            zoho_desk_index_result = await ZohoDeskService.index_desk_embeddings(tenant_res, zoho_desk_scan_result)
        except Exception as desk_exc:
            zoho_desk_last_error = str(desk_exc)
            print("[Zoho OAuth] Desk auto-index skipped", {"error": zoho_desk_last_error})

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
        if zoho_desk_scan_result and zoho_desk_index_result:
            provider_status.update(
                {
                    "zoho_desk_status": "indexed",
                    "zoho_desk_account_name": zoho_desk_scan_result.account_name,
                    "zoho_desk_org_id": zoho_desk_scan_result.org_id,
                    "zoho_desk_module_count": len(zoho_desk_scan_result.modules),
                    "zoho_desk_action_count": len(zoho_desk_scan_result.actions),
                    "zoho_desk_folder_path": zoho_desk_scan_result.folder_path,
                    "zoho_desk_indexed_points": zoho_desk_index_result["indexed_points"],
                    "zoho_desk_connected_at": datetime.now(timezone.utc).isoformat(),
                    "zoho_desk_last_error": None,
                }
            )
        elif zoho_desk_last_error:
            provider_status.update(
                {
                    "zoho_desk_status": "not_connected",
                    "zoho_desk_last_error": zoho_desk_last_error,
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


@router.post("/crm/zoho-desk/tickets", response_model=OrganizationZohoDeskTicketResponse)
async def create_organization_zoho_desk_ticket(
    payload: OrganizationZohoDeskTicketCreateRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    org_id = provider_status.get("zoho_desk_org_id")
    if provider_status.get("zoho_desk_status") != "indexed":
        raise HTTPException(status_code=409, detail="Zoho Desk is not connected for this organization")

    try:
        provider, credentials = await CRMIntegrationService.load_connection_secret(tenant_res)
        if provider != "zoho":
            raise RuntimeError("Zoho CRM credentials are not available for Zoho Desk")
        request_payload = {
            "subject": payload.subject,
            "departmentId": payload.department_id,
            "description": payload.description,
            "contactId": payload.contact_id,
            "email": str(payload.email) if payload.email else None,
            "phone": payload.phone,
            "status": payload.status,
            "priority": payload.priority,
            "cf": payload.custom_fields or {},
        }
        request_payload = {key: value for key, value in request_payload.items() if value not in (None, "", {})}
        ticket = await ZohoDeskService.create_ticket(credentials, org_id, request_payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return OrganizationZohoDeskTicketResponse(
        id=str(ticket.get("id") or ""),
        status=ticket.get("status"),
        subject=ticket.get("subject"),
        department_id=str(ticket.get("departmentId")) if ticket.get("departmentId") is not None else None,
        web_url=ticket.get("webUrl") or ticket.get("portalUrl"),
        raw=ticket,
    )


@router.patch("/crm/zoho-desk/tickets/{ticket_id}", response_model=OrganizationZohoDeskTicketResponse)
async def update_organization_zoho_desk_ticket(
    ticket_id: str,
    payload: OrganizationZohoDeskTicketUpdateRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    org_id = provider_status.get("zoho_desk_org_id")
    if provider_status.get("zoho_desk_status") != "indexed":
        raise HTTPException(status_code=409, detail="Zoho Desk is not connected for this organization")

    try:
        provider, credentials = await CRMIntegrationService.load_connection_secret(tenant_res)
        if provider != "zoho":
            raise RuntimeError("Zoho CRM credentials are not available for Zoho Desk")
        request_payload = {
            "subject": payload.subject,
            "description": payload.description,
            "contactId": payload.contact_id,
            "email": str(payload.email) if payload.email else None,
            "phone": payload.phone,
            "status": payload.status,
            "priority": payload.priority,
            "cf": payload.custom_fields or {},
        }
        request_payload = {key: value for key, value in request_payload.items() if value not in (None, "", {})}
        ticket = await ZohoDeskService.update_ticket(credentials, org_id, ticket_id, request_payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return OrganizationZohoDeskTicketResponse(
        id=str(ticket.get("id") or ticket_id),
        status=ticket.get("status"),
        subject=ticket.get("subject"),
        department_id=str(ticket.get("departmentId")) if ticket.get("departmentId") is not None else None,
        web_url=ticket.get("webUrl") or ticket.get("portalUrl"),
        raw=ticket,
    )


@router.post("/erp/tally/xml", response_model=OrganizationTallyXMLResponse)
async def execute_organization_tally_xml(
    payload: OrganizationTallyXMLRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)

    try:
        provider, credentials = await ERPIntegrationService.load_connection_secret(tenant_res)
        if provider != "tally":
            raise RuntimeError("Tally ERP must be connected before executing Tally XML")
        response_xml = await ERPIntegrationService.execute_tally_xml(credentials, payload.xml_payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return OrganizationTallyXMLResponse(response_xml=response_xml)


async def _load_shiprocket_credentials(tenant_res: TenantResources) -> dict:
    provider, credentials = await ShippingIntegrationService.load_connection_secret(tenant_res)
    if provider != "shiprocket":
        raise RuntimeError("Shiprocket must be connected before using shipping operations")
    return credentials


@router.post("/shipping/shiprocket/serviceability", response_model=OrganizationShiprocketAPIResponse)
async def check_shiprocket_serviceability(
    payload: OrganizationShiprocketServiceabilityRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        credentials = await _load_shiprocket_credentials(tenant_res)
        result = await ShippingIntegrationService.check_serviceability(credentials, payload.model_dump(exclude_none=True))
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationShiprocketAPIResponse(raw=result)


@router.post("/shipping/shiprocket/orders", response_model=OrganizationShiprocketAPIResponse)
async def create_shiprocket_order(
    payload: OrganizationShiprocketCreateOrderRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        credentials = await _load_shiprocket_credentials(tenant_res)
        result = await ShippingIntegrationService.create_order(credentials, payload.payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationShiprocketAPIResponse(raw=result)


@router.post("/shipping/shiprocket/awb", response_model=OrganizationShiprocketAPIResponse)
async def assign_shiprocket_awb(
    payload: OrganizationShiprocketAssignAWBRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    request_payload = payload.model_dump(exclude_none=True)
    try:
        credentials = await _load_shiprocket_credentials(tenant_res)
        result = await ShippingIntegrationService.assign_awb(credentials, request_payload)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationShiprocketAPIResponse(raw=result)


@router.post("/shipping/shiprocket/pickup", response_model=OrganizationShiprocketAPIResponse)
async def generate_shiprocket_pickup(
    payload: OrganizationShiprocketPickupRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    shipment_id = payload.shipment_id if isinstance(payload.shipment_id, list) else [payload.shipment_id]
    try:
        credentials = await _load_shiprocket_credentials(tenant_res)
        result = await ShippingIntegrationService.generate_pickup(credentials, {"shipment_id": shipment_id})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationShiprocketAPIResponse(raw=result)


@router.post("/shipping/shiprocket/track", response_model=OrganizationShiprocketAPIResponse)
async def track_shiprocket_shipment(
    payload: OrganizationShiprocketTrackRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        credentials = await _load_shiprocket_credentials(tenant_res)
        result = await ShippingIntegrationService.track(
            credentials,
            order_id=payload.order_id,
            awb_code=payload.awb_code,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationShiprocketAPIResponse(raw=result)


@router.post("/toolkit/generate", response_model=OrganizationToolkitDraftResponse)
async def generate_toolkit_tool(
    payload: OrganizationToolkitGenerateRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    if not _toolkit_integration_connected(provider_status, payload.integration_type, payload.provider):
        raise HTTPException(status_code=409, detail="Toolkit tools can only be generated for connected integrations")
    capability = ToolkitGeneratorService.provider_capability(payload.integration_type, payload.provider)
    if capability is None:
        raise HTTPException(status_code=400, detail="No toolkit builder capability is registered for this provider")
    if payload.builder_key and payload.builder_key != capability.builder_key:
        raise HTTPException(status_code=409, detail="Selected toolkit builder does not match the connected integration")
    draft = await ToolkitGeneratorService.generate_tool(
        tenant_res,
        payload.integration_type,
        payload.provider,
        payload.nlp_prompt,
        payload.system_prompt,
        builder_key=payload.builder_key,
    )

    _, drafts = _toolkit_state(provider_status)
    drafts.append(draft)
    provider_status["toolkit_drafts"] = drafts[-100:]
    _append_toolkit_audit_event(
        provider_status,
        "toolkit_draft_generated",
        current_user.id,
        draft["integration_type"],
        draft["provider"],
        tool_name=(draft.get("tool") or {}).get("name"),
        draft_id=draft["id"],
        metadata=draft.get("context_summary", {}),
    )
    tenant_res.provider_status = provider_status
    db.add(tenant_res)
    await db.commit()
    return OrganizationToolkitDraftResponse(**draft)


@router.get("/toolkit/builders", response_model=OrganizationToolkitBuildersResponse)
async def get_toolkit_builders(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    builders = [OrganizationToolkitBuilderResponse(**item) for item in ToolkitGeneratorService.connected_toolkit_builders(provider_status)]
    return OrganizationToolkitBuildersResponse(builders=builders)


@router.get("/toolkit/registry", response_model=OrganizationToolkitRegistryResponse)
async def get_toolkit_registry(
    integration_type: str,
    provider: str,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    _, drafts = _toolkit_state(provider_status)
    integration_type = integration_type.strip().lower()
    provider = provider.strip().lower()
    if not _toolkit_integration_connected(provider_status, integration_type, provider):
        return OrganizationToolkitRegistryResponse(
            integration_type=integration_type,
            provider=provider,
            tools=[],
            drafts=[],
        )
    matching_drafts = [
        OrganizationToolkitDraftResponse(**draft)
        for draft in drafts
        if draft.get("status") in {"draft", "approved"} and _toolkit_matches(draft, integration_type, provider)
    ]
    tools = await _get_registered_tool_definitions(
        db,
        current_user.organization_id,
        integration_type,
        provider,
        provider_status,
    )
    return OrganizationToolkitRegistryResponse(
        integration_type=integration_type,
        provider=provider,
        tools=tools,
        drafts=matching_drafts,
    )


@router.post("/toolkit/drafts/{draft_id}/approve", response_model=OrganizationToolkitDraftResponse)
async def approve_toolkit_draft(
    draft_id: str,
    payload: OrganizationToolkitReviewRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    _, drafts = _toolkit_state(provider_status)
    selected = None
    for draft in drafts:
        if draft.get("id") == draft_id:
            selected = draft
            break
    if not selected:
        raise HTTPException(status_code=404, detail="Toolkit draft not found")
    if selected.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Toolkit draft has already been reviewed")
    if not _toolkit_integration_connected(provider_status, selected["integration_type"], selected["provider"]):
        raise HTTPException(status_code=409, detail="Toolkit tools can only be approved for connected integrations")

    approval_context = await ToolkitGeneratorService.build_context(
        tenant_res,
        selected["integration_type"],
        selected["provider"],
        selected.get("nlp_prompt") or "",
    )
    generation_result = ToolkitGeneratorService._sanitize_tool(
        dict(selected["tool"]),
        selected["integration_type"],
        selected["provider"],
        selected.get("nlp_prompt") or "",
        approval_context,
    )
    if generation_result.get("validation", {}).get("status") != "passed":
        selected["tool"] = generation_result
        selected["status"] = "draft"
        provider_status["toolkit_drafts"] = drafts
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        raise HTTPException(status_code=409, detail="Toolkit draft failed automatic validation and cannot be approved")
    if generation_result.get("review", {}).get("status") == "rejected":
        selected["tool"] = generation_result
        selected["status"] = "draft"
        provider_status["toolkit_drafts"] = drafts
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        raise HTTPException(status_code=409, detail="Toolkit draft was rejected by automatic reviewer")

    selected["status"] = "approved"
    selected["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    selected["reviewed_by"] = str(current_user.id)
    selected["review_notes"] = payload.notes
    generation_result["review"] = {
        "status": "approved",
        "required": True,
        "reviewer_role": "admin",
        "reviewed_by": str(current_user.id),
        "reviewed_at": selected["reviewed_at"],
        "notes": payload.notes,
        "reason": "Admin approved draft for test-run/publish gate.",
        "required_changes": [],
    }
    generation_result["tool"]["review"] = generation_result["review"]
    generation_result["tool"]["status"] = "approved"
    generation_result["tool"]["approved_at"] = selected["reviewed_at"]
    generation_result["tool"]["approved_by"] = selected["reviewed_by"]
    generation_result["tool"]["tenant_scope"] = {
        "organization_id": str(current_user.organization_id),
        "tenant_id": tenant_res.tenant_id,
    }
    generation_result["tool"]["version"]["reviewed_by"] = str(current_user.id)
    generation_result["tool"]["version"]["approved_by"] = str(current_user.id)
    generation_result["tool"]["version"]["updated_at"] = selected["reviewed_at"]
    generation_result["tool"]["version"]["status"] = "approved"
    generation_result["publish_gate"] = ToolkitGeneratorService.publish_gate(generation_result, admin_approval_exists=True)
    selected["tool"] = generation_result
    tool = dict(generation_result["tool"])
    tool_name = ToolkitGeneratorService._normalize_tool_name((tool.get("mcp") or {}).get("tool_name") or tool.get("name"))
    tool.setdefault("mcp", {})
    tool["mcp"]["tool_name"] = tool_name

    if not generation_result["publish_gate"].get("can_publish"):
        provider_status.pop("mcp_tool_registry", None)
        provider_status["toolkit_drafts"] = drafts
        _append_toolkit_audit_event(
            provider_status,
            "toolkit_tool_admin_approved_pending_publish_gate",
            current_user.id,
            selected["integration_type"],
            selected["provider"],
            tool_name=tool_name,
            draft_id=selected["id"],
            metadata={"publish_gate": generation_result["publish_gate"]},
        )
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        return OrganizationToolkitDraftResponse(**selected)

    existing_result = await db.execute(
        select(MCPToolRegistryEntry).where(
            MCPToolRegistryEntry.organization_id == current_user.organization_id,
            MCPToolRegistryEntry.integration_type == selected["integration_type"],
            MCPToolRegistryEntry.provider == selected["provider"],
            MCPToolRegistryEntry.tool_name == tool_name,
        )
    )
    registry_entry = existing_result.scalars().first()
    if registry_entry:
        registry_entry.title = tool.get("title") or tool.get("name")
        registry_entry.description = tool.get("description")
        registry_entry.tool_definition = tool
        registry_entry.status = "active"
        registry_entry.version = (registry_entry.version or 1) + 1
        registry_entry.draft_id = selected["id"]
        registry_entry.approved_by = current_user.id
        registry_entry.approved_at = datetime.fromisoformat(selected["reviewed_at"])
    else:
        registry_entry = MCPToolRegistryEntry(
            id=uuid.uuid4(),
            organization_id=current_user.organization_id,
            tenant_id=tenant_res.tenant_id,
            integration_type=selected["integration_type"],
            provider=selected["provider"],
            tool_name=tool_name,
            title=tool.get("title") or tool.get("name"),
            description=tool.get("description"),
            tool_definition=tool,
            status="active",
            version=1,
            draft_id=selected["id"],
            approved_by=current_user.id,
            approved_at=datetime.fromisoformat(selected["reviewed_at"]),
        )
    db.add(registry_entry)

    tool["registry_id"] = str(registry_entry.id)
    selected["tool"] = tool
    registry_entry.tool_definition = tool

    provider_status.pop("mcp_tool_registry", None)
    provider_status["toolkit_drafts"] = drafts
    _append_toolkit_audit_event(
        provider_status,
        "toolkit_tool_approved",
        current_user.id,
        selected["integration_type"],
        selected["provider"],
        tool_name=tool_name,
        draft_id=selected["id"],
        metadata={"registry_id": str(registry_entry.id), "version": registry_entry.version},
    )
    tenant_res.provider_status = provider_status
    db.add(tenant_res)
    await db.commit()
    return OrganizationToolkitDraftResponse(**selected)


@router.post("/toolkit/drafts/{draft_id}/reject", response_model=OrganizationToolkitDraftResponse)
async def reject_toolkit_draft(
    draft_id: str,
    payload: OrganizationToolkitReviewRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    provider_status = dict(tenant_res.provider_status or {})
    _, drafts = _toolkit_state(provider_status)
    selected = None
    for draft in drafts:
        if draft.get("id") == draft_id:
            selected = draft
            break
    if not selected:
        raise HTTPException(status_code=404, detail="Toolkit draft not found")
    if selected.get("status") != "draft":
        raise HTTPException(status_code=409, detail="Toolkit draft has already been reviewed")

    selected["status"] = "rejected"
    selected["reviewed_at"] = datetime.now(timezone.utc).isoformat()
    selected["reviewed_by"] = str(current_user.id)
    selected["review_notes"] = payload.notes
    provider_status.pop("mcp_tool_registry", None)
    provider_status["toolkit_drafts"] = drafts
    _append_toolkit_audit_event(
        provider_status,
        "toolkit_draft_rejected",
        current_user.id,
        selected["integration_type"],
        selected["provider"],
        tool_name=(selected.get("tool") or {}).get("name"),
        draft_id=selected["id"],
        metadata={"notes": payload.notes},
    )
    tenant_res.provider_status = provider_status
    db.add(tenant_res)
    await db.commit()
    return OrganizationToolkitDraftResponse(**selected)


@router.get("/agent/documents", response_model=OrganizationAgentDocumentsResponse)
async def get_agent_knowledge_documents(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    return OrganizationAgentDocumentsResponse(documents=AgentKnowledgeService.list_documents(tenant_res))


@router.post("/agent/documents/upload", response_model=OrganizationAgentDocumentResponse)
async def upload_agent_knowledge_document(
    payload: OrganizationAgentDocumentUploadRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    try:
        content = base64.b64decode(payload.content_base64, validate=True)
    except Exception as exc:
        raise HTTPException(status_code=422, detail="Document content must be valid base64") from exc
    if not content:
        raise HTTPException(status_code=422, detail="Document file is empty")
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        document = await AgentKnowledgeService.upload_document(
            tenant_res,
            db,
            current_user,
            name=payload.name,
            document_type=payload.document_type,
            description=payload.description,
            tags=payload.tags,
            filename=payload.filename,
            content=content,
            content_type=payload.content_type,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentDocumentResponse(**document)


@router.post("/agent/documents/{document_id}/approve", response_model=OrganizationAgentDocumentResponse)
async def approve_agent_knowledge_document(
    document_id: str,
    payload: OrganizationAgentDocumentReviewRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        document = await AgentKnowledgeService.review_document(
            tenant_res,
            db,
            current_user,
            document_id,
            approve=True,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentDocumentResponse(**document)


@router.post("/agent/documents/{document_id}/reject", response_model=OrganizationAgentDocumentResponse)
async def reject_agent_knowledge_document(
    document_id: str,
    payload: OrganizationAgentDocumentReviewRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        document = await AgentKnowledgeService.review_document(
            tenant_res,
            db,
            current_user,
            document_id,
            approve=False,
            notes=payload.notes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentDocumentResponse(**document)


@router.post("/agent/test-retrieval", response_model=OrganizationAgentTestRetrievalResponse)
async def test_agent_knowledge_retrieval(
    payload: OrganizationAgentTestQueryRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        result = await AgentKnowledgeService.test_retrieval(tenant_res, payload.query, top_k=payload.top_k, db=db)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentTestRetrievalResponse(**result)


@router.post("/agent/test-answer", response_model=OrganizationAgentTestAnswerResponse)
async def test_agent_knowledge_answer(
    payload: OrganizationAgentTestQueryRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        result = await AgentRuntimeService.answer_text(
            tenant_res,
            payload.query,
            top_k=payload.top_k,
            db=db,
            response_language=payload.response_language,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentTestAnswerResponse(**result)


@router.get("/agent/runtime/status", response_model=OrganizationAgentRuntimeStatusResponse)
async def get_agent_runtime_status(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    return OrganizationAgentRuntimeStatusResponse(**AgentRuntimeService.runtime_status(tenant_res))


@router.post("/agent/runtime/chat", response_model=OrganizationAgentRuntimeChatResponse)
async def chat_with_agent_runtime(
    payload: OrganizationAgentRuntimeChatRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        result = await AgentRuntimeService.answer_text(
            tenant_res,
            payload.query,
            db=db,
            top_k=payload.top_k,
            latency_budget_ms=payload.latency_budget_ms,
            response_language=payload.response_language,
            conversation_history=payload.conversation_history,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentRuntimeChatResponse(**result)


@router.get("/agent/phone-link", response_model=OrganizationAgentPhoneLinkResponse)
async def get_agent_phone_link(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    return OrganizationAgentPhoneLinkResponse(**_agent_phone_link_response(tenant_res))


@router.post("/agent/phone-link", response_model=OrganizationAgentPhoneLinkResponse)
async def link_agent_phone_number(
    payload: OrganizationAgentPhoneLinkRequest,
    request: Request,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    organization: Organization = Depends(deps.get_current_organization),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        public_base_url = settings.AGENT_PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
        link = await ExotelService.link_agent_phone_number(
            tenant_res,
            db,
            phone_number=payload.phone_number,
            public_base_url=public_base_url,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentPhoneLinkResponse(**link)


@router.delete("/agent/phone-link", response_model=OrganizationAgentPhoneLinkResponse)
async def unlink_agent_phone_number(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        await ExotelService.unlink_agent_phone_number(tenant_res, db)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentPhoneLinkResponse(**_agent_phone_link_response(tenant_res))


@router.post("/agent/runtime/latency-test", response_model=OrganizationAgentLatencyTestResponse)
async def test_agent_latency(
    payload: OrganizationAgentLatencyTestRequest,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        result = await AgentRuntimeService.latency_test(
            tenant_res,
            payload.query,
            db=db,
            target_ms=payload.target_ms,
            response_language=payload.response_language,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return OrganizationAgentLatencyTestResponse(**result)


@router.get("/agent/campaigns", response_model=list[CampaignOut])
async def list_agent_campaigns(
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    return await OutboundCampaignService.list_campaigns(tenant_res, db)


@router.post("/agent/campaigns", response_model=CampaignDetailOut)
async def create_agent_campaign(
    name: str = Form(...),
    contacts_file: UploadFile = File(...),
    script_file: UploadFile = File(...),
    from_number: str | None = Form(None),
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    try:
        campaign = await OutboundCampaignService.create_campaign(
            tenant_res,
            db,
            name=name,
            excel_file=contacts_file,
            doc_file=script_file,
            from_number=from_number,
        )
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return campaign


@router.get("/agent/campaigns/{campaign_id}", response_model=CampaignDetailOut)
async def get_agent_campaign(
    campaign_id: uuid.UUID,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin", "manager"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tenant_res, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.post("/agent/campaigns/{campaign_id}/launch", response_model=CampaignDetailOut)
async def launch_agent_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tenant_res, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        base_url = settings.AGENT_PUBLIC_BASE_URL or str(request.base_url).rstrip("/")
        campaign = await OutboundCampaignService.launch_campaign(campaign, db, public_base_url=base_url)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return campaign


@router.post("/agent/campaigns/{campaign_id}/cancel", response_model=CampaignDetailOut)
async def cancel_agent_campaign(
    campaign_id: uuid.UUID,
    current_user: OrganizationUser = Depends(deps.RequireOrganizationRole(["admin"])),
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tenant_res, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        return await OutboundCampaignService.cancel_campaign(campaign, db)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/agent/exotel/voice/{link_id}", response_class=PlainTextResponse)
async def exotel_agent_voice_webhook(
    link_id: str,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_by_agent_phone_link(db, link_id)
    if not tenant_res:
        return PlainTextResponse("NOKVO agent is not linked to this number.", status_code=404)
    host = request.url.hostname or "localhost"
    scheme = "wss" if request.url.scheme == "https" else "ws"
    port = f":{request.url.port}" if request.url.port else ""
    media_url = f"{scheme}://{host}{port}/api/org-auth/agent/exotel/media/{link_id}"
    return PlainTextResponse(media_url, media_type="text/plain")


@router.websocket("/agent/exotel/media/{link_id}")
async def exotel_agent_media_websocket(websocket: WebSocket, link_id: str):
    async for db in deps.get_db():
        tenant_res = await _get_tenant_resources_by_agent_phone_link(db, link_id)
        if not tenant_res:
            await websocket.close(code=1008)
            return
        await ExotelBridgeService.run_session(websocket, tenant_res, db=db)
        return


@router.post("/agent/exotel/outbound-status/{call_link_id}")
async def exotel_outbound_status_callback(
    call_link_id: str,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
):
    campaign, _contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
    if not campaign:
        return {"ok": False, "reason": "campaign_not_found"}
    try:
        payload = dict(await request.form())
    except Exception:
        payload = await request.json()
    event_type = str(payload.get("Status") or payload.get("status") or payload.get("CallStatus") or payload.get("event") or "call.update")
    normalized_event = "call.answered" if event_type.lower() in {"answered", "in-progress", "in progress"} else "call.hangup"
    await OutboundCampaignService.handle_call_status(campaign, call_link_id, normalized_event, payload, db)
    return {"ok": True, "call_link_id": call_link_id}


@router.websocket("/agent/exotel/outbound-media/{call_link_id}")
async def exotel_outbound_media_websocket(websocket: WebSocket, call_link_id: str):
    async for db in deps.get_db():
        campaign, contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
        if not campaign or not contact:
            await websocket.close(code=1008)
            return
        tenant_res = await _get_tenant_resources_by_tenant_id(db, campaign.tenant_id)
        if not tenant_res:
            await websocket.close(code=1008)
            return
        adapter = ExotelWebSocketAdapter(websocket, language="en")
        campaign_context = {
            "campaign_id": str(campaign.id),
            "goal": campaign.name,
            "contact": contact,
            "opening_message": (
                f"Start the outbound campaign call for {contact.get('name') or 'the recipient'}. "
                "Use the campaign script context, introduce yourself briefly, and ask if this is a good time to talk."
            ),
        }
        await NokvoOneVoiceStreamService.run_session(
            adapter,
            tenant_res,
            db=db,
            language="en",
            call_id=call_link_id,
            campaign_context=campaign_context,
        )
        return


@router.post("/agent/twilio/voice/{link_id}", response_class=PlainTextResponse)
async def twilio_agent_voice_webhook(
    link_id: str,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
):
    tenant_res = await _get_tenant_resources_by_agent_phone_link(db, link_id)
    if not tenant_res:
        return PlainTextResponse(
            "<Response><Say>The NOKVO agent is not linked to this number.</Say><Hangup/></Response>",
            media_type="application/xml",
            status_code=404,
        )
    host = request.url.hostname or "localhost"
    scheme = "wss" if request.url.scheme == "https" else "ws"
    port = f":{request.url.port}" if request.url.port else ""
    media_url = f"{scheme}://{host}{port}/api/org-auth/agent/twilio/media/{link_id}"
    twiml = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response>"
        "<Connect>"
        f'<Stream url="{media_url}">'
        f'<Parameter name="tenant_id" value="{tenant_res.tenant_id}"/>'
        "</Stream>"
        "</Connect>"
        "</Response>"
    )
    return PlainTextResponse(twiml, media_type="application/xml")


@router.post("/agent/twilio/status/{link_id}")
async def twilio_agent_status_callback(link_id: str):
    return {"ok": True, "link_id": link_id}


@router.websocket("/agent/twilio/media/{link_id}")
async def twilio_agent_media_websocket(websocket: WebSocket, link_id: str):
    async for db in deps.get_db():
        tenant_res = await _get_tenant_resources_by_agent_phone_link(db, link_id)
        if not tenant_res:
            await websocket.close(code=1008)
            return
        await websocket.accept()
        await websocket.send_json({"event": "connected", "protocol": "Call", "version": "1.0.0"})
        await websocket.send_json({
            "event": "mark",
            "mark": {"name": "agent_link_ready"},
        })
        # Twilio Media Streams send telephony audio here. The browser Agent Studio
        # runtime is already full-duplex; this endpoint establishes the phone-number
        # link and is the integration point for production transcoding to Twilio media.
        while True:
            message = await websocket.receive_text()
            payload = json.loads(message)
            if payload.get("event") == "stop":
                await websocket.close()
                return


@router.websocket("/agent/voice/ws")
async def agent_voice_websocket(websocket: WebSocket):
    async for db in deps.get_db():
        current_user = await _get_websocket_organization_user(websocket, db)
        if not current_user or current_user.role not in {"admin", "manager"}:
            await websocket.close(code=1008)
            return
        tenant_res = await _get_tenant_resources_for_org(db, current_user.organization_id)
        await NokvoOneVoiceStreamService.run_session(websocket, tenant_res, db=db)
        return


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
