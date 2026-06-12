"""Real-estate projects API.

Only meaningful for organizations whose ``industry`` is ``real_estate``. The
inbound agent reads this catalog so it can answer questions about specific
projects and attach site visits / leads to the right one.
"""
from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.real_estate_project import RealEstateProject
from app.schemas.nokvo_one import (
    RealEstateProjectCreateRequest,
    RealEstateProjectResponse,
    RealEstateProjectUpdateRequest,
)


router = APIRouter()


def _agent_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["pending_approval", "active"],
    )


def _admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["pending_approval", "active"],
        allowed_roles=["admin", "manager"],
    )


async def _ensure_real_estate(db: AsyncSession, organization_id: uuid.UUID) -> Organization:
    res = await db.execute(select(Organization).where(Organization.id == organization_id))
    organization = res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if (organization.industry or "").lower() != "real_estate":
        raise HTTPException(
            status_code=400,
            detail="Projects are only available for real-estate organizations.",
        )
    return organization


def _serialize(project: RealEstateProject) -> RealEstateProjectResponse:
    return RealEstateProjectResponse(
        id=project.id,
        organization_id=project.organization_id,
        name=project.name,
        location=project.location,
        rera_number=project.rera_number,
        property_type=project.property_type,
        price_min=float(project.price_min) if project.price_min is not None else None,
        price_max=float(project.price_max) if project.price_max is not None else None,
        price_display=project.price_display,
        configurations=list(project.configurations or []),
        amenities=list(project.amenities or []),
        description=project.description,
        possession_date=project.possession_date,
        builder_name=project.builder_name,
        brochure_url=project.brochure_url,
        contact_phone=project.contact_phone,
        whatsapp=dict(project.whatsapp or {}),
        extra=dict(project.extra or {}),
        status=project.status,
        created_at=project.created_at,
        updated_at=project.updated_at,
    )


@router.get("/", response_model=list[RealEstateProjectResponse])
async def list_projects(
    user: OrganizationUser = Depends(_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _ensure_real_estate(db, user.organization_id)
    res = await db.execute(
        select(RealEstateProject)
        .where(RealEstateProject.organization_id == user.organization_id)
        .order_by(RealEstateProject.created_at.desc())
    )
    return [_serialize(p) for p in res.scalars().all()]


@router.post(
    "/",
    response_model=RealEstateProjectResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_project(
    payload: RealEstateProjectCreateRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _ensure_real_estate(db, user.organization_id)
    project = RealEstateProject(
        id=uuid.uuid4(),
        organization_id=user.organization_id,
        name=payload.name,
        location=payload.location,
        rera_number=payload.rera_number,
        property_type=payload.property_type,
        price_min=payload.price_min,
        price_max=payload.price_max,
        price_display=payload.price_display,
        configurations=payload.configurations,
        amenities=payload.amenities,
        description=payload.description,
        possession_date=payload.possession_date,
        builder_name=payload.builder_name,
        brochure_url=payload.brochure_url,
        contact_phone=payload.contact_phone,
        whatsapp=payload.whatsapp or {},
        extra=payload.extra or {},
        status=payload.status or "active",
        created_by_user_id=user.id,
    )
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _serialize(project)


@router.patch("/{project_id}", response_model=RealEstateProjectResponse)
async def update_project(
    project_id: uuid.UUID,
    payload: RealEstateProjectUpdateRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _ensure_real_estate(db, user.organization_id)
    res = await db.execute(
        select(RealEstateProject).where(
            RealEstateProject.id == project_id,
            RealEstateProject.organization_id == user.organization_id,
        )
    )
    project = res.scalars().first()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")

    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(project, field, value)
    db.add(project)
    await db.commit()
    await db.refresh(project)
    return _serialize(project)


@router.delete("/{project_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_project(
    project_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _ensure_real_estate(db, user.organization_id)
    res = await db.execute(
        select(RealEstateProject).where(
            RealEstateProject.id == project_id,
            RealEstateProject.organization_id == user.organization_id,
        )
    )
    project = res.scalars().first()
    if project is None:
        raise HTTPException(status_code=404, detail="Project not found")
    await db.delete(project)
    await db.commit()
