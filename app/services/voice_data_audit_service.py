from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit import VoiceDataAccessAuditLog
from app.models.tenant_resources import TenantResources


logger = logging.getLogger(__name__)


class VoiceDataAuditService:
    @staticmethod
    async def log(
        db: AsyncSession | None,
        *,
        organization_id: uuid.UUID,
        tenant_id: str | None = None,
        actor_type: str,
        actor_id: str | None = None,
        access_type: str,
        resource_type: str,
        resource_id: str | None = None,
        call_id: str | None = None,
        reason: str | None = None,
        request: Request | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        if db is None:
            return
        try:
            db.add(
                VoiceDataAccessAuditLog(
                    id=uuid.uuid4(),
                    organization_id=organization_id,
                    tenant_id=tenant_id,
                    actor_type=actor_type,
                    actor_id=actor_id,
                    access_type=access_type,
                    resource_type=resource_type,
                    resource_id=resource_id,
                    call_id=call_id,
                    reason=reason,
                    ip_address=(request.client.host if request and request.client else None),
                    user_agent=(request.headers.get("user-agent") if request else None),
                    request_id=(request.headers.get("x-request-id") if request else None),
                    metadata_=metadata or {},
                )
            )
            await db.commit()
        except Exception:
            await db.rollback()
            logger.warning("voice data audit log write failed", exc_info=True)
            if settings.VOICE_DATA_AUDIT_ENFORCED:
                raise

    @staticmethod
    async def log_tenant_access(
        db: AsyncSession | None,
        tenant_res: TenantResources,
        *,
        actor_type: str,
        actor_id: str | None = None,
        access_type: str,
        resource_type: str,
        resource_id: str | None = None,
        call_id: str | None = None,
        reason: str | None = None,
        request: Request | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        await VoiceDataAuditService.log(
            db,
            organization_id=tenant_res.organization_id,
            tenant_id=tenant_res.tenant_id,
            actor_type=actor_type,
            actor_id=actor_id,
            access_type=access_type,
            resource_type=resource_type,
            resource_id=resource_id,
            call_id=call_id,
            reason=reason,
            request=request,
            metadata=metadata,
        )
