"""Clinic services API.

Only meaningful for organizations whose ``industry`` is ``clinics``. The clinic
defines its service catalog here and maps each service to the doctors who
provide it; the inbound agent reads this to answer service questions and to
route a booking service-first to an eligible doctor.
"""
from __future__ import annotations

import uuid
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.clinic_service import ClinicService, ClinicServiceProvider
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser


router = APIRouter()


def _agent_dep():
    return deps.RequireNokvoOneOrganization(allowed_statuses=["active"])


def _admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["active"],
        allowed_roles=["admin", "manager"],
    )


async def _ensure_clinics(db: AsyncSession, organization_id: uuid.UUID) -> Organization:
    res = await db.execute(select(Organization).where(Organization.id == organization_id))
    organization = res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if (organization.industry or "").lower() != "clinics":
        raise HTTPException(
            status_code=400,
            detail="Services are only available for clinic organizations.",
        )
    return organization


# ── Schemas ───────────────────────────────────────────────────────────────


class ClinicServiceCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str | None = None
    department: str | None = Field(default=None, max_length=160)
    duration_minutes: int | None = Field(default=None, ge=0, le=1440)
    price: Decimal | None = None
    price_display: str | None = Field(default=None, max_length=120)
    is_active: bool = True
    doctor_ids: list[uuid.UUID] = Field(default_factory=list)


class ClinicServiceUpdateRequest(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    department: str | None = Field(default=None, max_length=160)
    duration_minutes: int | None = Field(default=None, ge=0, le=1440)
    price: Decimal | None = None
    price_display: str | None = Field(default=None, max_length=120)
    is_active: bool | None = None


class ClinicServiceResponse(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    description: str | None
    department: str | None
    duration_minutes: int | None
    price: float | None
    price_display: str | None
    is_active: bool
    doctor_ids: list[uuid.UUID]


class DoctorResponse(BaseModel):
    id: uuid.UUID
    name: str
    email: str
    role: str


class SetDoctorsRequest(BaseModel):
    doctor_ids: list[uuid.UUID] = Field(default_factory=list)


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _doctor_ids_for(db: AsyncSession, service_id: uuid.UUID) -> list[uuid.UUID]:
    res = await db.execute(
        select(ClinicServiceProvider.member_id).where(
            ClinicServiceProvider.service_id == service_id
        )
    )
    return [row[0] for row in res.all()]


async def _serialize(db: AsyncSession, svc: ClinicService) -> ClinicServiceResponse:
    return ClinicServiceResponse(
        id=svc.id,
        organization_id=svc.organization_id,
        name=svc.name,
        description=svc.description,
        department=svc.department,
        duration_minutes=svc.duration_minutes,
        price=float(svc.price) if svc.price is not None else None,
        price_display=svc.price_display,
        is_active=svc.is_active,
        doctor_ids=await _doctor_ids_for(db, svc.id),
    )


async def _set_doctors(
    db: AsyncSession, organization_id: uuid.UUID, service_id: uuid.UUID, doctor_ids: list[uuid.UUID]
) -> None:
    """Replace the provider mapping for a service. Only members of this org are kept."""
    await db.execute(
        sa_delete(ClinicServiceProvider).where(ClinicServiceProvider.service_id == service_id)
    )
    if doctor_ids:
        valid = {
            row[0]
            for row in (
                await db.execute(
                    select(OrganizationUser.id).where(
                        OrganizationUser.organization_id == organization_id,
                        OrganizationUser.id.in_(doctor_ids),
                    )
                )
            ).all()
        }
        for mid in doctor_ids:
            if mid in valid:
                db.add(ClinicServiceProvider(id=uuid.uuid4(), service_id=service_id, member_id=mid))


# ── Endpoints ───────────────────────────────────────────────────────────────


@router.get("/doctors", response_model=list[DoctorResponse])
async def list_doctors(
    user: OrganizationUser = Depends(_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Org members available to map to services (for the mapping UI)."""
    await _ensure_clinics(db, user.organization_id)
    res = await db.execute(
        select(OrganizationUser)
        .where(OrganizationUser.organization_id == user.organization_id)
        .order_by(OrganizationUser.full_name.asc())
    )
    return [
        DoctorResponse(id=u.id, name=(u.full_name or u.email), email=u.email, role=u.role)
        for u in res.scalars().all()
    ]


@router.get("/", response_model=list[ClinicServiceResponse])
async def list_services(
    user: OrganizationUser = Depends(_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _ensure_clinics(db, user.organization_id)
    res = await db.execute(
        select(ClinicService)
        .where(ClinicService.organization_id == user.organization_id)
        .order_by(ClinicService.name.asc())
    )
    return [await _serialize(db, s) for s in res.scalars().all()]


@router.post("/", response_model=ClinicServiceResponse, status_code=status.HTTP_201_CREATED)
async def create_service(
    payload: ClinicServiceCreateRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _ensure_clinics(db, user.organization_id)
    svc = ClinicService(
        id=uuid.uuid4(),
        organization_id=user.organization_id,
        name=payload.name,
        description=payload.description,
        department=payload.department,
        duration_minutes=payload.duration_minutes,
        price=payload.price,
        price_display=payload.price_display,
        is_active=payload.is_active,
        created_by_user_id=user.id,
    )
    db.add(svc)
    await db.flush()
    await _set_doctors(db, user.organization_id, svc.id, payload.doctor_ids)
    from app.services.offering_sync import sync_service_offering
    await sync_service_offering(db, svc)  # dual-write mirror (no-op unless OFFERINGS_DUAL_WRITE)
    await db.commit()
    await db.refresh(svc)
    return await _serialize(db, svc)


async def _load_owned(db: AsyncSession, organization_id: uuid.UUID, service_id: uuid.UUID) -> ClinicService:
    res = await db.execute(
        select(ClinicService).where(
            ClinicService.id == service_id,
            ClinicService.organization_id == organization_id,
        )
    )
    svc = res.scalars().first()
    if svc is None:
        raise HTTPException(status_code=404, detail="Service not found")
    return svc


@router.patch("/{service_id}", response_model=ClinicServiceResponse)
async def update_service(
    service_id: uuid.UUID,
    payload: ClinicServiceUpdateRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _ensure_clinics(db, user.organization_id)
    svc = await _load_owned(db, user.organization_id, service_id)
    for field, value in payload.model_dump(exclude_unset=True).items():
        setattr(svc, field, value)
    db.add(svc)
    from app.services.offering_sync import sync_service_offering
    await sync_service_offering(db, svc)  # dual-write mirror (no-op unless OFFERINGS_DUAL_WRITE)
    await db.commit()
    await db.refresh(svc)
    return await _serialize(db, svc)


@router.put("/{service_id}/doctors", response_model=ClinicServiceResponse)
async def set_service_doctors(
    service_id: uuid.UUID,
    payload: SetDoctorsRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _ensure_clinics(db, user.organization_id)
    svc = await _load_owned(db, user.organization_id, service_id)
    await _set_doctors(db, user.organization_id, svc.id, payload.doctor_ids)
    await db.commit()
    await db.refresh(svc)
    return await _serialize(db, svc)


@router.delete("/{service_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_service(
    service_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _ensure_clinics(db, user.organization_id)
    svc = await _load_owned(db, user.organization_id, service_id)
    from app.services.offering_sync import delete_offering
    await delete_offering(db, svc.id)  # dual-write mirror (no-op unless OFFERINGS_DUAL_WRITE)
    await db.delete(svc)
    await db.commit()
