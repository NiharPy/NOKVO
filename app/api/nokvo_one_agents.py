from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.rate_limit import limiter
from app.models.nokvo_one_agent import NokvoOneAgent
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.schemas.nokvo_one import (
    NokvoOneAgentChatRequest,
    NokvoOneAgentChatResponse,
    NokvoOneAgentCreate,
    NokvoOneAgentFromOutcomesRequest,
    NokvoOneAgentResponse,
    NokvoOneAgentUpdate,
    NokvoOneOutcomeOption,
    NokvoOneOutcomesResponse,
    NokvoOnePredefinedToolResponse,
    NokvoOneToolCatalogResponse,
    NokvoOneToolGroupResponse,
)
from app.services.dynamic_tool_resolver import (
    catalog_as_groups,
    default_tool_keys,
    resolve_index,
    validate_resolved_tool_keys,
)
from app.services.nokvo_one_agent_runtime import NokvoOneAgentRuntime, NokvoOneAgentRuntimeError
from app.services.nokvo_one_business_templates import (
    custom_tab_record_type,
    custom_tabs_from_overrides,
    enabled_tabs_for,
    tab_record_type,
)
from app.services.nokvo_one_outcome_starter import (
    default_agent_name,
    materialize_starter_pack,
    outcomes_for,
)
from app.services.predefined_tools_service import resolve_legacy_key



def _safe_detail(exc: BaseException) -> str:
    """Return a user-safe error detail (forward RuntimeError/ValueError text;
    swallow internal exception messages and log them)."""
    import logging
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)
    logging.getLogger(__name__).exception("unexpected exception in request handler", exc_info=exc)
    return "Operation failed"


router = APIRouter()


def _agent_dep(allow_pending_approval: bool = True):
    statuses = ["active"]
    if allow_pending_approval:
        statuses = ["pending_approval", "active"]
    return deps.RequireNokvoOneOrganization(allowed_statuses=statuses)


def _admin_agent_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["pending_approval", "active"],
        allowed_roles=["admin", "manager"],
    )


