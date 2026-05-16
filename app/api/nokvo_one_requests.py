from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.member_assignment import MemberBlockedSlot
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.schemas.nokvo_one import (
    NokvoOneBlockedSlotResponse,
    NokvoOneBlockedSlotUpdateRequest,
    NokvoOneRequestAssignRequest,
    NokvoOneRequestAssignResponse,
)
from app.services.nokvo_one_assignment_service import NokvoOneAssignmentService


router = APIRouter()


async def _get_organization(db: AsyncSession, organization_id: uuid.UUID) -> Organization:
    res = await db.execute(select(Organization).where(Organization.id == organization_id))
    organization = res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organization


def _assignment_response(result: dict) -> NokvoOneRequestAssignResponse:
    return NokvoOneRequestAssignResponse(
        request_id=result.get("request_id"),
        organization_id=result["organization_id"],
        selected_member_id=result.get("selected_member_id"),
        request_type=result["request_type"],
        assignment_status=result["assignment_status"],
        reason=result.get("reason"),
        active_load_count=result.get("active_load_count"),
        skipped_member_reasons=result.get("skipped_member_reasons") or {},
        timestamp=result["timestamp"],
    )


@router.post("/requests/assign", response_model=NokvoOneRequestAssignResponse)
async def assign_new_request(
    payload: NokvoOneRequestAssignRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(allowed_statuses=["pending_approval", "active"])
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization(db, user.organization_id)
    try:
        result = await NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_type=payload.request_type,
            requested_time=payload.requested_time,
            summary=payload.summary,
            metadata=payload.metadata,
            record_type=payload.record_type,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return _assignment_response(result)


@router.post("/requests/{request_id}/assign", response_model=NokvoOneRequestAssignResponse)
async def assign_existing_request(
    request_id: uuid.UUID,
    payload: NokvoOneRequestAssignRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(allowed_statuses=["pending_approval", "active"])
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization(db, user.organization_id)
    try:
        result = await NokvoOneAssignmentService.assign_request(
            db,
            organization,
            request_id=request_id,
            request_type=payload.request_type,
            requested_time=payload.requested_time,
            summary=payload.summary,
            metadata=payload.metadata,
            record_type=payload.record_type,
        )
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    await db.commit()
    return _assignment_response(result)


@router.put("/blocked-slots/{blocked_slot_id}", response_model=NokvoOneBlockedSlotResponse)
async def update_blocked_slot(
    blocked_slot_id: uuid.UUID,
    payload: NokvoOneBlockedSlotUpdateRequest,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization(db, user.organization_id)
    if organization.industry != "clinics":
        raise HTTPException(status_code=404, detail="Blocked slots are only available for Clinics")
    res = await db.execute(
        select(MemberBlockedSlot).where(
            MemberBlockedSlot.id == blocked_slot_id,
            MemberBlockedSlot.organization_id == organization.id,
        )
    )
    slot = res.scalars().first()
    if slot is None:
        raise HTTPException(status_code=404, detail="Blocked slot not found")
    if payload.start_time is not None:
        slot.start_time = payload.start_time
    if payload.end_time is not None:
        slot.end_time = payload.end_time
    if slot.end_time <= slot.start_time:
        raise HTTPException(status_code=400, detail="Blocked slot end_time must be after start_time")
    if payload.reason is not None:
        slot.reason = payload.reason
    if payload.repeat_rule is not None:
        slot.repeat_rule = payload.repeat_rule
    db.add(slot)
    await db.commit()
    await db.refresh(slot)
    return NokvoOneBlockedSlotResponse.model_validate(slot)


@router.delete("/blocked-slots/{blocked_slot_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_blocked_slot(
    blocked_slot_id: uuid.UUID,
    user: OrganizationUser = Depends(
        deps.RequireNokvoOneOrganization(
            allowed_statuses=["pending_approval", "active"],
            allowed_roles=["admin", "manager"],
        )
    ),
    db: AsyncSession = Depends(deps.get_db),
):
    organization = await _get_organization(db, user.organization_id)
    if organization.industry != "clinics":
        raise HTTPException(status_code=404, detail="Blocked slots are only available for Clinics")
    res = await db.execute(
        select(MemberBlockedSlot).where(
            MemberBlockedSlot.id == blocked_slot_id,
            MemberBlockedSlot.organization_id == organization.id,
        )
    )
    slot = res.scalars().first()
    if slot is None:
        raise HTTPException(status_code=404, detail="Blocked slot not found")
    await db.delete(slot)
    await db.commit()
