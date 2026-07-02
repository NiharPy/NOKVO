"""Nokvo Connect — admin-side API key management.

Mounted under ``/api/nokvo-one/connect`` and authenticated with the standard
NOKVO ONE organization JWT (deps.RequireNokvoOneOrganization). Lets an admin
mint / revoke / inspect API keys without needing one to begin with.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core import api_keys as api_key_utils
from app.core.rate_limit import limiter  # noqa: F401 — keeps slowapi state object alive
from app.models.connect_api_key import OrganizationApiKey
from app.models.organization_user import OrganizationUser
from app.models.tenant_usage_event import TenantUsageEvent
from app.schemas.connect import (
    ApiKeyCreateRequest,
    ApiKeyCreatedResponse,
    ApiKeyResponse,
    ApiKeyUpdateRequest,
)


logger = logging.getLogger(__name__)
router = APIRouter()


def _admin_dep():
    # Nokvo Connect is a Nokvo One feature. Reject Prime tenants up front so
    # we don't accidentally hand out API keys against a Prime workspace, and
    # require the ``admin`` role on the Nokvo One side.
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["active"],
        allowed_roles=["admin"],
    )


def _serialize(record: OrganizationApiKey) -> ApiKeyResponse:
    return ApiKeyResponse.model_validate(record)


@router.get("/api-keys", response_model=list[ApiKeyResponse])
async def list_api_keys(
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    res = await db.execute(
        select(OrganizationApiKey)
        .where(OrganizationApiKey.organization_id == user.organization_id)
        .order_by(OrganizationApiKey.created_at.desc())
    )
    return [_serialize(row) for row in res.scalars().all()]


@router.post("/api-keys", response_model=ApiKeyCreatedResponse, status_code=status.HTTP_201_CREATED)
async def create_api_key(
    payload: ApiKeyCreateRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    minted = api_key_utils.mint(payload.mode)
    webhook_raw = None
    webhook_hash = None
    if payload.webhook_url is not None:
        webhook_raw, webhook_hash = api_key_utils.mint_webhook_secret()

    record = OrganizationApiKey(
        id=uuid.uuid4(),
        organization_id=user.organization_id,
        label=payload.label,
        key_prefix=minted.key_prefix,
        secret_hash=minted.secret_hash,
        mode=minted.mode,
        scopes=list(payload.scopes),
        allowed_origins=list(payload.allowed_origins),
        rate_limit_rpm=payload.rate_limit_rpm,
        max_concurrent_sessions=payload.max_concurrent_sessions,
        webhook_url=str(payload.webhook_url) if payload.webhook_url else None,
        webhook_secret_hash=webhook_hash,
        created_by_user_id=user.id,
        expires_at=payload.expires_at,
    )
    db.add(record)
    await db.commit()
    await db.refresh(record)

    return ApiKeyCreatedResponse(
        api_key=_serialize(record),
        secret=minted.raw,
        webhook_secret=webhook_raw,
    )


@router.patch("/api-keys/{key_id}", response_model=ApiKeyResponse)
async def update_api_key(
    key_id: uuid.UUID,
    payload: ApiKeyUpdateRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    res = await db.execute(
        select(OrganizationApiKey).where(
            OrganizationApiKey.id == key_id,
            OrganizationApiKey.organization_id == user.organization_id,
        )
    )
    record = res.scalars().first()
    if record is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if record.status != "active":
        raise HTTPException(status_code=409, detail="API key is not active")

    if payload.label is not None:
        record.label = payload.label
    if payload.scopes is not None:
        record.scopes = list(payload.scopes)
    if payload.allowed_origins is not None:
        record.allowed_origins = list(payload.allowed_origins)
    if payload.rate_limit_rpm is not None:
        record.rate_limit_rpm = payload.rate_limit_rpm
    if payload.max_concurrent_sessions is not None:
        record.max_concurrent_sessions = payload.max_concurrent_sessions
    if payload.webhook_url is not None:
        record.webhook_url = str(payload.webhook_url)
        if not record.webhook_secret_hash:
            # First time a webhook URL is set — mint a signing secret. We
            # can't return it on this PATCH (no one-shot envelope), so
            # require the dashboard to call a rotate endpoint for that.
            pass
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _serialize(record)


@router.post("/api-keys/{key_id}/revoke", response_model=ApiKeyResponse)
async def revoke_api_key(
    key_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    res = await db.execute(
        select(OrganizationApiKey).where(
            OrganizationApiKey.id == key_id,
            OrganizationApiKey.organization_id == user.organization_id,
        )
    )
    record = res.scalars().first()
    if record is None:
        raise HTTPException(status_code=404, detail="API key not found")
    if record.status == "revoked":
        return _serialize(record)
    record.status = "revoked"
    record.revoked_at = datetime.now(timezone.utc)
    record.revoke_reason = "admin_revoke"
    db.add(record)
    await db.commit()
    await db.refresh(record)
    return _serialize(record)


@router.get("/api-keys/{key_id}/usage")
async def api_key_usage(
    key_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    # Per-key usage rollup. We tagged every Connect-emitted usage row with
    # ``metadata.api_key_id``; aggregate across the calendar month.
    res = await db.execute(
        select(OrganizationApiKey).where(
            OrganizationApiKey.id == key_id,
            OrganizationApiKey.organization_id == user.organization_id,
        )
    )
    record = res.scalars().first()
    if record is None:
        raise HTTPException(status_code=404, detail="API key not found")

    usage_rows = (
        await db.execute(
            select(TenantUsageEvent)
            .where(TenantUsageEvent.organization_id == user.organization_id)
            .order_by(TenantUsageEvent.created_at.desc())
            .limit(500)
        )
    ).scalars().all()
    rollup = {
        "stt_minutes": 0.0,
        "tts_characters": 0,
        "llm_input_tokens": 0,
        "llm_output_tokens": 0,
        "cost_usd": 0.0,
        "session_count": 0,
    }
    for row in usage_rows:
        meta = dict(row.metadata_ or {})
        if str(meta.get("api_key_id") or "") != str(key_id):
            continue
        rollup["stt_minutes"] += float(row.stt_minutes or 0)
        rollup["tts_characters"] += int(row.tts_characters or 0)
        rollup["llm_input_tokens"] += int(row.llm_input_tokens or 0)
        rollup["llm_output_tokens"] += int(row.llm_output_tokens or 0)
        rollup["cost_usd"] += float(row.cost_usd or 0)
        rollup["session_count"] += 1
    return {"api_key_id": str(key_id), "rollup": rollup}
