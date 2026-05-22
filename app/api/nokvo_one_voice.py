"""Nokvo One voice pipeline router.

Mounted at /api/nokvo-one/agents. Exposes the tenant-isolated voice/RAG
endpoints described in the architecture spec:

  - GET  /runtime/status                          — pipeline diagnostics
  - WS   /voice/ws                                 — browser mic tester (admin/manager JWT)
  - GET  /phone-link                               — tenant's Exotel link config
  - POST /phone-link                               — admin sets/clears the link_id
  - POST /exotel/voice/{link_id}                   — Exotel inbound webhook
  - WS   /exotel/media/{link_id}                   — Exotel inbound media stream
  - POST /exotel/outbound-status/{call_link_id}    — outbound call status
  - WS   /exotel/outbound-media/{call_link_id}     — outbound media stream
  - GET  /campaigns                                — list outbound campaigns
  - POST /campaigns                                — create + auto-ingest script (admin)
  - GET  /campaigns/{id}                           — single campaign
  - POST /campaigns/{id}/launch                    — kick off the calls (admin)
  - POST /campaigns/{id}/cancel                    — cancel (admin)

Tenant isolation is enforced via TenantResources lookups for every path:
  - Authenticated routes: lookup by user.organization_id
  - Exotel webhooks: lookup by provider_status.agent_phone_link.link_id
  - Outbound: lookup by campaign.tenant_id (campaign already scoped to a tenant)
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

import jwt
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    status,
)
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api import deps
from app.core.config import settings
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.outgoing_lead import (
    LeadCaptureForm,
    LeadSourceConnection,
    LeadSourceProvider,
    OutgoingLead,
)
from app.models.outbound_campaign import OutboundCampaign
from app.models.tenant_resources import TenantResources
from app.services.exotel_bridge_service import ExotelBridgeService, ExotelWebSocketAdapter
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
from app.services.outgoing_lead_service import (
    OutgoingLeadService,
    OutgoingLeadServiceError,
    decode_oauth_state,
    lead_is_callable,
)
from app.services.outbound_campaign_service import OutboundCampaignService


def _safe_detail(exc: BaseException) -> str:
    """Return a user-safe error detail (forward RuntimeError/ValueError text;
    swallow internal exception messages and log them)."""
    import logging
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)
    logging.getLogger(__name__).exception("unexpected exception in request handler", exc_info=exc)
    return "Operation failed"


router = APIRouter()

_ALLOWED_STATUSES = ["pending_approval", "active", "suspended"]


def _viewer_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=_ALLOWED_STATUSES,
        allowed_roles=["admin", "manager"],
    )


def _admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=_ALLOWED_STATUSES,
        allowed_roles=["admin"],
    )


async def _tenant_for_user(db: AsyncSession, user: OrganizationUser) -> TenantResources:
    res = await db.execute(
        select(TenantResources).where(TenantResources.organization_id == user.organization_id)
    )
    tr = res.scalars().first()
    if not tr:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")
    return tr


async def _tenant_by_link_id(db: AsyncSession, link_id: str) -> TenantResources | None:
    # Push the filter into Postgres so we don't pull every tenant row into
    # Python on each Exotel webhook. Still requires a JSONB GIN index for sub-
    # linear lookup, but at minimum keeps the row scan in the DB.
    res = await db.execute(
        select(TenantResources).where(
            TenantResources.provider_status["agent_phone_link"]["link_id"].astext == link_id,
            TenantResources.provider_status["agent_phone_link"]["status"].astext == "linked",
        )
    )
    return res.scalars().first()


async def _tenant_by_tenant_id(db: AsyncSession, tenant_id: str) -> TenantResources | None:
    res = await db.execute(
        select(TenantResources).where(TenantResources.tenant_id == tenant_id)
    )
    return res.scalars().first()


async def _connection_for_user(
    db: AsyncSession,
    tenant_res: TenantResources,
    connection_id: uuid.UUID,
) -> LeadSourceConnection:
    res = await db.execute(
        select(LeadSourceConnection).where(
            LeadSourceConnection.id == connection_id,
            LeadSourceConnection.tenant_id == tenant_res.tenant_id,
        )
    )
    connection = res.scalars().first()
    if connection is None:
        raise HTTPException(status_code=404, detail="Lead source connection not found")
    return connection


async def _ws_user(websocket: WebSocket, db: AsyncSession) -> OrganizationUser | None:
    """Decode an organization_user JWT carried in ?token= or Authorization header.
    Returns the user only when it belongs to an active Nokvo One organization,
    the session has not been revoked, and (when TOTP is enrolled) the token is
    MFA-elevated."""
    from app.models.organization_session import OrganizationSession

    token = websocket.query_params.get("token") or ""
    auth = websocket.headers.get("authorization") or ""
    if not token and auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        payload = jwt.decode(token, settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError:
        return None
    if payload.get("principal_type") != "organization_user":
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return None

    session_id = payload.get("sid")
    if session_id:
        try:
            sess_res = await db.execute(
                select(OrganizationSession).where(OrganizationSession.id == session_id)
            )
            session = sess_res.scalars().first()
        except Exception:
            return None
        if not session or session.revoked_at is not None:
            return None

    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == uid))
    user = user_res.scalars().first()
    if user is None or user.status == "disabled":
        return None

    has_totp = bool(
        getattr(user, "totp_secret_encrypted", None)
        or getattr(user, "totp_secret_encrypted_v2", None)
    )
    if user.mfa_required and has_totp and not bool(payload.get("mfa_completed", False)):
        return None

    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    org = org_res.scalars().first()
    if org is None or (org.product_tier or "nokvo_prime") != "nokvo_one":
        return None
    if org.status not in _ALLOWED_STATUSES:
        return None
    return user


# ────────────────────────── Runtime status ──────────────────────────


@router.get("/runtime/status")
async def get_runtime_status(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    return NokvoOneVoicePipeline.runtime_status(tr)


@router.get("/runtime/health")
async def get_runtime_health(
    window_hours: int = 24,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Operator triage view: retry queue depth, recent outcome
    distribution + failure rate, and KB source freshness. ``window_hours``
    bounds the outcome window (default 24h)."""
    from app.services.agent_runtime_health import build_health_report

    tr = await _tenant_for_user(db, user)
    window = max(1, min(int(window_hours), 24 * 30))
    return await build_health_report(db, tr, window_hours=window)


