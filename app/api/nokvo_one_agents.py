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
from app.schemas.nokvo_one import (
    NokvoOneAgentChatRequest,
    NokvoOneAgentChatResponse,
    NokvoOneAgentCreate,
    NokvoOneAgentResponse,
    NokvoOneAgentUpdate,
    NokvoOnePredefinedToolResponse,
)
from app.services.nokvo_one_agent_runtime import NokvoOneAgentRuntime, NokvoOneAgentRuntimeError
from app.services.predefined_tools_service import list_tools, validate_tool_keys


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


@router.get("/tools/predefined", response_model=list[NokvoOnePredefinedToolResponse])
async def list_predefined_tools(_: OrganizationUser = Depends(_agent_dep())):
    return [NokvoOnePredefinedToolResponse(**tool) for tool in list_tools()]


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


@router.post("/", response_model=NokvoOneAgentResponse, status_code=status.HTTP_201_CREATED)
async def create_agent(
    payload: NokvoOneAgentCreate,
    user: OrganizationUser = Depends(_admin_agent_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    try:
        keys = validate_tool_keys(payload.tool_keys or [])
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

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
        try:
            agent.tool_keys = validate_tool_keys(payload.tool_keys)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

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

    try:
        result = await NokvoOneAgentRuntime.chat_turn(
            db,
            organization_id=user.organization_id,
            user_id=user.id,
            agent_system_prompt=agent.system_prompt,
            tool_keys=list(agent.tool_keys or []),
            user_message=payload.message,
        )
    except NokvoOneAgentRuntimeError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
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
