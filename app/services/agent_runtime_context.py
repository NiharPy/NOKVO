"""Unified runtime context for the Nokvo One agent.

Every surface that talks to a caller — inbound voice, outbound voice
campaigns, chat — needs the same five things to do its job:

1. The :class:`Organization` that owns the conversation (industry,
   business-template overrides, custom tabs).
2. Tenant infrastructure (:class:`TenantResources`) for STT/TTS/Redis.
3. Campaign context if this is an outbound call (campaign id, goal,
   contact details, opening message).
4. Caller identity (phone, optional name) for cross-call memory and
   identity verification.
5. Surface metadata (``voice_inbound`` / ``voice_outbound`` / ``chat``)
   so logging and analytics know which entry point handled the turn.

Previously the voice pipeline built its own ``business_context`` tuple
and the outbound bridges built their own ``campaign_context`` dict, while
chat (text_query) effectively used neither. That divergence meant a
behaviour change in voice didn't automatically apply to chat.

:class:`AgentRuntimeContext` is the one-shape representation, with a
single :func:`build` factory that all three surfaces call. The pipeline's
existing ``_voice_business_context`` is now a thin wrapper around it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.services.nokvo_one_business_templates import custom_tabs_from_overrides


_VALID_SURFACES = {"voice_inbound", "voice_outbound", "chat"}


@dataclass
class AgentRuntimeContext:
    """The single context object the agent runtime reads from every turn,
    on every surface.

    Fields are intentionally flat so a chat client can construct one with
    just an organization + surface, while voice can pass the full set
    (tenant resources, campaign, caller phone) without growing a wrapper
    around it.
    """

    organization: Organization
    tenant_res: TenantResources | None = None
    surface: str = "voice_inbound"

    # Business template state — drives FSM / tool flow choices.
    overrides: dict[str, Any] = field(default_factory=dict)
    custom_tabs: list[dict[str, Any]] = field(default_factory=list)

    # Outbound-campaign context (None for inbound voice / chat).
    campaign_id: str | None = None
    campaign_goal: str | None = None
    campaign_opening_message: str | None = None

    # Caller identity (set by caller-ID, outbound contact, or chat user
    # session). Used for cross-call memory + identity verification.
    caller_phone: str | None = None
    caller_name: str | None = None

    # The call_id / chat session id, used for AgentSessionStore keys.
    session_id: str | None = None

    def as_legacy_business_context_tuple(self) -> tuple[Organization, dict[str, Any], list[dict[str, Any]]]:
        """Compatibility shim: the existing pipeline branches expect a
        ``(organization, overrides, custom_tabs)`` tuple. Returns that
        view so they keep working unchanged while we migrate."""
        return self.organization, self.overrides, self.custom_tabs

    def as_legacy_campaign_context(self) -> dict[str, Any]:
        """Compatibility shim mirroring the old ``campaign_context`` dict
        the voice WebSocket entry points pass around."""
        ctx: dict[str, Any] = {}
        if self.campaign_id:
            ctx["campaign_id"] = self.campaign_id
        if self.campaign_goal:
            ctx["goal"] = self.campaign_goal
        if self.campaign_opening_message:
            ctx["opening_message"] = self.campaign_opening_message
        if self.caller_phone or self.caller_name:
            contact: dict[str, Any] = {}
            if self.caller_phone:
                contact["phone"] = self.caller_phone
            if self.caller_name:
                contact["name"] = self.caller_name
            ctx["contact"] = contact
        return ctx


async def build(
    db: AsyncSession | None,
    tenant_res: TenantResources,
    *,
    surface: str = "voice_inbound",
    campaign_id: str | None = None,
    campaign_goal: str | None = None,
    campaign_opening_message: str | None = None,
    caller_phone: str | None = None,
    caller_name: str | None = None,
    session_id: str | None = None,
) -> AgentRuntimeContext | None:
    """Load the organization + template state for ``tenant_res`` and wrap
    it (plus any caller / campaign metadata) into a single
    :class:`AgentRuntimeContext`. Returns ``None`` when the organization
    can't be loaded — every surface should fall back to a degraded mode
    in that case rather than continue with a None context.
    """
    if surface not in _VALID_SURFACES:
        raise ValueError(f"Unknown surface: {surface}")
    if db is None:
        return None
    try:
        res = await db.execute(select(Organization).where(Organization.id == tenant_res.organization_id))
    except Exception:
        return None
    organization = res.scalars().first()
    if organization is None:
        return None
    provider_status = dict(tenant_res.provider_status or {})
    overrides = dict(provider_status.get("business_template_schema_overrides") or {})
    return AgentRuntimeContext(
        organization=organization,
        tenant_res=tenant_res,
        surface=surface,
        overrides=overrides,
        custom_tabs=custom_tabs_from_overrides(provider_status),
        campaign_id=campaign_id,
        campaign_goal=campaign_goal,
        campaign_opening_message=campaign_opening_message,
        caller_phone=caller_phone,
        caller_name=caller_name,
        session_id=session_id,
    )


def from_legacy_inputs(
    organization: Organization,
    tenant_res: TenantResources,
    *,
    overrides: dict[str, Any] | None,
    custom_tabs: list[dict[str, Any]] | None,
    surface: str = "voice_inbound",
    campaign_context: dict[str, Any] | None = None,
    session_id: str | None = None,
) -> AgentRuntimeContext:
    """Adapter for the old pipeline call sites that already have the
    business_context tuple and campaign_context dict in scope. Lets us
    flip them over to :class:`AgentRuntimeContext` incrementally."""
    contact = (campaign_context or {}).get("contact") if isinstance((campaign_context or {}).get("contact"), dict) else {}
    return AgentRuntimeContext(
        organization=organization,
        tenant_res=tenant_res,
        surface=surface,
        overrides=overrides or {},
        custom_tabs=list(custom_tabs or []),
        campaign_id=(campaign_context or {}).get("campaign_id"),
        campaign_goal=(campaign_context or {}).get("goal"),
        campaign_opening_message=(campaign_context or {}).get("opening_message"),
        caller_phone=(contact or {}).get("phone") or (campaign_context or {}).get("from_phone"),
        caller_name=(contact or {}).get("name"),
        session_id=session_id,
    )
