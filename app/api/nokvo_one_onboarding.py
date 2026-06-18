"""Post-payment onboarding wizard API.

A single linear, **resumable** flow the admin walks after paying:

    business_details → documents → working_hours → projects → agent → terms → done

The org sits in ``status="onboarding"`` for the whole wizard; each endpoint
advances ``Organization.onboarding_step`` so a refresh or re-login resumes exactly
where the user left off. The ``terms`` step flips the org to ``active`` and lands
the user on the dashboard. Projects are added via the existing
``/api/nokvo-one/projects`` endpoints (their deps now allow ``onboarding``); a
``/projects/done`` advance moves the wizard on.
"""
from __future__ import annotations

import uuid
from datetime import datetime, time, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.member_assignment import OrganizationAssignmentDefaults
from app.models.nokvo_one_agent import NokvoOneAgent
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.real_estate_project import RealEstateProject
from app.models.tenant_resources import TenantResources
from app.services.plivo_compliance_service import PlivoComplianceService

router = APIRouter()

# Version stamped on the org when the user accepts the legal docs.
TERMS_VERSION = "2026-06-18"

_STEP_ORDER = ["business_details", "documents", "working_hours", "projects", "agent", "terms", "done"]


def _onboarding_admin_dep():
    return deps.RequireNokvoOneOrganization(allowed_statuses=["onboarding"], allowed_roles=["admin"])


async def _org(db: AsyncSession, user: OrganizationUser) -> Organization:
    org = (await db.execute(select(Organization).where(Organization.id == user.organization_id))).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


async def _tenant(db: AsyncSession, user: OrganizationUser) -> TenantResources:
    tr = (
        await db.execute(select(TenantResources).where(TenantResources.organization_id == user.organization_id))
    ).scalars().first()
    if tr is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found")
    return tr


def _advance(org: Organization, *, after: str) -> str:
    """Move onboarding_step to the step that follows ``after`` (never backwards)."""
    nxt = _STEP_ORDER[min(_STEP_ORDER.index(after) + 1, len(_STEP_ORDER) - 1)]
    cur = org.onboarding_step or "business_details"
    if cur not in _STEP_ORDER or _STEP_ORDER.index(nxt) > _STEP_ORDER.index(cur):
        org.onboarding_step = nxt
    return org.onboarding_step


def _parse_time(value: str | None) -> time | None:
    if not value:
        return None
    try:
        hh, mm = str(value).split(":")[:2]
        return time(hour=int(hh), minute=int(mm))
    except (ValueError, TypeError):
        return None


# ───────────────────────── request bodies ─────────────────────────
class BusinessDetailsRequest(BaseModel):
    legal_name: str
    alias_name: str | None = None
    business_pan: str | None = None
    cin: str | None = None


class WorkingHoursRequest(BaseModel):
    working_days: list[str] = []
    start_time: str | None = None
    end_time: str | None = None
    timezone: str | None = None


class AgentRequest(BaseModel):
    name: str


class TermsRequest(BaseModel):
    terms_accepted: bool = False
    privacy_accepted: bool = False


