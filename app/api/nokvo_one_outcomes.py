from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.organization_user import OrganizationUser
from app.services.agent_spec import spec_snapshot
from app.services.outcome_tracker import OutcomeTracker, VALID_OUTCOMES
from app.services.tool_retry_service import ToolRetryService


router = APIRouter()


@router.get("/agent/spec")
async def agent_spec():
    """Canonical agent contract — capabilities, confirmation policy,
    retry policy, identity rules, valid outcomes. Chat/voice/outbound
    clients use this to verify they're built against the right spec
    version and to render UI affordances that match what the backend
    will actually do."""
    return spec_snapshot()


@router.get("/agent/retries")
async def list_pending_retries(
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    """Pending tool retries for this organization. Operator dashboard
    surface for the queue — shows what's stuck and why."""
    rows = await ToolRetryService.list_pending(db, organization_id=user.organization_id, limit=200)
    return [
        {
            "id": str(row.id),
            "tool_key": row.tool_key,
            "status": row.status,
            "attempts": row.attempts,
            "last_error": row.last_error,
            "next_attempt_at": row.next_attempt_at.isoformat() if row.next_attempt_at else None,
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "arguments": row.arguments,
            "context": row.context,
        }
        for row in rows
    ]


@router.post("/agent/retries/process")
async def process_pending_retries(
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    """Drain due retries for this organization. Safe to call repeatedly —
    only rows whose backoff window has elapsed get re-tried."""
    report = await ToolRetryService.process_due(db, organization_id=user.organization_id)
    return {"processed": len(report), "results": report}


@router.get("/agent/campaigns/{campaign_id}/outcomes")
async def campaign_outcomes(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    """Per-campaign outcome aggregation — how many records ended up confirmed
    vs no-show vs failed_followup vs pending. Powers the post-campaign
    dashboard view and the per-campaign retry / reschedule decisions."""
    return await OutcomeTracker.aggregate_for_campaign(
        db,
        organization_id=user.organization_id,
        campaign_id=campaign_id,
    )


@router.post("/agent/records/{record_id}/auto-followup")
async def trigger_auto_followup(
    record_id: uuid.UUID,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    """Idempotent: ensures a follow-up callback exists for the given record
    if its outcome warrants one (no_show / failed_followup). Useful for the
    ops team to manually escalate a record."""
    cb = await OutcomeTracker.auto_followup_if_needed(
        db, organization_id=user.organization_id, record_id=record_id
    )
    if cb is None:
        return {"created": False, "reason": "no follow-up needed for current outcome"}
    return {"created": True, "callback_record_id": str(cb.id)}


@router.post("/agent/retries/{retry_id}/resolve")
async def resolve_retry(
    retry_id: uuid.UUID,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    """Manual resolution — operator worked around the failure by hand and
    wants the queue entry closed out."""
    row = await ToolRetryService.resolve_manually(db, retry_id=retry_id, note="resolved via API")
    if row is None:
        raise HTTPException(status_code=404, detail="Retry not found")
    return {"id": str(row.id), "status": row.status, "resolved_at": row.resolved_at.isoformat() if row.resolved_at else None}


class OutcomeRequest(BaseModel):
    status: str = Field(min_length=1, max_length=40)
    source: str = Field(default="ops_dashboard", max_length=80)
    notes: str | None = Field(default=None, max_length=2000)


class OutcomeResponse(BaseModel):
    record_id: uuid.UUID
    status: str
    recorded_at: str


@router.post(
    "/records/{record_id}/outcome",
    response_model=OutcomeResponse,
    status_code=status.HTTP_200_OK,
)
async def record_outcome(
    record_id: uuid.UUID,
    payload: OutcomeRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    if payload.status not in VALID_OUTCOMES:
        raise HTTPException(
            status_code=400,
            detail=f"status must be one of: {', '.join(sorted(VALID_OUTCOMES))}",
        )
    record = await OutcomeTracker.record_outcome(
        db,
        organization_id=user.organization_id,
        record_id=record_id,
        status=payload.status,
        source=payload.source,
        notes=payload.notes,
    )
    if record is None:
        raise HTTPException(status_code=404, detail="Record not found")
    outcome = (record.data or {}).get("outcome") or {}
    return OutcomeResponse(
        record_id=record_id,
        status=outcome.get("status", payload.status),
        recorded_at=outcome.get("at") or "",
    )
