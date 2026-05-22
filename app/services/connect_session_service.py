"""Business logic for Nokvo Connect sessions.

* Mint a ConnectSession row.
* Append turns to the persisted transcript.
* Roll usage counters into both the session row AND a TenantUsageEvent so the
  existing billing pipeline sees Connect traffic without a separate path.
* Drive the existing agent_runtime stack for text turns; voice turns go
  through the WebSocket adapter elsewhere.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.models.connect_api_key import OrganizationApiKey
from app.models.connect_session import ConnectSession
from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.models.tenant_usage_event import TenantUsageEvent

logger = logging.getLogger(__name__)


_IDLE_TIMEOUT_SECONDS = 90


class ConnectSessionService:
    @staticmethod
    async def create(
        db: AsyncSession,
        *,
        api_key: OrganizationApiKey,
        organization: Organization,
        channel: str,
        language: str,
        client_session_id: str | None,
        origin: str | None,
        metadata: dict[str, Any],
    ) -> ConnectSession:
        record = ConnectSession(
            id=uuid.uuid4(),
            organization_id=organization.id,
            api_key_id=api_key.id,
            channel=channel,
            status="created",
            language=language or "en",
            client_session_id=client_session_id,
            origin=origin,
            transcript=[],
            usage={
                "stt_minutes": 0.0,
                "tts_characters": 0,
                "llm_input_tokens": 0,
                "llm_output_tokens": 0,
                "messages": 0,
            },
            metadata_={"created_via": "connect_api", **(metadata or {})},
        )
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return record

    @staticmethod
    async def get(
        db: AsyncSession,
        *,
        session_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> ConnectSession | None:
        from sqlalchemy import select
        res = await db.execute(
            select(ConnectSession).where(
                ConnectSession.id == session_id,
                ConnectSession.organization_id == organization_id,
            )
        )
        return res.scalars().first()

    @staticmethod
    async def append_turn(
        db: AsyncSession,
        record: ConnectSession,
        *,
        role: str,
        content: str,
        extra: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        entry = {
            "role": role,
            "content": content,
            "ts": datetime.now(timezone.utc).isoformat(),
        }
        if extra:
            entry.update(extra)
        transcript = list(record.transcript or [])
        transcript.append(entry)
        record.transcript = transcript
        flag_modified(record, "transcript")
        usage = dict(record.usage or {})
        usage["messages"] = int(usage.get("messages") or 0) + 1
        record.usage = usage
        flag_modified(record, "usage")
        if record.status == "created":
            record.status = "live"
            record.started_at = datetime.now(timezone.utc)
        db.add(record)
        await db.commit()
        await db.refresh(record)
        return entry

    @staticmethod
    async def bump_usage(
        db: AsyncSession,
        record: ConnectSession,
        *,
        deltas: dict[str, float],
    ) -> None:
        usage = dict(record.usage or {})
        for key, value in deltas.items():
            usage[key] = float(usage.get(key) or 0) + float(value)
        record.usage = usage
        flag_modified(record, "usage")
        db.add(record)
        await db.commit()

    @staticmethod
    async def end(
        db: AsyncSession,
        record: ConnectSession,
        *,
        status: str = "completed",
        reason: str | None = None,
        cost_usd: float = 0.0,
    ) -> ConnectSession:
        record.status = status
        record.failure_reason = reason
        record.ended_at = datetime.now(timezone.utc)
        db.add(record)

        # Mirror final usage into TenantUsageEvent so the standing billing
        # rollup picks the session up. Tag with api_key_id so the per-key
        # usage endpoint can filter to just this Connect key.
        usage = dict(record.usage or {})
        try:
            from sqlalchemy import select
            tr_res = await db.execute(
                select(TenantResources).where(
                    TenantResources.organization_id == record.organization_id
                )
            )
            tenant_res = tr_res.scalars().first()
            if tenant_res is not None:
                event = TenantUsageEvent(
                    id=uuid.uuid4(),
                    organization_id=record.organization_id,
                    tenant_id=tenant_res.tenant_id,
                    event_type=f"connect_api_{record.channel}_session",
                    minutes=int(usage.get("stt_minutes") or 0),
                    stt_minutes=float(usage.get("stt_minutes") or 0),
                    tts_characters=int(usage.get("tts_characters") or 0),
                    llm_api_calls=int(usage.get("messages") or 0),
                    llm_input_tokens=int(usage.get("llm_input_tokens") or 0),
                    llm_output_tokens=int(usage.get("llm_output_tokens") or 0),
                    cost_usd=cost_usd,
                    metadata_={
                        "api_key_id": str(record.api_key_id),
                        "session_id": str(record.id),
                        "channel": record.channel,
                        "client_session_id": record.client_session_id,
                    },
                )
                db.add(event)
        except Exception:
            logger.exception("connect session usage event write failed")
        await db.commit()
        await db.refresh(record)
        return record

    @staticmethod
    def is_idle(record: ConnectSession) -> bool:
        if record.status not in {"created", "live"}:
            return False
        anchor = record.started_at or record.created_at
        if anchor is None:
            return False
        delta = (datetime.now(timezone.utc) - anchor).total_seconds()
        return delta > _IDLE_TIMEOUT_SECONDS
