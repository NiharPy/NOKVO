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

import uuid
from typing import Any

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
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api import deps
from app.core.config import settings
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.outbound_campaign import OutboundCampaign
from app.models.tenant_resources import TenantResources
from app.services.exotel_bridge_service import ExotelBridgeService, ExotelWebSocketAdapter
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
from app.services.outbound_campaign_service import OutboundCampaignService

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
    res = await db.execute(select(TenantResources))
    for tr in res.scalars().all():
        link = dict((tr.provider_status or {}).get("agent_phone_link") or {})
        if link.get("link_id") == link_id and link.get("status") == "linked":
            return tr
    return None


async def _tenant_by_tenant_id(db: AsyncSession, tenant_id: str) -> TenantResources | None:
    res = await db.execute(
        select(TenantResources).where(TenantResources.tenant_id == tenant_id)
    )
    return res.scalars().first()


async def _ws_user(websocket: WebSocket, db: AsyncSession) -> OrganizationUser | None:
    """Decode an organization_user JWT carried in ?token= or Authorization header.
    Returns the user only when it belongs to an active Nokvo One organization the
    caller is allowed to operate on."""
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
    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == uid))
    user = user_res.scalars().first()
    if user is None or user.status == "disabled":
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
        "doc_blob_path": c.doc_blob_path,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


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
    excel_file: UploadFile = File(...),
    doc_file: UploadFile = File(...),
    from_number: str | None = Form(None),
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    try:
        campaign = await OutboundCampaignService.create_campaign(
            tr,
            db,
            name=name,
            excel_file=excel_file,
            doc_file=doc_file,
            from_number=from_number,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _campaign_response(campaign)


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        campaign = await OutboundCampaignService.cancel_campaign(campaign, db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return _campaign_response(campaign)