# ────────────────────────── Browser voice tester ──────────────────────────


@router.websocket("/voice/ws")
async def voice_tester_websocket(websocket: WebSocket):
    async for db in deps.get_db():
        user = await _ws_user(websocket, db)
        if user is None or user.role not in {"admin", "manager"}:
            await websocket.close(code=1008)
            return
        tr = await _tenant_for_user(db, user)
        await NokvoOneVoiceStreamService.run_session(websocket, tr, db=db)
        return


# ────────────────────────── Phone link configuration ──────────────────────────


class PhoneLinkConfigRequest(BaseModel):
    link_id: str | None = None


class LeadOauthStartRequest(BaseModel):
    provider: str = Field(pattern="^(meta_ads|google_ads|google_forms)$")
    mode: str = "ads"


class LeadConnectionUpdateRequest(BaseModel):
    display_name: str | None = None
    provider_account_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LeadExternalFormRequest(BaseModel):
    provider: LeadSourceProvider
    name: str
    provider_form_id: str
    source_connection_id: uuid.UUID | None = None
    field_mapping: dict[str, Any] = Field(default_factory=dict)
    consent_field_key: str | None = None
    consent_text: str | None = None
    default_call_consent: bool = False


class LeadNokvoFormRequest(BaseModel):
    name: str
    fields: list[dict[str, Any]] = Field(default_factory=list)
    consent_text: str


