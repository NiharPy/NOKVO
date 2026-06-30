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

import logging
import re
import uuid
from datetime import datetime, time, timezone

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.models.member_assignment import OrganizationAssignmentDefaults
from app.models.nokvo_one_agent import NokvoOneAgent
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.real_estate_project import RealEstateProject
from app.models.tenant_resources import TenantResources
from app.services.plivo_compliance_service import PlivoComplianceService
from app.services.tool_flow_questions import ensure_tool_flow_questions
from sqlalchemy.orm.attributes import flag_modified

router = APIRouter()
logger = logging.getLogger(__name__)

# Version stamped on the org when the user accepts the legal docs.
TERMS_VERSION = "2026-06-20"

_STEP_ORDER = ["business_details", "documents", "working_hours", "projects", "agent", "terms", "done"]
# NOKVO APEX onboarding is just the two KYC screens (business details + docs),
# then done — no working-hours/projects/agent/terms.
_APEX_STEP_ORDER = ["business_details", "documents", "done"]


def _step_order(org: Organization) -> list[str]:
    return _APEX_STEP_ORDER if (org.product_tier or "") == "nokvo_apex" else _STEP_ORDER


def _onboarding_admin_dep():
    # Accept both Nokvo One and NOKVO APEX orgs (onboarding is org-scoped).
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["onboarding"],
        allowed_roles=["admin"],
        allowed_product_tiers=["nokvo_one", "nokvo_apex"],
    )


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
    """Move onboarding_step to the step that follows ``after`` (never backwards).
    Uses the org's product step order — APEX jumps documents → done."""
    order = _step_order(org)
    if after not in order:
        # ``after`` belongs to the other product's flow (shouldn't happen) — no-op.
        return org.onboarding_step or order[0]
    nxt = order[min(order.index(after) + 1, len(order) - 1)]
    cur = org.onboarding_step or order[0]
    if cur not in order or order.index(nxt) > order.index(cur):
        org.onboarding_step = nxt
    return org.onboarding_step


async def _ensure_apex_bulk_request(
    db: AsyncSession,
    org: Organization,
    tr: TenantResources,
    user: OrganizationUser,
) -> None:
    """Auto-create a Bulk-Calling request for an APEX org at onboarding activation.

    APEX is the deterministic-outbound product, so its bulk DID pool is filled by
    the SuperAdmin one-click *auto-provision* action — which hangs off a
    ``BulkCallingRequest`` row. APEX never hits the (nokvo_one-only)
    ``/bulk-calling/request`` endpoint, so without this seed the console would never
    surface the "Auto-provision 5 numbers" button for an APEX tenant. By now the
    sub-account (provisioning Step 4) and the compliance application (the documents
    step just filed it) both exist, so the request is immediately actionable.

    Idempotent — no-op if a request already exists for the org. Best-effort: a
    failure here is logged and swallowed so it can never block APEX activation."""
    if (org.product_tier or "") != "nokvo_apex":
        return
    from app.models.bulk_calling_request import BulkCallingRequest

    try:
        existing = (
            await db.execute(
                select(BulkCallingRequest.id)
                .where(BulkCallingRequest.organization_id == org.id)
                .limit(1)
            )
        ).first()
    except Exception:
        logger.warning("APEX-BULK: could not check for an existing request", exc_info=True)
        return
    if existing is not None:
        return
    db.add(
        BulkCallingRequest(
            organization_id=org.id,
            tenant_id=getattr(tr, "tenant_id", None),
            requested_by_user_id=getattr(user, "id", None),
            # No human-entered contact for an auto-created request — the admin's
            # email is the most useful way for ops to reach them. contact_number is
            # a free String column (not a validated phone), so this is fine.
            contact_number=(getattr(user, "email", None) or "APEX auto-provision")[:255],
            note="Auto-created at APEX onboarding — Auto-provision assigns 5 bulk DIDs.",
            status="pending",
        )
    )


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


def _norm_code(value: str | None) -> str | None:
    """Normalize a PAN/CIN for comparison: strip all whitespace, uppercase."""
    if not value:
        return None
    cleaned = re.sub(r"\s+", "", value).upper()
    return cleaned or None


def _norm_name(value: str | None) -> str | None:
    """Normalize a company name: trim, collapse inner whitespace, lowercase."""
    if not value:
        return None
    cleaned = re.sub(r"\s+", " ", value).strip().lower()
    return cleaned or None


