"""Nokvo Connect — public voice/text API surface.

Mounted at ``/api/voice``. Authenticated with an organization API key
(``nk_live_...`` / ``nk_test_...``) over either ``X-Nokvo-API-Key`` or
``Authorization: Bearer``. The voice WS reuses the same pipeline the
phone-call path uses; the text endpoints reuse ``AgentRuntimeService``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Request,
    WebSocket,
    status,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.config import settings
from app.core.rate_limit import limiter
from app.core import api_keys as api_key_utils
from app.models.connect_api_key import OrganizationApiKey
from app.models.connect_session import ConnectSession
from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.schemas.connect import (
    SessionCreateRequest,
    SessionCreateResponse,
    SessionResponse,
    TextMessageRequest,
    TextMessageResponse,
)
from app.services.agent_runtime_service import AgentRuntimeService
from app.services.connect_session_service import ConnectSessionService
from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService


logger = logging.getLogger(__name__)
router = APIRouter()


# Concurrent-session counter, per API key. Kept in-process for v1; promoting
# to Redis is one swap. Lock per-key to keep increment/decrement atomic.
_concurrent: dict[uuid.UUID, int] = {}
_concurrent_locks: dict[uuid.UUID, asyncio.Lock] = {}


def _lock_for(api_key_id: uuid.UUID) -> asyncio.Lock:
    lock = _concurrent_locks.get(api_key_id)
    if lock is None:
        lock = asyncio.Lock()
        _concurrent_locks[api_key_id] = lock
    return lock


async def _acquire_slot(api_key: OrganizationApiKey) -> bool:
    async with _lock_for(api_key.id):
        current = _concurrent.get(api_key.id, 0)
        if current >= int(api_key.max_concurrent_sessions or 5):
            return False
        _concurrent[api_key.id] = current + 1
        return True


async def _release_slot(api_key_id: uuid.UUID) -> None:
    async with _lock_for(api_key_id):
        current = _concurrent.get(api_key_id, 0)
        if current > 0:
            _concurrent[api_key_id] = current - 1


def _serialize_session(record: ConnectSession) -> SessionResponse:
    return SessionResponse.model_validate(record, from_attributes=True)


def _ws_base_url(request: Request) -> str:
    forwarded_proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).lower()
    scheme = "wss" if forwarded_proto == "https" else "ws"
    host = request.headers.get("x-forwarded-host") or request.url.netloc
    return f"{scheme}://{host}"


# ────────────────────────────────────────────────────────────────────────────
# REST
# ────────────────────────────────────────────────────────────────────────────


@router.post("/sessions", response_model=SessionCreateResponse, status_code=status.HTTP_201_CREATED)
@limiter.limit("120/minute")
async def create_session(
    request: Request,
    payload: SessionCreateRequest,
    auth: tuple[OrganizationApiKey, Organization, TenantResources] = Depends(deps.get_api_key_record),
    db: AsyncSession = Depends(deps.get_db),
):
    api_key, organization, _tenant_res = auth
    if not await _acquire_slot(api_key):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Concurrent session limit reached for this API key",
        )
    try:
        record = await ConnectSessionService.create(
            db,
            api_key=api_key,
            organization=organization,
            channel=payload.channel,
            language=payload.language,
            client_session_id=payload.client_session_id,
            origin=request.headers.get("origin"),
            metadata=payload.metadata,
        )
    except Exception:
        await _release_slot(api_key.id)
        raise

    response = SessionCreateResponse(
        session=_serialize_session(record),
        websocket_url=(
            f"{_ws_base_url(request)}/api/voice/sessions/{record.id}/stream"
            if payload.channel == "voice"
            else None
        ),
    )
    return response


@router.get("/sessions/{session_id}", response_model=SessionResponse)
@limiter.limit("240/minute")
async def get_session(
    request: Request,
    session_id: uuid.UUID,
    auth: tuple[OrganizationApiKey, Organization, TenantResources] = Depends(deps.get_api_key_record),
    db: AsyncSession = Depends(deps.get_db),
):
    api_key, organization, _tenant_res = auth
    record = await ConnectSessionService.get(db, session_id=session_id, organization_id=organization.id)
    if record is None or record.api_key_id != api_key.id:
        raise HTTPException(status_code=404, detail="Session not found")
    return _serialize_session(record)


@router.post("/sessions/{session_id}/message", response_model=TextMessageResponse)
@limiter.limit("120/minute")
async def post_text_message(
    request: Request,
    session_id: uuid.UUID,
    payload: TextMessageRequest,
    auth: tuple[OrganizationApiKey, Organization, TenantResources] = Depends(deps.get_api_key_record),
    db: AsyncSession = Depends(deps.get_db),
):
    api_key, organization, tenant_res = auth
    record = await ConnectSessionService.get(db, session_id=session_id, organization_id=organization.id)
    if record is None or record.api_key_id != api_key.id:
        raise HTTPException(status_code=404, detail="Session not found")
    if record.status not in {"created", "live"}:
        raise HTTPException(status_code=409, detail=f"Session is {record.status}")

    user_entry = await ConnectSessionService.append_turn(
        db, record, role="user", content=payload.content
    )

    history = [
        {"role": item.get("role"), "content": item.get("content")}
        for item in (record.transcript or [])
        if item.get("role") in {"user", "assistant"}
    ]
    history_for_runtime = history[:-1]  # exclude the message we just appended

    try:
        result = await AgentRuntimeService.answer_text(
            tenant_res,
            payload.content,
            db=db,
            response_language=record.language,
            conversation_history=history_for_runtime,
        )
    except Exception as exc:
        await ConnectSessionService.end(db, record, status="failed", reason=str(exc)[:200])
        await _release_slot(api_key.id)
        raise HTTPException(status_code=502, detail="Agent runtime failed") from exc

    answer = str(result.get("answer") or "")
    assistant_entry = await ConnectSessionService.append_turn(
        db,
        record,
        role="assistant",
        content=answer,
        extra={"citations": result.get("citations") or []},
    )

    # Crude usage attribution: 4 chars ≈ 1 token. Replace with real Azure
    # token counts once the agent_runtime returns them.
    user_tokens = max(1, len(payload.content) // 4)
    assistant_tokens = max(1, len(answer) // 4)
    await ConnectSessionService.bump_usage(
        db,
        record,
        deltas={
            "tts_characters": float(len(answer)),
            "llm_input_tokens": float(user_tokens),
            "llm_output_tokens": float(assistant_tokens),
        },
    )

    await db.refresh(record)
    return TextMessageResponse(
        session_id=record.id,
        user_message=user_entry,
        assistant_message=assistant_entry,
        usage=dict(record.usage or {}),
    )


@router.post("/sessions/{session_id}/end", response_model=SessionResponse)
@limiter.limit("120/minute")
async def end_session(
    request: Request,
    session_id: uuid.UUID,
    auth: tuple[OrganizationApiKey, Organization, TenantResources] = Depends(deps.get_api_key_record),
    db: AsyncSession = Depends(deps.get_db),
):
    api_key, organization, _tenant_res = auth
    record = await ConnectSessionService.get(db, session_id=session_id, organization_id=organization.id)
    if record is None or record.api_key_id != api_key.id:
        raise HTTPException(status_code=404, detail="Session not found")
    if record.status in {"completed", "failed", "expired"}:
        return _serialize_session(record)
    closed = await ConnectSessionService.end(db, record, status="completed")
    await _release_slot(api_key.id)
    return _serialize_session(closed)


# ────────────────────────────────────────────────────────────────────────────
# WebSocket bridge
# ────────────────────────────────────────────────────────────────────────────


@router.websocket("/sessions/{session_id}/stream")
async def stream_session(websocket: WebSocket, session_id: uuid.UUID):
    # Pull the key off the query string (browser SDK case) OR a subprotocol
    # header (recommended for server-to-server). Reject before .accept() so
    # we surface a clean 4xx-equivalent close code.
    raw_key = websocket.query_params.get("api_key") or websocket.query_params.get("key")
    if not raw_key:
        auth_hdr = websocket.headers.get("authorization") or ""
        if auth_hdr.lower().startswith("bearer "):
            raw_key = auth_hdr.split(" ", 1)[1].strip()
    parsed = api_key_utils.parse(raw_key)
    if parsed is None:
        await websocket.close(code=1008)
        return

    async for db in deps.get_db():
        key_res = await db.execute(
            select(OrganizationApiKey).where(OrganizationApiKey.key_prefix == parsed.key_prefix)
        )
        api_key = key_res.scalars().first()
        if api_key is None or api_key.status != "active":
            await websocket.close(code=1008)
            return
        if api_key.expires_at is not None and api_key.expires_at <= datetime.now(timezone.utc):
            await websocket.close(code=1008)
            return
        if not api_key_utils.verify(parsed, api_key.secret_hash):
            await websocket.close(code=1008)
            return

        origin = websocket.headers.get("origin")
        allowed = list(api_key.allowed_origins or [])
        if origin and allowed and origin not in allowed:
            await websocket.close(code=1008)
            return

        record_res = await db.execute(
            select(ConnectSession).where(ConnectSession.id == session_id)
        )
        record = record_res.scalars().first()
        if record is None or record.api_key_id != api_key.id:
            await websocket.close(code=1008)
            return
        if record.status not in {"created", "live"}:
            await websocket.close(code=1008)
            return

        org_res = await db.execute(
            select(Organization).where(Organization.id == api_key.organization_id)
        )
        organization = org_res.scalars().first()
        if organization is None or (organization.product_tier or "nokvo_prime") != "nokvo_one":
            # Tier downgraded after key was minted — refuse the upgrade.
            await websocket.close(code=1008)
            return

        tr_res = await db.execute(
            select(TenantResources).where(TenantResources.organization_id == api_key.organization_id)
        )
        tenant_res = tr_res.scalars().first()
        if tenant_res is None:
            await websocket.close(code=1011)
            return

        try:
            await NokvoOneVoiceStreamService.run_session(
                websocket,
                tenant_res,
                db=db,
                language=record.language,
                call_id=f"connect:{record.id}",
            )
        finally:
            try:
                # Pipeline ran; close out the session row and decrement the
                # concurrency slot regardless of how the WS ended.
                await ConnectSessionService.end(
                    db, record, status="completed", reason="stream_closed"
                )
            except Exception:
                logger.exception("connect session cleanup on stream close failed")
            await _release_slot(api_key.id)
        return
