from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel

from app.models.outbound_campaign import CampaignStatus


class ContactStatus(BaseModel):
    phone: str
    name: str
    status: str = "pending"  # pending | calling | answered | no_answer | failed
    call_id: str | None = None
    call_link_id: str = ""
    duration_s: int | None = None
    answered_at: str | None = None


class CampaignCreate(BaseModel):
    name: str
    from_number: str | None = None  # caller ID; defaults to first linked number


class CampaignOut(BaseModel):
    id: uuid.UUID
    tenant_id: str
    name: str
    status: CampaignStatus
    from_number: str | None
    total_count: int
    answered_count: int
    failed_count: int
    created_at: datetime | None
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class CampaignDetailOut(CampaignOut):
    contacts: list[dict[str, Any]] = []
    agent_config: dict[str, Any] = {}