async def _load_business_context(
    db: AsyncSession, organization_id: uuid.UUID
) -> tuple[Organization, dict, list[dict]]:
    org_res = await db.execute(select(Organization).where(Organization.id == organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    tr_res = await db.execute(
        select(TenantResources).where(TenantResources.organization_id == organization_id)
    )
    tenant_res = tr_res.scalars().first()
    provider_status = dict((tenant_res.provider_status if tenant_res else {}) or {})
    overrides = dict(provider_status.get("business_template_schema_overrides") or {})
    custom_tabs = custom_tabs_from_overrides(provider_status)
    return organization, overrides, custom_tabs


@router.get("/tools/predefined", response_model=list[NokvoOnePredefinedToolResponse])
async def list_predefined_tools(
    user: OrganizationUser = Depends(_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Flat list of tools available to this org. Kept for backwards compatibility."""
    organization, overrides, custom_tabs = await _load_business_context(db, user.organization_id)
    groups = catalog_as_groups(organization.industry, overrides, custom_tabs)
    flat: list[NokvoOnePredefinedToolResponse] = []
    for group in groups:
        for tool in group["tools"]:
            flat.append(NokvoOnePredefinedToolResponse(**tool))
    return flat


@router.get("/tools/catalog", response_model=NokvoOneToolCatalogResponse)
async def get_tool_catalog(
    user: OrganizationUser = Depends(_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Grouped tool catalog for the agent-create UI."""
    organization, overrides, custom_tabs = await _load_business_context(db, user.organization_id)
    groups = catalog_as_groups(organization.industry, overrides, custom_tabs)
    group_responses = [
        NokvoOneToolGroupResponse(
            label=group["label"],
            tools=[NokvoOnePredefinedToolResponse(**tool) for tool in group["tools"]],
        )
        for group in groups
    ]
    return NokvoOneToolCatalogResponse(
        business_type=organization.industry,
        groups=group_responses,
        default_tool_keys=default_tool_keys(organization.industry, overrides, custom_tabs),
    )


@router.get("/outcomes", response_model=NokvoOneOutcomesResponse)
async def get_outcome_catalog(
    user: OrganizationUser = Depends(_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Plain-English outcome list for the onboarding wizard."""
    organization, _, _ = await _load_business_context(db, user.organization_id)
    return NokvoOneOutcomesResponse(
        business_type=organization.industry,
        outcomes=[NokvoOneOutcomeOption(**o) for o in outcomes_for(organization.industry)],
        default_agent_name=default_agent_name(organization.industry),
    )


@router.post(
    "/from-outcomes",
    response_model=NokvoOneAgentResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_agent_from_outcomes(
    payload: NokvoOneAgentFromOutcomesRequest,
    user: OrganizationUser = Depends(_admin_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Materialize a starter agent from selected outcomes.

    Used by the onboarding-v2 wizard. Resolves the per-org tool catalog,
    intersects with the outcome-suggested tool keys, and writes one
    NokvoOneAgent. Safe to call multiple times — each call creates a new agent.
    """
    organization, overrides, custom_tabs = await _load_business_context(db, user.organization_id)
    available = set(resolve_index(organization.industry, overrides, custom_tabs).keys())
    starter = materialize_starter_pack(
        organization.industry,
        list(payload.outcomes or []),
        available_tool_keys=available,
    )
    name = (payload.agent_name or starter["name"]).strip() or starter["name"]
    agent = NokvoOneAgent(
        id=uuid.uuid4(),
        organization_id=user.organization_id,
        name=name,
        description=starter["description"],
        system_prompt=starter["system_prompt"],
        tool_keys=starter["tool_keys"],
        created_by_user_id=user.id,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return NokvoOneAgentResponse.model_validate(agent)


@router.get("/", response_model=list[NokvoOneAgentResponse])
async def list_agents(
    user: OrganizationUser = Depends(_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    res = await db.execute(
        select(NokvoOneAgent)
        .where(NokvoOneAgent.organization_id == user.organization_id)
        .order_by(NokvoOneAgent.created_at.desc())
    )
    return [NokvoOneAgentResponse.model_validate(agent) for agent in res.scalars().all()]


def _normalize_tool_keys(
    keys: list[str],
    business_type: str | None,
    overrides: dict,
    custom_tabs: list[dict],
) -> list[str]:
    resolved = [resolve_legacy_key(k) for k in (keys or [])]
    try:
        return validate_resolved_tool_keys(resolved, business_type, overrides, custom_tabs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc


@router.post("/", response_model=NokvoOneAgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: NokvoOneAgentCreate,
    user: OrganizationUser = Depends(_admin_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    organization, overrides, custom_tabs = await _load_business_context(db, user.organization_id)
    keys = _normalize_tool_keys(payload.tool_keys or [], organization.industry, overrides, custom_tabs)

    agent = NokvoOneAgent(
        id=uuid.uuid4(),
        organization_id=user.organization_id,
        name=payload.name.strip(),
        description=payload.description,
        system_prompt=payload.system_prompt,
        tool_keys=keys,
        created_by_user_id=user.id,
    )
    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return NokvoOneAgentResponse.model_validate(agent)


@router.patch("/{agent_id}", response_model=NokvoOneAgentResponse)
async def update_agent(
    agent_id: uuid.UUID,
    payload: NokvoOneAgentUpdate,
    user: OrganizationUser = Depends(_admin_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    res = await db.execute(
        select(NokvoOneAgent).where(
            NokvoOneAgent.id == agent_id,
            NokvoOneAgent.organization_id == user.organization_id,
        )
    )
    agent = res.scalars().first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    if payload.name is not None:
        agent.name = payload.name.strip()
    if payload.description is not None:
        agent.description = payload.description
    if payload.system_prompt is not None:
        agent.system_prompt = payload.system_prompt
    if payload.tool_keys is not None:
        organization, overrides, custom_tabs = await _load_business_context(db, user.organization_id)
        agent.tool_keys = _normalize_tool_keys(
            payload.tool_keys, organization.industry, overrides, custom_tabs
        )

    db.add(agent)
    await db.commit()
    await db.refresh(agent)
    return NokvoOneAgentResponse.model_validate(agent)


@router.delete("/{agent_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_agent(
    agent_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    res = await db.execute(
        select(NokvoOneAgent).where(
            NokvoOneAgent.id == agent_id,
            NokvoOneAgent.organization_id == user.organization_id,
        )
    )
    agent = res.scalars().first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")
    await db.delete(agent)
    await db.commit()


@router.post("/{agent_id}/chat", response_model=NokvoOneAgentChatResponse)
@limiter.limit("60/minute")
async def chat_with_agent(
    request: Request,
    agent_id: uuid.UUID,
    payload: NokvoOneAgentChatRequest,
    user: OrganizationUser = Depends(_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    res = await db.execute(
        select(NokvoOneAgent).where(
            NokvoOneAgent.id == agent_id,
            NokvoOneAgent.organization_id == user.organization_id,
        )
    )
    agent = res.scalars().first()
    if agent is None:
        raise HTTPException(status_code=404, detail="Agent not found")

    organization, overrides, custom_tabs = await _load_business_context(db, user.organization_id)

    try:
        result = await NokvoOneAgentRuntime.chat_turn(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            agent_id=agent.id,
            agent_system_prompt=agent.system_prompt,
            business_type=organization.industry,
            schema_overrides=overrides,
            custom_tabs=custom_tabs,
            tool_keys=list(agent.tool_keys or []),
            user_message=payload.message,
        )
    except NokvoOneAgentRuntimeError as exc:
        raise HTTPException(status_code=503, detail=_safe_detail(exc)) from exc
    return NokvoOneAgentChatResponse(reply=result["reply"], tool_calls=result.get("tool_calls", []))


# ─────────── Calling gate ───────────


@router.post("/{agent_id}/phone-link")
async def link_phone_number(
    agent_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    organization = org_res.scalars().first()
    if organization is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if not organization.calling_enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "Phone-number linking and calling features are gated. Request superadmin approval "
                "to enable calling for this Nokvo One organization."
            ),
        )
    # Calling itself is not implemented in Nokvo One V1 even when the gate is open.
    raise HTTPException(
        status_code=501,
        detail="Calling integration is not yet implemented for Nokvo One.",
    )


# ─────────── Tool record views (so the portal can show drafts and queues) ───────────


@router.get("/records/email-drafts")
async def list_email_drafts(
    user: OrganizationUser = Depends(_admin_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    from app.models.nokvo_one_tool_record import NokvoOneToolRecord

    res = await db.execute(
        select(NokvoOneToolRecord)
        .where(
            NokvoOneToolRecord.organization_id == user.organization_id,
            NokvoOneToolRecord.record_type == "email_draft",
        )
        .order_by(NokvoOneToolRecord.created_at.desc())
        .limit(100)
    )
    return [
        {
            "id": str(r.id),
            "status": r.status,
            "data": r.data or {},
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in res.scalars().all()
    ]




@router.get("/records/tab/{tab_slug}")
async def list_tab_records(
    tab_slug: str,
    limit: int = 100,
    user: OrganizationUser = Depends(_admin_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    from app.models.nokvo_one_tool_record import NokvoOneToolRecord

    organization, _, custom_tabs = await _load_business_context(db, user.organization_id)
    available_tabs = set(enabled_tabs_for(organization.industry))
    available_tabs.update(str(tab.get("slug") or "") for tab in custom_tabs)
    if tab_slug not in available_tabs:
        raise HTTPException(status_code=404, detail="Tab is not enabled for this organization")

    record_type = tab_record_type(tab_slug) or custom_tab_record_type(tab_slug)
    max_limit = min(max(int(limit or 100), 1), 200)
    res = await db.execute(
        select(NokvoOneToolRecord)
        .where(
            NokvoOneToolRecord.organization_id == user.organization_id,
            NokvoOneToolRecord.record_type == record_type,
        )
        .order_by(NokvoOneToolRecord.created_at.desc())
        .limit(max_limit)
    )
    return [
        {
            "id": str(r.id),
            "record_type": r.record_type,
            "status": r.status,
            "data": r.data or {},
            "contact_phone": r.contact_phone,
            "contact_email": r.contact_email,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "updated_at": r.updated_at.isoformat() if r.updated_at else None,
        }
        for r in res.scalars().all()
    ]


@router.post("/records/email-drafts/{draft_id}/discard")
async def discard_email_draft(
    draft_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    from app.models.nokvo_one_tool_record import NokvoOneToolRecord

    res = await db.execute(
        select(NokvoOneToolRecord).where(
            NokvoOneToolRecord.id == draft_id,
            NokvoOneToolRecord.organization_id == user.organization_id,
            NokvoOneToolRecord.record_type == "email_draft",
        )
    )
    draft = res.scalars().first()
    if draft is None:
        raise HTTPException(status_code=404, detail="Email draft not found")
    draft.status = "discarded"
    db.add(draft)
    await db.commit()
    return {"ok": True, "id": str(draft.id), "status": draft.status}


# NOTE: Confirming an email draft would actually send the email. V1 explicitly does NOT
# wire an external send path — the confirmation flow is left to the operator (copy/paste,
# or future integration). The draft remains in 'pending_confirmation' until discarded or
# externally handled. This preserves the "no agent-initiated external sends" guarantee.