async def _duplicate_company_conflict(
    db: AsyncSession,
    *,
    exclude_id,
    legal_name: str | None,
    business_pan: str | None,
    cin: str | None,
    product_tier: str | None = None,
) -> str | None:
    """Return the field that collides with another org IN THE SAME PRODUCT, or None.

    A company is considered a duplicate if its CIN, business PAN, or company
    name matches an existing organization (case-/whitespace-insensitive). CIN
    and PAN are government identifiers (truly unique per company); the name is
    a softer check but the user wants all three guarded.

    Scoped to ``product_tier`` (when given) so the products are independent: a
    company registered on Nokvo One can still register on NOKVO APEX (and vice-
    versa) — the duplicate check only looks within the same product.
    """
    name_n = _norm_name(legal_name)
    pan_n = _norm_code(business_pan)
    cin_n = _norm_code(cin)

    conds = []
    if name_n:
        conds.append(func.lower(func.btrim(Organization.legal_name)) == name_n)
        conds.append(func.lower(func.btrim(Organization.name)) == name_n)
    if pan_n:
        conds.append(Organization.business_pan.isnot(None))
    if cin_n:
        conds.append(Organization.cin.isnot(None))
    if not conds:
        return None

    where = [Organization.id != exclude_id, or_(*conds)]
    if product_tier:
        # Only collide with orgs of the SAME product — Nokvo One and APEX are
        # independent registries.
        where.append(Organization.product_tier == product_tier)

    rows = (
        await db.execute(
            select(Organization.legal_name, Organization.name, Organization.business_pan, Organization.cin)
            .where(*where)
        )
    ).all()

    for o_legal, o_name, o_pan, o_cin in rows:
        # PAN/CIN compared with full normalization (the SQL only narrowed rows).
        if cin_n and _norm_code(o_cin) == cin_n:
            return "CIN"
        if pan_n and _norm_code(o_pan) == pan_n:
            return "business PAN"
        if name_n and (_norm_name(o_legal) == name_n or _norm_name(o_name) == name_n):
            return "company name"
    return None


@router.post("/business-details")
async def save_business_details(
    payload: BusinessDetailsRequest,
    user: OrganizationUser = Depends(_onboarding_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    if not payload.legal_name.strip():
        raise HTTPException(status_code=400, detail="Company legal name is required.")
    org = await _org(db, user)

    # Safeguard: block onboarding a company that already exists IN THIS PRODUCT.
    # Nokvo One and NOKVO APEX are independent registries — a company on one can
    # still register on the other, so the check is scoped to the org's product.
    conflict = await _duplicate_company_conflict(
        db,
        exclude_id=org.id,
        legal_name=payload.legal_name,
        business_pan=payload.business_pan,
        cin=payload.cin,
        product_tier=org.product_tier,
    )
    if conflict:
        _product_label = "NOKVO APEX" if (org.product_tier or "") == "nokvo_apex" else "Nokvo One"
        raise HTTPException(
            status_code=409,
            detail=f"A company with this {conflict} is already registered on {_product_label}.",
        )

    org.legal_name = payload.legal_name.strip()
    org.alias_name = (payload.alias_name or "").strip() or None
    org.business_pan = (payload.business_pan or "").strip() or None
    org.cin = (payload.cin or "").strip() or None
    # Real estate is the only vertical — assign it here (the first onboarding
    # step) so the later, real-estate-gated steps (projects / brochure upload)
    # work. Also seed the vertical's tool-flow questions so booking/lead flows are
    # ready at runtime (mirrors the /business-template step). NOT for APEX — it's
    # deterministic bulk outbound, no real-estate flows in its onboarding.
    if (org.product_tier or "") != "nokvo_apex" and not (org.industry or "").strip():
        org.industry = "real_estate"
        tr = await _tenant(db, user)
        new_status, changed = ensure_tool_flow_questions(tr.provider_status, org.industry)
        if changed:
            tr.provider_status = new_status
            flag_modified(tr, "provider_status")
            db.add(tr)
    step = _advance(org, after="business_details")
    db.add(org)
    await db.commit()
    return {"onboarding_step": step}


# KYC document upload limits — guard against an unbounded read (memory-exhaustion
# DoS) and junk being shipped to Plivo compliance / blob.
_MAX_DOC_BYTES = 10 * 1024 * 1024  # 10 MB
_ALLOWED_DOC_TYPES = ("application/pdf", "image/jpeg", "image/png")
_ALLOWED_DOC_EXTS = (".pdf", ".jpg", ".jpeg", ".png")


async def _read_validated_upload(file: UploadFile, *, label: str) -> bytes:
    """Read a KYC upload with a content-type/extension allowlist + a hard size cap
    (reads one byte past the limit and rejects, so a huge file is never slurped
    whole into memory). 400 on a bad type, oversize, or empty file."""
    ctype = (file.content_type or "").split(";")[0].strip().lower()
    name = (file.filename or "").lower()
    if ctype not in _ALLOWED_DOC_TYPES and not name.endswith(_ALLOWED_DOC_EXTS):
        raise HTTPException(status_code=400, detail=f"{label} must be a PDF, JPG, or PNG file.")
    data = await file.read(_MAX_DOC_BYTES + 1)
    if len(data) > _MAX_DOC_BYTES:
        raise HTTPException(status_code=400, detail=f"{label} is too large (max 10 MB).")
    if not data:
        raise HTTPException(status_code=400, detail=f"{label} is empty — please re-upload.")
    return data


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
            "content": await _read_validated_upload(incorporation, label="Certificate of incorporation"),
            "content_type": incorporation.content_type,
        },
        {
            "kind": "gst_or_pan",
            "filename": gst_or_pan.filename,
            "content": await _read_validated_upload(gst_or_pan, label="GST/PAN document"),
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
    # APEX onboarding ends here (business_details → documents → done): activate the
    # org. Nokvo One continues to working-hours/projects/agent/terms (terms activates).
    if step == "done":
        org.status = "active"
        # APEX is now active with a sub-account + filed compliance — seed the
        # Bulk-Calling request so the SuperAdmin console shows the one-click DID
        # auto-provision (APEX can't create this request itself). Best-effort.
        await _ensure_apex_bulk_request(db, org, tr, user)
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