class PublicLeadFormSubmitRequest(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _connection_response(connection: LeadSourceConnection) -> dict[str, Any]:
    return {
        "id": str(connection.id),
        "provider": _enum_value(connection.provider),
        "status": _enum_value(connection.status),
        "display_name": connection.display_name,
        "provider_account_id": connection.provider_account_id,
        "scopes": connection.scopes or [],
        "metadata": connection.metadata_ or {},
        "last_sync_at": connection.last_sync_at.isoformat() if connection.last_sync_at else None,
        "last_error": connection.last_error,
        "created_at": connection.created_at.isoformat() if connection.created_at else None,
    }


def _form_public_url(request: Request, form: LeadCaptureForm) -> str | None:
    if not form.public_slug:
        return None
    base = f"{request.url.scheme}://{request.url.netloc}"
    return f"{base}/api/nokvo-one/agents/lead-sources/nokvo-forms/public/{form.public_slug}"


def _form_response(form: LeadCaptureForm, request: Request | None = None) -> dict[str, Any]:
    return {
        "id": str(form.id),
        "provider": _enum_value(form.provider),
        "status": _enum_value(form.status),
        "name": form.name,
        "provider_form_id": form.provider_form_id,
        "provider_account_id": form.provider_account_id,
        "source_connection_id": str(form.source_connection_id) if form.source_connection_id else None,
        "public_slug": form.public_slug,
        "public_url": _form_public_url(request, form) if request else None,
        "external_url": form.external_url,
        "field_schema": form.field_schema or [],
        "field_mapping": form.field_mapping or {},
        "consent_field_key": form.consent_field_key,
        "consent_text": form.consent_text,
        "default_call_consent": form.default_call_consent,
        "metadata": form.metadata_ or {},
        "last_synced_at": form.last_synced_at.isoformat() if form.last_synced_at else None,
        "created_at": form.created_at.isoformat() if form.created_at else None,
    }


def _lead_response(lead: OutgoingLead) -> dict[str, Any]:
    return {
        "id": str(lead.id),
        "source_provider": _enum_value(lead.source_provider),
        "source_connection_id": str(lead.source_connection_id) if lead.source_connection_id else None,
        "capture_form_id": str(lead.capture_form_id) if lead.capture_form_id else None,
        "provider_lead_id": lead.provider_lead_id,
        "name": lead.name,
        "email": lead.email,
        "phone_raw": lead.phone_raw,
        "phone_e164": lead.phone_e164,
        "fields": lead.fields or {},
        "source_metadata": lead.source_metadata or {},
        "consent_status": _enum_value(lead.consent_status),
        "consent_text": lead.consent_text,
        "consent_field_key": lead.consent_field_key,
        "consented_at": lead.consented_at.isoformat() if lead.consented_at else None,
        "submitted_at": lead.submitted_at.isoformat() if lead.submitted_at else None,
        "opt_out_at": lead.opt_out_at.isoformat() if lead.opt_out_at else None,
        "call_status": _enum_value(lead.call_status),
        "callable": lead_is_callable(lead),
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


def _phone_link_summary(request: Request, tr: TenantResources) -> dict[str, Any]:
    link = dict((tr.provider_status or {}).get("agent_phone_link") or {})
    link_id = link.get("link_id")
    status_val = link.get("status") or ("linked" if link_id else "not_linked")
    host = request.url.hostname or "localhost"
    scheme_http = "https" if request.url.scheme == "https" else "http"
    scheme_ws = "wss" if request.url.scheme == "https" else "ws"
    port = f":{request.url.port}" if request.url.port else ""
    inbound_webhook = (
        f"{scheme_http}://{host}{port}/api/nokvo-one/agents/exotel/voice/{link_id}"
        if link_id else None
    )
    inbound_media = (
        f"{scheme_ws}://{host}{port}/api/nokvo-one/agents/exotel/media/{link_id}"
        if link_id else None
    )
    return {
        "link_id": link_id,
        "status": status_val,
        "exotel_webhook_url": inbound_webhook,
        "exotel_media_url": inbound_media,
    }


@router.get("/phone-link")
async def get_phone_link(
    request: Request,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    return _phone_link_summary(request, tr)


@router.post("/phone-link")
async def set_phone_link(
    payload: PhoneLinkConfigRequest,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    provider_status = dict(tr.provider_status or {})
    link = dict(provider_status.get("agent_phone_link") or {})
    if payload.link_id:
        link.update({"link_id": payload.link_id.strip(), "status": "linked"})
    else:
        link.update({"link_id": None, "status": "not_linked"})
    provider_status["agent_phone_link"] = link
    tr.provider_status = provider_status
    flag_modified(tr, "provider_status")
    db.add(tr)
    await db.commit()
    await db.refresh(tr)
    return _phone_link_summary(request, tr)


# ────────────────────────── Lead sources and consented leads ──────────────────────────


@router.get("/lead-sources/connections")
async def list_lead_connections(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    connections = await OutgoingLeadService.list_connections(tr, db)
    return [_connection_response(c) for c in connections]


@router.post("/lead-sources/oauth/start")
async def start_lead_oauth(
    payload: LeadOauthStartRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    try:
        return OutgoingLeadService.oauth_start_url(
            tr,
            organization_id=user.organization_id,
            user_id=user.id,
            provider=payload.provider,
            mode=payload.mode,
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc


@router.get("/lead-sources/oauth/{provider}/callback")
async def lead_oauth_callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(deps.get_db),
):
    public_base = settings.NOKVO_ONE_PUBLIC_BASE_URL.rstrip("/") or "http://localhost:5173"
    if error:
        return RedirectResponse(f"{public_base}?lead_connection=error&provider={provider}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="OAuth code and state are required")
    try:
        state_data = decode_oauth_state(state)
        if state_data.get("provider") != provider:
            raise OutgoingLeadServiceError("OAuth provider does not match state.")
        tr = await _tenant_by_tenant_id(db, str(state_data["tenant_id"]))
        if tr is None:
            raise OutgoingLeadServiceError("Tenant resources not found for OAuth callback.")
        await OutgoingLeadService.exchange_oauth_code(
            db,
            tenant_res=tr,
            user_id=uuid.UUID(str(state_data["user_id"])),
            provider=provider,
            mode=str(state_data.get("mode") or ""),
            code=code,
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return RedirectResponse(f"{public_base}?lead_connection=success&provider={provider}")


@router.patch("/lead-sources/connections/{connection_id}")
async def update_lead_connection(
    connection_id: uuid.UUID,
    payload: LeadConnectionUpdateRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    connection = await _connection_for_user(db, tr, connection_id)
    connection = await OutgoingLeadService.update_connection_metadata(
        connection,
        db,
        metadata=payload.metadata,
        display_name=payload.display_name,
        provider_account_id=payload.provider_account_id,
    )
    return _connection_response(connection)


@router.post("/lead-sources/connections/{connection_id}/sync")
async def sync_lead_connection(
    connection_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    connection = await _connection_for_user(db, tr, connection_id)
    try:
        return await OutgoingLeadService.sync_connection(connection, db)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc


@router.get("/lead-sources/forms")
async def list_lead_forms(
    request: Request,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    forms = await OutgoingLeadService.list_forms(tr, db)
    return [_form_response(form, request) for form in forms]


@router.post("/lead-sources/forms", status_code=status.HTTP_201_CREATED)
async def register_external_lead_form(
    payload: LeadExternalFormRequest,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    if payload.source_connection_id:
        await _connection_for_user(db, tr, payload.source_connection_id)
    try:
        form = await OutgoingLeadService.register_external_form(
            tr,
            db,
            created_by_user_id=user.id,
            provider=payload.provider,
            name=payload.name,
            provider_form_id=payload.provider_form_id,
            source_connection_id=payload.source_connection_id,
            field_mapping=payload.field_mapping,
            consent_field_key=payload.consent_field_key,
            consent_text=payload.consent_text,
            default_call_consent=payload.default_call_consent,
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return _form_response(form, request)


@router.post("/lead-sources/nokvo-forms", status_code=status.HTTP_201_CREATED)
async def create_nokvo_lead_form(
    payload: LeadNokvoFormRequest,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    try:
        form = await OutgoingLeadService.create_nokvo_form(
            tr,
            db,
            created_by_user_id=user.id,
            name=payload.name,
            fields=payload.fields,
            consent_text=payload.consent_text,
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return _form_response(form, request)


@router.get("/lead-sources/nokvo-forms/public/{slug}")
async def get_public_nokvo_form(slug: str, request: Request, db: AsyncSession = Depends(deps.get_db)):
    form = await OutgoingLeadService.get_public_form(slug, db)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    return _form_response(form, request)


@router.post("/lead-sources/nokvo-forms/public/{slug}/submit", status_code=status.HTTP_201_CREATED)
async def submit_public_nokvo_form(
    slug: str,
    payload: PublicLeadFormSubmitRequest,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
):
    form = await OutgoingLeadService.get_public_form(slug, db)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    try:
        lead = await OutgoingLeadService.submit_public_form(
            form,
            db,
            fields=payload.fields,
            request_metadata={
                "ip": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
                "referer": request.headers.get("referer"),
            },
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return {"ok": True, "lead_id": str(lead.id)}


@router.get("/lead-sources/leads")
async def list_outgoing_leads(
    eligible_only: bool = False,
    limit: int = 200,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    leads = await OutgoingLeadService.list_leads(tr, db, eligible_only=eligible_only, limit=limit)
    return [_lead_response(lead) for lead in leads]


@router.get("/lead-sources/meta/webhook")
async def verify_meta_leadgen_webhook(request: Request):
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.META_LEADGEN_WEBHOOK_VERIFY_TOKEN
    ):
        return PlainTextResponse(params.get("hub.challenge") or "")
    raise HTTPException(status_code=403, detail="Meta webhook verification failed")


def _verify_meta_signature(raw_body: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 header against the raw request body
    using META_ADS_APP_SECRET. Returns False when secret is unset (fail closed)."""
    if not header or not settings.META_ADS_APP_SECRET:
        return False
    prefix = "sha256="
    if not header.startswith(prefix):
        return False
    expected = hmac.new(
        settings.META_ADS_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, header[len(prefix):])


@router.post("/lead-sources/meta/webhook")
async def receive_meta_leadgen_webhook(request: Request, db: AsyncSession = Depends(deps.get_db)):
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    if not _verify_meta_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="Meta webhook signature verification failed")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid Meta webhook payload")
    imported = 0
    errors: list[str] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "leadgen":
                continue
            value = change.get("value") or {}
            leadgen_id = value.get("leadgen_id")
            form_id = value.get("form_id")
            if not leadgen_id or not form_id:
                continue
            try:
                lead = await OutgoingLeadService.ingest_meta_leadgen_event(
                    db,
                    form_id=str(form_id),
                    leadgen_id=str(leadgen_id),
                )
                if lead:
                    imported += 1
            except Exception as exc:
                errors.append(str(exc)[:200])
    return {"ok": True, "imported": imported, "errors": errors[:5]}


# ────────────────────────── Exotel inbound ──────────────────────────


@router.post("/exotel/voice/{link_id}", response_class=PlainTextResponse)
async def exotel_inbound_webhook(
    link_id: str, request: Request, db: AsyncSession = Depends(deps.get_db)
):
    tr = await _tenant_by_link_id(db, link_id)
    if not tr:
        return PlainTextResponse(
            "Nokvo One agent is not linked to this number.", status_code=404
        )
    host = request.url.hostname or "localhost"
    scheme = "wss" if request.url.scheme == "https" else "ws"
    port = f":{request.url.port}" if request.url.port else ""
    media_url = f"{scheme}://{host}{port}/api/nokvo-one/agents/exotel/media/{link_id}"
    return PlainTextResponse(media_url, media_type="text/plain")


@router.websocket("/exotel/media/{link_id}")
async def exotel_inbound_media_websocket(websocket: WebSocket, link_id: str):
    async for db in deps.get_db():
        tr = await _tenant_by_link_id(db, link_id)
        if not tr:
            await websocket.close(code=1008)
            return
        await ExotelBridgeService.run_session(websocket, tr, db=db)
        return


# ────────────────────────── Exotel outbound ──────────────────────────


@router.post("/exotel/outbound-status/{call_link_id}")
async def exotel_outbound_status(
    call_link_id: str, request: Request, db: AsyncSession = Depends(deps.get_db)
):
    campaign, _contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
    if not campaign:
        return {"ok": False, "reason": "campaign_not_found"}
    try:
        payload = dict(await request.form())
    except Exception:
        payload = await request.json()
    event_type = str(
        payload.get("Status")
        or payload.get("status")
        or payload.get("CallStatus")
        or payload.get("event")
        or "call.update"
    )
    normalized = (
        "call.answered"
        if event_type.lower() in {"answered", "in-progress", "in progress"}
        else "call.hangup"
    )
    await OutboundCampaignService.handle_call_status(
        campaign, call_link_id, normalized, payload, db
    )
    return {"ok": True, "call_link_id": call_link_id}


@router.websocket("/exotel/outbound-media/{call_link_id}")
async def exotel_outbound_media_websocket(websocket: WebSocket, call_link_id: str):
    async for db in deps.get_db():
        campaign, contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
        if not campaign or not contact:
            await websocket.close(code=1008)
            return
        tr = await _tenant_by_tenant_id(db, campaign.tenant_id)
        if not tr:
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
            tr,
            db=db,
            language="en",
            call_id=call_link_id,
            campaign_context=campaign_context,
        )
        return


# ────────────────────────── Campaigns ──────────────────────────


def _campaign_response(c: OutboundCampaign) -> dict[str, Any]:
    status_val = c.status.value if hasattr(c.status, "value") else c.status
    return {
        "id": str(c.id),
        "name": c.name,
        "status": status_val,
        "from_number": c.from_number,
        "total_count": c.total_count or 0,
        "answered_count": c.answered_count or 0,
        "failed_count": c.failed_count or 0,
        "contacts": c.contacts or [],
        "agent_config": c.agent_config or {},
        "doc_blob_path": c.doc_blob_path,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


def _parse_campaign_list_field(value: str | None) -> list[str]:
    if not value:
        return []
    raw = value.strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw.splitlines()
    if isinstance(parsed, str):
        parsed = parsed.splitlines()
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


@router.get("/campaigns")
async def list_campaigns(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    items = await OutboundCampaignService.list_campaigns(tr, db)
    return [_campaign_response(c) for c in items]


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    name: str = Form(...),
    lead_ids: str | None = Form(None),
    excel_file: UploadFile | None = File(None),
    doc_file: UploadFile = File(...),
    from_number: str | None = Form(None),
    agent_prompt: str | None = Form(None),
    objectives: str | None = Form(None),
    exit_conditions: str | None = Form(None),
    tone: str | None = Form(None),
    silence_timeout_seconds: float | None = Form(None),
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    try:
        parsed_lead_ids: list[uuid.UUID] = []
        if lead_ids:
            raw_ids = json.loads(lead_ids) if lead_ids.strip().startswith("[") else [x.strip() for x in lead_ids.split(",")]
            parsed_lead_ids = [uuid.UUID(str(item)) for item in raw_ids if str(item).strip()]
        if not parsed_lead_ids:
            raise ValueError(
                "Outgoing Agent campaigns can only be created from consented leads imported from Meta Ads, "
                "Google Ads, Google Forms, or Nokvo forms."
            )
        campaign = await OutboundCampaignService.create_campaign_from_leads(
            tr,
            db,
            name=name,
            lead_ids=parsed_lead_ids,
            doc_file=doc_file,
            from_number=from_number,
            agent_config={
                "agent_prompt": agent_prompt,
                "objectives": _parse_campaign_list_field(objectives),
                "exit_conditions": _parse_campaign_list_field(exit_conditions),
                "tone": tone,
                "silence_timeout_seconds": silence_timeout_seconds,
            },
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return _campaign_response(campaign)


@router.post("/campaigns/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    public_base = f"{request.url.scheme}://{request.url.netloc}"
    try:
        campaign = await OutboundCampaignService.launch_campaign(
            campaign,
            db,
            public_base_url=public_base,
            path_prefix="/api/nokvo-one/agents",
            tenant_res=tr,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        campaign = await OutboundCampaignService.cancel_campaign(campaign, db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)