# ───────────────────────── endpoints ─────────────────────────
@router.get("/state")
async def onboarding_state(
    user: OrganizationUser = Depends(_onboarding_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    org = await _org(db, user)
    tr = await _tenant(db, user)
    plivo = dict((tr.provider_status or {}).get("plivo") or {})
    compliance = dict(plivo.get("compliance") or {})
    defaults = (
        await db.execute(
            select(OrganizationAssignmentDefaults).where(
                OrganizationAssignmentDefaults.organization_id == org.id
            )
        )
    ).scalars().first()
    projects_count = (
        await db.execute(
            select(func.count(RealEstateProject.id)).where(RealEstateProject.organization_id == org.id)
        )
    ).scalar() or 0
    agent = (
        await db.execute(
            select(NokvoOneAgent).where(NokvoOneAgent.organization_id == org.id).limit(1)
        )
    ).scalars().first()
    return {
        "onboarding_step": org.onboarding_step or "business_details",
        "business_details": {
            "legal_name": org.legal_name,
            "alias_name": org.alias_name,
            "business_pan": org.business_pan,
            "cin": org.cin,
        },
        "number": plivo.get("number"),
        "number_status": plivo.get("number_status"),
        "compliance_status": compliance.get("status"),
        "working_hours": {
            "working_days": (defaults.working_days if defaults else []) or [],
            "start_time": defaults.start_time.strftime("%H:%M") if defaults and defaults.start_time else None,
            "end_time": defaults.end_time.strftime("%H:%M") if defaults and defaults.end_time else None,
            "timezone": defaults.timezone if defaults else "Asia/Kolkata",
        },
        "projects_count": int(projects_count),
        "agent_name": agent.name if agent else None,
        "terms": {
            "terms_accepted": org.terms_accepted_at is not None,
            "privacy_accepted": org.privacy_accepted_at is not None,
        },
    }


@router.post("/business-details")
async def save_business_details(
    payload: BusinessDetailsRequest,
    user: OrganizationUser = Depends(_onboarding_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    if not payload.legal_name.strip():
        raise HTTPException(status_code=400, detail="Company legal name is required.")
    org = await _org(db, user)
    org.legal_name = payload.legal_name.strip()
    org.alias_name = (payload.alias_name or "").strip() or None
    org.business_pan = (payload.business_pan or "").strip() or None
    org.cin = (payload.cin or "").strip() or None
    step = _advance(org, after="business_details")
    db.add(org)
    await db.commit()
    return {"onboarding_step": step}


@router.post("/documents")
async def submit_documents(
    incorporation: UploadFile = File(...),
    gst_or_pan: UploadFile = File(...),
    user: OrganizationUser = Depends(_onboarding_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    org = await _org(db, user)
    tr = await _tenant(db, user)
    documents = [
        {
            "kind": "incorporation",
            "filename": incorporation.filename,
            "content": await incorporation.read(),
            "content_type": incorporation.content_type,
        },
        {
            "kind": "gst_or_pan",
            "filename": gst_or_pan.filename,
            "content": await gst_or_pan.read(),
            "content_type": gst_or_pan.content_type,
        },
    ]
    result = await PlivoComplianceService.submit_compliance_and_allot_number(
        tr,
        db,
        legal_name=org.legal_name or org.name,
        alias_name=org.alias_name,
        business_pan=org.business_pan,
        cin=org.cin,
        documents=documents,
    )
    step = _advance(org, after="documents")
    db.add(org)
    await db.commit()
    return {
        "onboarding_step": step,
        "number": result.get("number"),
        "number_status": result.get("number_status"),
    }


@router.post("/working-hours")
async def save_working_hours(
    payload: WorkingHoursRequest,
    user: OrganizationUser = Depends(_onboarding_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    org = await _org(db, user)
    defaults = (
        await db.execute(
            select(OrganizationAssignmentDefaults).where(
                OrganizationAssignmentDefaults.organization_id == org.id
            )
        )
    ).scalars().first()
    if defaults is None:
        defaults = OrganizationAssignmentDefaults(id=uuid.uuid4(), organization_id=org.id)
    defaults.working_days = payload.working_days
    defaults.start_time = _parse_time(payload.start_time)
    defaults.end_time = _parse_time(payload.end_time)
    defaults.timezone = (payload.timezone or "Asia/Kolkata")
    db.add(defaults)
    step = _advance(org, after="working_hours")
    db.add(org)
    await db.commit()
    return {"onboarding_step": step}


@router.post("/projects/done")
async def projects_done(
    user: OrganizationUser = Depends(_onboarding_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    org = await _org(db, user)
    count = (
        await db.execute(
            select(func.count(RealEstateProject.id)).where(RealEstateProject.organization_id == org.id)
        )
    ).scalar() or 0
    if not count:
        raise HTTPException(status_code=400, detail="Add at least one project before continuing.")
    step = _advance(org, after="projects")
    db.add(org)
    await db.commit()
    return {"onboarding_step": step}


@router.post("/agent")
async def name_agent(
    payload: AgentRequest,
    user: OrganizationUser = Depends(_onboarding_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    name = (payload.name or "").strip() or "Property Assistant"
    org = await _org(db, user)
    tr = await _tenant(db, user)

    # Build the agent on the curated real-estate template (no prompt/tools step in
    # the simplified flow — the agent runs on the per-vertical curated prompt).
    from app.services.dynamic_tool_resolver import default_tool_keys
    from app.services.nokvo_one_business_templates import (
        business_template_prompt,
        custom_tabs_from_overrides,
    )

    provider_status = dict(tr.provider_status or {})
    overrides = dict(provider_status.get("business_template_schema_overrides") or {})
    custom_tabs = custom_tabs_from_overrides(provider_status)
    try:
        tool_keys = default_tool_keys(org.industry, overrides, custom_tabs)
    except Exception:
        tool_keys = []
    try:
        system_prompt = business_template_prompt(org.industry) or ""
    except Exception:
        system_prompt = ""

    existing = (
        await db.execute(select(NokvoOneAgent).where(NokvoOneAgent.organization_id == org.id).limit(1))
    ).scalars().first()
    if existing is not None:
        existing.name = name  # re-run of the step just renames
        db.add(existing)
    else:
        db.add(
            NokvoOneAgent(
                id=uuid.uuid4(),
                organization_id=org.id,
                name=name,
                description="Configured during onboarding.",
                system_prompt=system_prompt,
                tool_keys=tool_keys,
                created_by_user_id=user.id,
            )
        )
    step = _advance(org, after="agent")
    db.add(org)
    await db.commit()
    return {"onboarding_step": step, "agent_name": name}


@router.post("/terms")
async def accept_terms(
    payload: TermsRequest,
    user: OrganizationUser = Depends(_onboarding_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    if not (payload.terms_accepted and payload.privacy_accepted):
        raise HTTPException(
            status_code=400, detail="You must accept both the Terms of Service and the Privacy Policy."
        )
    org = await _org(db, user)
    now = datetime.now(timezone.utc)
    org.terms_accepted_at = now
    org.privacy_accepted_at = now
    org.terms_version = TERMS_VERSION
    org.status = "active"
    org.onboarding_step = "done"
    db.add(org)
    await db.commit()
    return {"status": "active", "onboarding_step": "done"}
