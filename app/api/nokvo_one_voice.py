"""Nokvo One voice pipeline router.

Mounted at /api/nokvo-one/agents. Exposes the tenant-isolated voice/RAG
endpoints described in the architecture spec:

  - GET  /runtime/status                          — pipeline diagnostics
  - WS   /voice/ws                                 — browser mic tester (admin/manager JWT)
  - GET  /phone-link                               — tenant's Exotel link config
  - POST /phone-link                               — admin sets/clears the link_id
  - POST /exotel/voice/{link_id}                   — Exotel inbound webhook
  - WS   /exotel/media/{link_id}                   — Exotel inbound media stream
  - POST /exotel/outbound-status/{call_link_id}    — outbound call status
  - WS   /exotel/outbound-media/{call_link_id}     — outbound media stream
  - GET  /campaigns                                — list outbound campaigns
  - POST /campaigns                                — create + auto-ingest script (admin)
  - GET  /campaigns/{id}                           — single campaign
  - POST /campaigns/{id}/launch                    — kick off the calls (admin)
  - POST /campaigns/{id}/cancel                    — cancel (admin)

Tenant isolation is enforced via TenantResources lookups for every path:
  - Authenticated routes: lookup by user.organization_id
  - Exotel webhooks: lookup by provider_status.agent_phone_link.link_id
  - Outbound: lookup by campaign.tenant_id (campaign already scoped to a tenant)
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import uuid
from typing import Any

logger = logging.getLogger(__name__)

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    WebSocket,
    status,
)
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.api import deps
from app.core import security
from app.core.config import settings
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.outgoing_lead import (
    LeadCaptureForm,
    LeadSourceConnection,
    LeadSourceProvider,
    OutgoingLead,
)
from app.models.outbound_campaign import OutboundCampaign
from app.models.tenant_resources import TenantResources
from app.services.exotel_bridge_service import ExotelBridgeService, ExotelWebSocketAdapter
from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
from app.services.outgoing_lead_service import (
    OutgoingLeadService,
    OutgoingLeadServiceError,
    decode_oauth_state,
    lead_is_callable,
)
from app.services.outbound_campaign_service import OutboundCampaignService


def _safe_detail(exc: BaseException) -> str:
    """Return a user-safe error detail (forward RuntimeError/ValueError text;
    swallow internal exception messages and log them)."""
    import logging
    if isinstance(exc, (RuntimeError, ValueError)):
        return str(exc)
    logging.getLogger(__name__).exception("unexpected exception in request handler", exc_info=exc)
    return "Operation failed"


router = APIRouter()

_ALLOWED_STATUSES = ["pending_approval", "active", "suspended"]


def _viewer_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=_ALLOWED_STATUSES,
        allowed_roles=["admin", "manager"],
    )


def _admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=_ALLOWED_STATUSES,
        allowed_roles=["admin"],
    )


async def _tenant_for_user(db: AsyncSession, user: OrganizationUser) -> TenantResources:
    res = await db.execute(
        select(TenantResources).where(TenantResources.organization_id == user.organization_id)
    )
    tr = res.scalars().first()
    if not tr:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")
    return tr


async def _tenant_by_link_id(db: AsyncSession, link_id: str) -> TenantResources | None:
    # Push the filter into Postgres so we don't pull every tenant row into
    # Python on each Exotel webhook. Still requires a JSONB GIN index for sub-
    # linear lookup, but at minimum keeps the row scan in the DB.
    res = await db.execute(
        select(TenantResources).where(
            TenantResources.provider_status["agent_phone_link"]["link_id"].astext == link_id,
            TenantResources.provider_status["agent_phone_link"]["status"].astext == "linked",
        )
    )
    return res.scalars().first()


async def _tenant_by_tenant_id(db: AsyncSession, tenant_id: str) -> TenantResources | None:
    res = await db.execute(
        select(TenantResources).where(TenantResources.tenant_id == tenant_id)
    )
    return res.scalars().first()


async def _connection_for_user(
    db: AsyncSession,
    tenant_res: TenantResources,
    connection_id: uuid.UUID,
) -> LeadSourceConnection:
    res = await db.execute(
        select(LeadSourceConnection).where(
            LeadSourceConnection.id == connection_id,
            LeadSourceConnection.tenant_id == tenant_res.tenant_id,
        )
    )
    connection = res.scalars().first()
    if connection is None:
        raise HTTPException(status_code=404, detail="Lead source connection not found")
    return connection


async def _ws_user(websocket: WebSocket, db: AsyncSession) -> OrganizationUser | None:
    """Decode an organization_user JWT carried in ?token= or Authorization header.
    Returns the user only when it belongs to an active Nokvo One organization,
    the session has not been revoked, and (when TOTP is enrolled) the token is
    MFA-elevated."""
    from app.models.organization_session import OrganizationSession

    token = websocket.query_params.get("token") or ""
    auth = websocket.headers.get("authorization") or ""
    if not token and auth.lower().startswith("bearer "):
        token = auth.split(" ", 1)[1].strip()
    if not token:
        return None
    try:
        payload = security.decode_access_token(token, expected_tiers=[security.JWT_TIER_ORGANIZATION])
    except Exception:
        return None
    if payload.get("principal_type") != "organization_user":
        return None
    user_id = payload.get("sub")
    if not user_id:
        return None
    try:
        uid = uuid.UUID(user_id)
    except ValueError:
        return None

    session_id = payload.get("sid")
    if session_id:
        try:
            sess_res = await db.execute(
                select(OrganizationSession).where(OrganizationSession.id == session_id)
            )
            session = sess_res.scalars().first()
        except Exception:
            return None
        if not session or session.revoked_at is not None:
            return None

    user_res = await db.execute(select(OrganizationUser).where(OrganizationUser.id == uid))
    user = user_res.scalars().first()
    if user is None or user.status == "disabled":
        return None

    has_totp = bool(
        getattr(user, "totp_secret_encrypted", None)
        or getattr(user, "totp_secret_encrypted_v2", None)
    )
    if user.mfa_required and has_totp and not bool(payload.get("mfa_completed", False)):
        return None

    org_res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    org = org_res.scalars().first()
    if org is None or (org.product_tier or "nokvo_prime") != "nokvo_one":
        return None
    if org.status not in _ALLOWED_STATUSES:
        return None
    return user


# ────────────────────────── Runtime status ──────────────────────────


@router.get("/runtime/status")
async def get_runtime_status(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    return NokvoOneVoicePipeline.runtime_status(tr)


@router.get("/runtime/health")
async def get_runtime_health(
    window_hours: int = 24,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Operator triage view: retry queue depth, recent outcome
    distribution + failure rate, and KB source freshness. ``window_hours``
    bounds the outcome window (default 24h)."""
    from app.services.agent_runtime_health import build_health_report

    tr = await _tenant_for_user(db, user)
    window = max(1, min(int(window_hours), 24 * 30))
    return await build_health_report(db, tr, window_hours=window)


# ────────────────────────── Browser voice tester ──────────────────────────


@router.websocket("/voice/ws")
async def voice_tester_websocket(websocket: WebSocket):
    async for db in deps.get_db():
        user = await _ws_user(websocket, db)
        if user is None or user.role not in {"admin", "manager"}:
            await websocket.close(code=1008)
            return
        tr = await _tenant_for_user(db, user)
        await NokvoOneVoiceStreamService.run_session(websocket, tr, db=db)
        return


# ────────────────────────── Outbound tester session stash ──────────────────────────
#
# Doc text + system prompt are too big for query params and we don't want to
# stuff them into the first WS frame (would require accept()-then-recv before
# delegating to run_session, complicating cleanup). Instead the frontend
# uploads them to a small REST endpoint, the server extracts text and parks
# the pair in memory keyed by a one-shot token. The WS endpoint reads it
# back, then evicts. Tokens auto-expire after 5 minutes regardless.
#
# In-memory is fine for single-uvicorn; promoting to Redis is a one-swap when
# we scale, matching the rest of the platform.

import time as _time

_TESTER_STASH_TTL_SECONDS = 300
_TESTER_STASH_MAX = 256
_tester_stash: dict[str, dict[str, Any]] = {}
_tester_stash_lock = asyncio.Lock()


async def _tester_stash_put(payload: dict[str, Any]) -> str:
    token = uuid.uuid4().hex
    expires_at = _time.monotonic() + _TESTER_STASH_TTL_SECONDS
    async with _tester_stash_lock:
        # Drop expired entries opportunistically — cheap O(n) sweep.
        now = _time.monotonic()
        for stale in [k for k, v in _tester_stash.items() if v.get("_expires_at", 0) <= now]:
            _tester_stash.pop(stale, None)
        while len(_tester_stash) >= _TESTER_STASH_MAX:
            # Drop oldest insertion.
            _tester_stash.pop(next(iter(_tester_stash)), None)
        _tester_stash[token] = {**payload, "_expires_at": expires_at}
    return token


async def _tester_stash_pop(token: str | None) -> dict[str, Any] | None:
    if not token:
        return None
    async with _tester_stash_lock:
        record = _tester_stash.pop(token, None)
    if record is None:
        return None
    if record.get("_expires_at", 0) <= _time.monotonic():
        return None
    return {k: v for k, v in record.items() if not k.startswith("_")}


@router.post("/lead-sources/outbound-tester/prepare")
async def prepare_outbound_tester_session(
    agent_prompt: str | None = Form(default=None),
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
):
    """Stash the optional agent_prompt for the live outbound tester WS.

    The agent_prompt is the agent's only "knowledge" — there is no
    reference-document path. Returns ``{tester_session_token, has_prompt}``;
    token is single-use, expires in 5 minutes, and the WS endpoint
    consumes it on open. Sending no prompt is valid for tester sessions
    that target a saved campaign (the campaign carries its own prompt).
    """
    prompt = (agent_prompt or "").strip()
    token = await _tester_stash_put({"agent_prompt": prompt, "doc_text": ""})
    return {
        "tester_session_token": token,
        "expires_in_seconds": _TESTER_STASH_TTL_SECONDS,
        "has_prompt": bool(prompt),
    }


@router.websocket("/voice/outbound-tester/ws")
async def outbound_voice_tester_websocket(websocket: WebSocket):
    """Live outbound-agent tester.

    Mirrors :func:`voice_tester_websocket` for auth/tenant lookup, but
    constructs a synthetic ``OutboundCampaignContext`` from query params
    so admins can rehearse the outbound flow without persisting a draft
    campaign first. Inputs (all optional, sensible defaults applied):

      * ``goal`` — the call objective in plain language ("Confirm Friday's
        site visit").
      * ``agent_prompt`` — the system-prompt override the campaign would
        normally store.
      * ``objectives`` — newline- or pipe-separated checklist the agent
        should land before exiting.
      * ``exit_conditions`` — newline- or pipe-separated reasons to wrap
        up the call.
      * ``tone`` — short style hint.

    Auth model is identical to the inbound tester: admin/manager role on
    a Nokvo One organization, MFA already handled by the JWT.
    """
    from app.services.agent_outbound_context import (
        OutboundCampaignContext,
        build_agent_config,
        load_outbound_context,
    )

    async for db in deps.get_db():
        user = await _ws_user(websocket, db)
        if user is None or user.role not in {"admin", "manager"}:
            await websocket.close(code=1008)
            return
        tr = await _tenant_for_user(db, user)

        params = websocket.query_params

        def _split_list(raw: str | None) -> list[str]:
            if not raw:
                return []
            return [item.strip() for item in raw.replace("|", "\n").split("\n") if item.strip()]

        # New path — tester picks a real campaign and rehearses with its actual
        # config (agent_prompt, objectives, exit_conditions, doc_text, persona
        # fields). The query-param + preset stash path stays as a fallback for
        # backwards-compat callers that still send the old fields.
        outbound_context: OutboundCampaignContext | None = None
        campaign_id_param = (params.get("campaign_id") or "").strip()
        sample_phone: str | None = None
        sample_name: str | None = None
        if campaign_id_param:
            try:
                campaign_uuid = uuid.UUID(campaign_id_param)
            except ValueError:
                await websocket.close(code=1008)
                return
            campaign = await OutboundCampaignService.get_campaign(campaign_uuid, tr, db)
            if campaign is None:
                await websocket.close(code=1008)
                return
            # Pull a sample contact (first valid one) so the safety net has a
            # phone / name to file a ticket or lead against. Without this, a
            # tester call would compose the booking but persist nothing.
            for contact in (campaign.contacts or []):
                if isinstance(contact, dict):
                    cand = str(contact.get("phone") or contact.get("phone_e164") or "").strip()
                    if cand:
                        sample_phone = cand
                        sample_name = (contact.get("name") or contact.get("full_name") or "").strip() or None
                        break
            try:
                outbound_context = await load_outbound_context(
                    db, str(campaign.id), goal=campaign.name,
                )
            except Exception as exc:
                logger.warning("NOKVO-OUTBOUND-TESTER: load_outbound_context failed: %r", exc)
                outbound_context = None

        if outbound_context is None:
            # Pull doc text / system-prompt override from the one-shot stash if
            # the frontend called /prepare first. The stash pop validates the
            # token and clears it so it can't be reused.
            stashed = await _tester_stash_pop(params.get("tester_session_token")) or {}
            prompt_override = stashed.get("agent_prompt") or params.get("agent_prompt")

            agent_config = build_agent_config(
                agent_prompt=prompt_override,
                objectives=_split_list(params.get("objectives")),
                exit_conditions=_split_list(params.get("exit_conditions")),
                tone=params.get("tone"),
                silence_timeout_seconds=params.get("silence_timeout_seconds"),
                caller_name=params.get("caller_name"),
                company_name=params.get("company_name"),
                pitch_summary=params.get("pitch_summary"),
                objective=params.get("objective"),
                language=params.get("language"),
            )
            synthetic_id = f"tester-{uuid.uuid4()}"
            outbound_context = OutboundCampaignContext(
                campaign_id=synthetic_id,
                name=params.get("name") or "Outbound tester",
                goal=str(params.get("goal") or "").strip()[:1000],
                agent_prompt=str(agent_config.get("agent_prompt") or "").strip()[:8000],
                objectives=list(agent_config.get("objectives") or []),
                exit_conditions=list(agent_config.get("exit_conditions") or []),
                tone=(str(agent_config.get("tone")).strip() or None) if agent_config.get("tone") else None,
                doc_text=(stashed.get("doc_text") or None) if stashed else None,
                silence_timeout_seconds=float(agent_config.get("silence_timeout_seconds") or 8.0),
                caller_name=str(agent_config.get("caller_name") or "Riya"),
                company_name=str(agent_config.get("company_name") or ""),
                pitch_summary=str(agent_config.get("pitch_summary") or ""),
                objective=str(agent_config.get("objective") or "lead_qualification"),
            )

        # Synthetic campaign_context so the downstream pipeline (safety net,
        # opener personalisation) sees a contact phone/name and can persist a
        # site visit or lead at session end even though no real number was dialled.
        campaign_context_for_tester: dict[str, Any] | None = None
        if sample_phone:
            campaign_context_for_tester = {
                "campaign_id": str(outbound_context.campaign_id),
                "contact": {"phone": sample_phone, "name": sample_name or "Tester"},
            }

        call_id = f"tester:{outbound_context.campaign_id}"

        # End-of-call routing: classify the transcript, persist a lead row
        # if the operator played an interested / partial-interest prospect,
        # and surface the result back to the frontend over the WS before
        # the close handshake finishes. Captures the campaign ref so the
        # closure can decide which campaign tab the lead lands in.
        campaign_ref_for_persist = campaign if campaign_id_param else None
        outbound_context_for_persist = outbound_context

        async def _on_session_end(ws: WebSocket) -> None:
            await _classify_and_persist_tester_outcome(
                ws,
                db=db,
                tenant_res=tr,
                call_id=call_id,
                user_id=user.id,
                campaign=campaign_ref_for_persist,
                outbound_context=outbound_context_for_persist,
            )

        await NokvoOneVoiceStreamService.run_session(
            websocket,
            tr,
            db=db,
            language=str(params.get("language") or "en"),
            call_id=call_id,
            campaign_context=campaign_context_for_tester,
            outbound_context_override=outbound_context,
            on_session_end=_on_session_end,
        )
        return


async def _classify_and_persist_tester_outcome(
    websocket: WebSocket,
    *,
    db: AsyncSession,
    tenant_res: TenantResources,
    call_id: str,
    user_id: uuid.UUID,
    campaign: OutboundCampaign | None,
    outbound_context: Any | None,
) -> None:
    """Classify the tester transcript and route the outcome:

      * ``interested``     → create lead under ``campaign.id`` so it shows
                             in that campaign's tab on the Leads page.
      * ``partial``        → create lead with ``data.uncategorized=True``
                             (lands in the global Uncategorized tab).
      * ``not_interested`` → no row created.

    Sends ``{type: "outcome", outcome, reason, lead_id}`` over the WS so
    the frontend can render the post-call banner. All persistence errors
    are logged and swallowed — the operator must never see a 500 just
    because the lead row failed to write.
    """
    from app.services.agent_session_store import AgentSessionStore
    from app.services.outbound_call_outcome_classifier import (
        OUTCOME_INTERESTED,
        OUTCOME_NOT_INTERESTED,
        classify_outbound_outcome,
    )
    from app.models.nokvo_one_tool_record import NokvoOneToolRecord

    try:
        history = await AgentSessionStore.get_history(tenant_res, call_id)
    except Exception:
        history = []

    # AgentSessionStore returns {role, content} pairs; the classifier wants
    # turn dicts with `query`/`answer` (matching the frontend tester shape).
    transcript_turns: list[dict[str, Any]] = []
    pending_user: str | None = None
    for entry in history or []:
        role = (entry.get("role") or "").lower()
        content = (entry.get("content") or "").strip()
        if not content:
            continue
        if role == "user":
            if pending_user is not None:
                transcript_turns.append({"query": pending_user, "answer": ""})
            pending_user = content
        elif role == "assistant":
            transcript_turns.append({"query": pending_user or "", "answer": content})
            pending_user = None
        else:
            transcript_turns.append({"query": "", "answer": content})
    if pending_user is not None:
        transcript_turns.append({"query": pending_user, "answer": ""})

    campaign_name = campaign.name if campaign is not None else None
    campaign_goal = None
    if outbound_context is not None:
        campaign_goal = getattr(outbound_context, "goal", None) or getattr(
            outbound_context, "pitch_summary", None
        )

    outcome = await classify_outbound_outcome(
        tenant_res,
        transcript_turns=transcript_turns,
        campaign_name=campaign_name,
        campaign_goal=campaign_goal,
    )

    lead_id: str | None = None
    if outcome.outcome != OUTCOME_NOT_INTERESTED:
        try:
            # Best-effort: lift name + phone from the transcript via the
            # conversational memory promoted into the session blob. We
            # avoid building a new NLP path here — the memory layer
            # already does this work for inbound calls.
            from app.services.conversational_memory import load_memory as _load_memory

            memory = await _load_memory(tenant_res, call_id)
            captured_name = memory.get("name") if hasattr(memory, "get") else None
            captured_phone = memory.get("phone") if hasattr(memory, "get") else None

            is_uncategorized = outcome.is_uncategorized
            data: dict[str, Any] = {
                "source": "outbound_tester",
                "outcome": outcome.outcome,
                "outcome_reason": outcome.reason,
                "call_id": call_id,
                "name": captured_name,
                "phone": captured_phone,
            }
            if campaign is not None and not is_uncategorized:
                data["campaign_id"] = str(campaign.id)
                data["campaign_name"] = campaign.name
            if is_uncategorized:
                data["uncategorized"] = True
                if campaign is not None:
                    # Keep the originating campaign as a soft pointer so the
                    # operator can still see "was tested against X" in the row
                    # detail, but tab routing reads the explicit flag.
                    data["originating_campaign_id"] = str(campaign.id)
                    data["originating_campaign_name"] = campaign.name

            record = NokvoOneToolRecord(
                id=uuid.uuid4(),
                organization_id=tenant_res.organization_id,
                created_by_user_id=user_id,
                record_type="lead",
                status=outcome.outcome,
                data={k: v for k, v in data.items() if v not in (None, "")},
                contact_phone=str(captured_phone) if captured_phone else None,
            )
            db.add(record)
            await db.commit()
            lead_id = str(record.id)
        except Exception:
            logger.exception("NOKVO-OUTBOUND-TESTER: failed to persist outcome lead")

    try:
        await websocket.send_json(
            {
                "type": "outcome",
                "outcome": outcome.outcome,
                "reason": outcome.reason,
                "lead_id": lead_id,
                "uncategorized": outcome.is_uncategorized,
                "campaign_id": str(campaign.id) if campaign is not None else None,
                "campaign_name": campaign.name if campaign is not None else None,
            }
        )
    except Exception:
        # WS already closed by client — outcome is still persisted, the
        # frontend can fall back to refreshing the Leads page to see it.
        pass


# ────────────────────────── Phone link configuration ──────────────────────────


class PhoneLinkConfigRequest(BaseModel):
    link_id: str | None = None


class LeadOauthStartRequest(BaseModel):
    provider: str = Field(pattern="^(meta_ads|google_ads|google_forms)$")
    mode: str = "ads"


class LeadConnectionUpdateRequest(BaseModel):
    display_name: str | None = None
    provider_account_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class LeadExternalFormRequest(BaseModel):
    provider: LeadSourceProvider
    name: str
    provider_form_id: str
    source_connection_id: uuid.UUID | None = None
    field_mapping: dict[str, Any] = Field(default_factory=dict)
    consent_field_key: str | None = None
    consent_text: str | None = None
    default_call_consent: bool = False


class LeadNokvoFormRequest(BaseModel):
    name: str
    fields: list[dict[str, Any]] = Field(default_factory=list)
    consent_text: str


class PublicLeadFormSubmitRequest(BaseModel):
    fields: dict[str, Any] = Field(default_factory=dict)


def _enum_value(value: Any) -> Any:
    return value.value if hasattr(value, "value") else value


def _connection_response(connection: LeadSourceConnection) -> dict[str, Any]:
    return {
        "id": str(connection.id),
        "provider": _enum_value(connection.provider),
        "status": _enum_value(connection.status),
        "display_name": connection.display_name,
        "provider_account_id": connection.provider_account_id,
        "scopes": connection.scopes or [],
        "metadata": connection.metadata_ or {},
        "last_sync_at": connection.last_sync_at.isoformat() if connection.last_sync_at else None,
        "last_error": connection.last_error,
        "created_at": connection.created_at.isoformat() if connection.created_at else None,
    }


def _form_public_url(request: Request, form: LeadCaptureForm) -> str | None:
    if not form.public_slug:
        return None
    base = f"{request.url.scheme}://{request.url.netloc}"
    return f"{base}/api/nokvo-one/agents/lead-sources/nokvo-forms/public/{form.public_slug}"


def _form_response(form: LeadCaptureForm, request: Request | None = None) -> dict[str, Any]:
    return {
        "id": str(form.id),
        "provider": _enum_value(form.provider),
        "status": _enum_value(form.status),
        "name": form.name,
        "provider_form_id": form.provider_form_id,
        "provider_account_id": form.provider_account_id,
        "source_connection_id": str(form.source_connection_id) if form.source_connection_id else None,
        "public_slug": form.public_slug,
        "public_url": _form_public_url(request, form) if request else None,
        "external_url": form.external_url,
        "field_schema": form.field_schema or [],
        "field_mapping": form.field_mapping or {},
        "consent_field_key": form.consent_field_key,
        "consent_text": form.consent_text,
        "default_call_consent": form.default_call_consent,
        "metadata": form.metadata_ or {},
        "last_synced_at": form.last_synced_at.isoformat() if form.last_synced_at else None,
        "created_at": form.created_at.isoformat() if form.created_at else None,
    }


def _lead_response(lead: OutgoingLead) -> dict[str, Any]:
    return {
        "id": str(lead.id),
        "source_provider": _enum_value(lead.source_provider),
        "source_connection_id": str(lead.source_connection_id) if lead.source_connection_id else None,
        "capture_form_id": str(lead.capture_form_id) if lead.capture_form_id else None,
        "provider_lead_id": lead.provider_lead_id,
        "name": lead.name,
        "email": lead.email,
        "phone_raw": lead.phone_raw,
        "phone_e164": lead.phone_e164,
        "fields": lead.fields or {},
        "source_metadata": lead.source_metadata or {},
        "consent_status": _enum_value(lead.consent_status),
        "consent_text": lead.consent_text,
        "consent_field_key": lead.consent_field_key,
        "consented_at": lead.consented_at.isoformat() if lead.consented_at else None,
        "submitted_at": lead.submitted_at.isoformat() if lead.submitted_at else None,
        "opt_out_at": lead.opt_out_at.isoformat() if lead.opt_out_at else None,
        "call_status": _enum_value(lead.call_status),
        "callable": lead_is_callable(lead),
        "created_at": lead.created_at.isoformat() if lead.created_at else None,
    }


def _phone_link_summary(request: Request, tr: TenantResources) -> dict[str, Any]:
    link = dict((tr.provider_status or {}).get("agent_phone_link") or {})
    link_id = link.get("link_id")
    status_val = link.get("status") or ("linked" if link_id else "not_linked")
    host = request.url.hostname or "localhost"
    scheme_http = "https" if request.url.scheme == "https" else "http"
    scheme_ws = "wss" if request.url.scheme == "https" else "ws"
    port = f":{request.url.port}" if request.url.port else ""
    inbound_webhook = (
        f"{scheme_http}://{host}{port}/api/nokvo-one/agents/exotel/voice/{link_id}"
        if link_id else None
    )
    inbound_media = (
        f"{scheme_ws}://{host}{port}/api/nokvo-one/agents/exotel/media/{link_id}"
        if link_id else None
    )
    return {
        "link_id": link_id,
        "status": status_val,
        "exotel_webhook_url": inbound_webhook,
        "exotel_media_url": inbound_media,
    }


@router.get("/phone-link")
async def get_phone_link(
    request: Request,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    return _phone_link_summary(request, tr)


@router.post("/phone-link")
async def set_phone_link(
    payload: PhoneLinkConfigRequest,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    provider_status = dict(tr.provider_status or {})
    link = dict(provider_status.get("agent_phone_link") or {})
    if payload.link_id:
        link.update({"link_id": payload.link_id.strip(), "status": "linked"})
    else:
        link.update({"link_id": None, "status": "not_linked"})
    provider_status["agent_phone_link"] = link
    tr.provider_status = provider_status
    flag_modified(tr, "provider_status")
    db.add(tr)
    await db.commit()
    await db.refresh(tr)
    return _phone_link_summary(request, tr)


# ────────────────────────── Lead sources and consented leads ──────────────────────────


@router.get("/lead-sources/connections")
async def list_lead_connections(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    connections = await OutgoingLeadService.list_connections(tr, db)
    return [_connection_response(c) for c in connections]


@router.post("/lead-sources/oauth/start")
async def start_lead_oauth(
    payload: LeadOauthStartRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    try:
        return OutgoingLeadService.oauth_start_url(
            tr,
            organization_id=user.organization_id,
            user_id=user.id,
            provider=payload.provider,
            mode=payload.mode,
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc


@router.get("/lead-sources/oauth/{provider}/callback")
async def lead_oauth_callback(
    provider: str,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db: AsyncSession = Depends(deps.get_db),
):
    public_base = settings.NOKVO_ONE_PUBLIC_BASE_URL.rstrip("/") or "http://localhost:5173"
    if error:
        return RedirectResponse(f"{public_base}?lead_connection=error&provider={provider}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="OAuth code and state are required")
    try:
        state_data = decode_oauth_state(state)
        if state_data.get("provider") != provider:
            raise OutgoingLeadServiceError("OAuth provider does not match state.")
        tr = await _tenant_by_tenant_id(db, str(state_data["tenant_id"]))
        if tr is None:
            raise OutgoingLeadServiceError("Tenant resources not found for OAuth callback.")
        await OutgoingLeadService.exchange_oauth_code(
            db,
            tenant_res=tr,
            user_id=uuid.UUID(str(state_data["user_id"])),
            provider=provider,
            mode=str(state_data.get("mode") or ""),
            code=code,
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return RedirectResponse(f"{public_base}?lead_connection=success&provider={provider}")


@router.patch("/lead-sources/connections/{connection_id}")
async def update_lead_connection(
    connection_id: uuid.UUID,
    payload: LeadConnectionUpdateRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    connection = await _connection_for_user(db, tr, connection_id)
    connection = await OutgoingLeadService.update_connection_metadata(
        connection,
        db,
        metadata=payload.metadata,
        display_name=payload.display_name,
        provider_account_id=payload.provider_account_id,
    )
    return _connection_response(connection)


@router.post("/lead-sources/connections/{connection_id}/sync")
async def sync_lead_connection(
    connection_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    connection = await _connection_for_user(db, tr, connection_id)
    try:
        return await OutgoingLeadService.sync_connection(connection, db)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc


@router.post("/lead-sources/connections/{connection_id}/disconnect")
async def disconnect_lead_connection(
    connection_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    connection = await _connection_for_user(db, tr, connection_id)
    try:
        connection = await OutgoingLeadService.disconnect_connection(connection, db)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return _connection_response(connection)


@router.get("/lead-sources/forms")
async def list_lead_forms(
    request: Request,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    forms = await OutgoingLeadService.list_forms(tr, db)
    return [_form_response(form, request) for form in forms]


@router.post("/lead-sources/forms", status_code=status.HTTP_201_CREATED)
async def register_external_lead_form(
    payload: LeadExternalFormRequest,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    if payload.source_connection_id:
        await _connection_for_user(db, tr, payload.source_connection_id)
    try:
        form = await OutgoingLeadService.register_external_form(
            tr,
            db,
            created_by_user_id=user.id,
            provider=payload.provider,
            name=payload.name,
            provider_form_id=payload.provider_form_id,
            source_connection_id=payload.source_connection_id,
            field_mapping=payload.field_mapping,
            consent_field_key=payload.consent_field_key,
            consent_text=payload.consent_text,
            default_call_consent=payload.default_call_consent,
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return _form_response(form, request)


@router.post("/lead-sources/nokvo-forms", status_code=status.HTTP_201_CREATED)
async def create_nokvo_lead_form(
    payload: LeadNokvoFormRequest,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    try:
        form = await OutgoingLeadService.create_nokvo_form(
            tr,
            db,
            created_by_user_id=user.id,
            name=payload.name,
            fields=payload.fields,
            consent_text=payload.consent_text,
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return _form_response(form, request)


@router.get("/lead-sources/nokvo-forms/public/{slug}")
async def get_public_nokvo_form(slug: str, request: Request, db: AsyncSession = Depends(deps.get_db)):
    form = await OutgoingLeadService.get_public_form(slug, db)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    return _form_response(form, request)


@router.post("/lead-sources/nokvo-forms/public/{slug}/submit", status_code=status.HTTP_201_CREATED)
async def submit_public_nokvo_form(
    slug: str,
    payload: PublicLeadFormSubmitRequest,
    request: Request,
    db: AsyncSession = Depends(deps.get_db),
):
    form = await OutgoingLeadService.get_public_form(slug, db)
    if form is None:
        raise HTTPException(status_code=404, detail="Form not found")
    try:
        lead = await OutgoingLeadService.submit_public_form(
            form,
            db,
            fields=payload.fields,
            request_metadata={
                "ip": request.client.host if request.client else None,
                "user_agent": request.headers.get("user-agent"),
                "referer": request.headers.get("referer"),
            },
        )
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return {"ok": True, "lead_id": str(lead.id)}


@router.get("/lead-sources/leads")
async def list_outgoing_leads(
    eligible_only: bool = False,
    limit: int = 200,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    leads = await OutgoingLeadService.list_leads(tr, db, eligible_only=eligible_only, limit=limit)
    return [_lead_response(lead) for lead in leads]


@router.get("/lead-sources/meta/webhook")
async def verify_meta_leadgen_webhook(request: Request):
    params = request.query_params
    if (
        params.get("hub.mode") == "subscribe"
        and params.get("hub.verify_token") == settings.META_LEADGEN_WEBHOOK_VERIFY_TOKEN
    ):
        return PlainTextResponse(params.get("hub.challenge") or "")
    raise HTTPException(status_code=403, detail="Meta webhook verification failed")


def _verify_meta_signature(raw_body: bytes, header: str | None) -> bool:
    """Verify Meta's X-Hub-Signature-256 header against the raw request body
    using META_ADS_APP_SECRET. Returns False when secret is unset (fail closed)."""
    if not header or not settings.META_ADS_APP_SECRET:
        return False
    prefix = "sha256="
    if not header.startswith(prefix):
        return False
    expected = hmac.new(
        settings.META_ADS_APP_SECRET.encode("utf-8"),
        raw_body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, header[len(prefix):])


@router.post("/lead-sources/meta/webhook")
async def receive_meta_leadgen_webhook(request: Request, db: AsyncSession = Depends(deps.get_db)):
    raw_body = await request.body()
    signature = request.headers.get("x-hub-signature-256")
    if not _verify_meta_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="Meta webhook signature verification failed")
    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="Invalid Meta webhook payload")
    imported = 0
    errors: list[str] = []
    for entry in payload.get("entry") or []:
        for change in entry.get("changes") or []:
            if change.get("field") != "leadgen":
                continue
            value = change.get("value") or {}
            leadgen_id = value.get("leadgen_id")
            form_id = value.get("form_id")
            if not leadgen_id or not form_id:
                continue
            try:
                lead = await OutgoingLeadService.ingest_meta_leadgen_event(
                    db,
                    form_id=str(form_id),
                    leadgen_id=str(leadgen_id),
                )
                if lead:
                    imported += 1
            except Exception as exc:
                errors.append(str(exc)[:200])
    return {"ok": True, "imported": imported, "errors": errors[:5]}


# ────────────────────────── Exotel inbound ──────────────────────────


@router.post("/exotel/voice/{link_id}", response_class=PlainTextResponse)
async def exotel_inbound_webhook(
    link_id: str, request: Request, db: AsyncSession = Depends(deps.get_db)
):
    tr = await _tenant_by_link_id(db, link_id)
    if not tr:
        return PlainTextResponse(
            "Nokvo One agent is not linked to this number.", status_code=404
        )
    host = request.url.hostname or "localhost"
    scheme = "wss" if request.url.scheme == "https" else "ws"
    port = f":{request.url.port}" if request.url.port else ""
    media_url = f"{scheme}://{host}{port}/api/nokvo-one/agents/exotel/media/{link_id}"
    return PlainTextResponse(media_url, media_type="text/plain")


@router.websocket("/exotel/media/{link_id}")
async def exotel_inbound_media_websocket(websocket: WebSocket, link_id: str):
    async for db in deps.get_db():
        tr = await _tenant_by_link_id(db, link_id)
        if not tr:
            await websocket.close(code=1008)
            return
        await ExotelBridgeService.run_session(websocket, tr, db=db)
        return


# ────────────────────────── Exotel outbound ──────────────────────────


@router.post("/exotel/outbound-status/{call_link_id}")
async def exotel_outbound_status(
    call_link_id: str, request: Request, db: AsyncSession = Depends(deps.get_db)
):
    campaign, contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
    is_followup = bool(contact and contact.get("is_followup"))
    if not campaign and not is_followup:
        return {"ok": False, "reason": "campaign_not_found"}
    try:
        payload = dict(await request.form())
    except Exception:
        payload = await request.json()
    event_type = str(
        payload.get("Status")
        or payload.get("status")
        or payload.get("CallStatus")
        or payload.get("event")
        or "call.update"
    )
    normalized = (
        "call.answered"
        if event_type.lower() in {"answered", "in-progress", "in progress"}
        else "call.hangup"
    )
    await OutboundCampaignService.handle_call_status(
        campaign,
        call_link_id,
        normalized,
        payload,
        db,
        followup_contact=contact if is_followup else None,
    )
    return {"ok": True, "call_link_id": call_link_id}


@router.websocket("/exotel/outbound-media/{call_link_id}")
async def exotel_outbound_media_websocket(websocket: WebSocket, call_link_id: str):
    async for db in deps.get_db():
        campaign, contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
        is_followup = bool(contact and contact.get("is_followup"))
        if not contact:
            await websocket.close(code=1008)
            return
        # Follow-up calls may not be attached to a campaign (manual
        # follow-ups). When campaign is None, we still need the tenant
        # to resolve infra — pull it from the lead via the contact's
        # lead_id, attached via OutgoingLead.
        tenant_id = campaign.tenant_id if campaign is not None else None
        if tenant_id is None and contact.get("lead_id"):
            from app.models.outgoing_lead import OutgoingLead as _OL
            lead_res = await db.execute(
                select(_OL).where(_OL.id == uuid.UUID(str(contact["lead_id"])))
            )
            lead = lead_res.scalars().first()
            tenant_id = lead.tenant_id if lead else None
        if not tenant_id:
            await websocket.close(code=1008)
            return
        tr = await _tenant_by_tenant_id(db, tenant_id)
        if not tr:
            await websocket.close(code=1008)
            return
        # Outbound campaigns can be authored in any supported language —
        # pull it off the persisted agent_config (default "en" for legacy
        # campaigns). Follow-ups inherit the parent campaign's language
        # when one is attached.
        campaign_language = str(
            ((campaign.agent_config or {}).get("language") if campaign else "en")
            or "en"
        ).strip().lower() or "en"
        adapter = ExotelWebSocketAdapter(websocket, language=campaign_language)
        campaign_context = {
            "campaign_id": str(campaign.id) if campaign else None,
            "goal": campaign.name if campaign else "Follow-up call",
            "contact": contact,
            "language": campaign_language,
            "is_followup": is_followup,
            "attempt_n": int(contact.get("_attempt_n") or 0) + 1 if is_followup else None,
            "source_call_id": contact.get("_source_call_id") if is_followup else None,
            "opening_message": (
                f"Start the follow-up call for {contact.get('name') or 'the recipient'}. "
                "Acknowledge the prior conversation briefly before asking your first question."
                if is_followup
                else (
                    f"Start the outbound campaign call for {contact.get('name') or 'the recipient'}. "
                    "Use the campaign script context, introduce yourself briefly, and ask if this is a good time to talk."
                )
            ),
        }
        await NokvoOneVoiceStreamService.run_session(
            adapter,
            tr,
            db=db,
            language=campaign_language,
            call_id=call_link_id,
            campaign_context=campaign_context,
        )
        return


# ────────────────────────── Campaigns ──────────────────────────


def _campaign_response(c: OutboundCampaign) -> dict[str, Any]:
    status_val = c.status.value if hasattr(c.status, "value") else c.status
    return {
        "id": str(c.id),
        "name": c.name,
        "status": status_val,
        "from_number": c.from_number,
        "total_count": c.total_count or 0,
        "answered_count": c.answered_count or 0,
        "failed_count": c.failed_count or 0,
        "contacts": c.contacts or [],
        "agent_config": c.agent_config or {},
        "doc_blob_path": c.doc_blob_path,
        "created_at": c.created_at.isoformat() if c.created_at else None,
        "started_at": c.started_at.isoformat() if c.started_at else None,
        "completed_at": c.completed_at.isoformat() if c.completed_at else None,
    }


def _parse_campaign_list_field(value: str | None) -> list[str]:
    if not value:
        return []
    raw = value.strip()
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw.splitlines()
    if isinstance(parsed, str):
        parsed = parsed.splitlines()
    if not isinstance(parsed, list):
        return []
    return [str(item).strip() for item in parsed if str(item).strip()]


@router.get("/campaigns")
async def list_campaigns(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    items = await OutboundCampaignService.list_campaigns(tr, db)
    return [_campaign_response(c) for c in items]


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    name: str = Form(...),
    lead_ids: str | None = Form(None),
    excel_file: UploadFile | None = File(None),
    doc_file: UploadFile | None = File(None),
    from_number: str | None = Form(None),
    agent_prompt: str = Form(...),
    objectives: str | None = Form(None),
    exit_conditions: str | None = Form(None),
    tone: str | None = Form(None),
    silence_timeout_seconds: float | None = Form(None),
    followup_rules: str | None = Form(None),
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Create a campaign.

    The campaign's "knowledge" is the ``agent_prompt`` itself — there is
    no separate reference document. ``doc_file`` is kept as an optional
    legacy input but new callers should leave it empty.

    ``lead_ids`` is optional. When omitted, the campaign is created in
    prompt-only mode and consented leads can be attached later via
    ``POST /campaigns/{id}/leads``.
    """
    tr = await _tenant_for_user(db, user)
    if not (agent_prompt or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Campaigns need an agent prompt — it's what the agent reads "
                "during the call. Add one and try again."
            ),
        )
    try:
        parsed_lead_ids: list[uuid.UUID] = []
        if lead_ids:
            raw_ids = json.loads(lead_ids) if lead_ids.strip().startswith("[") else [x.strip() for x in lead_ids.split(",")]
            parsed_lead_ids = [uuid.UUID(str(item)) for item in raw_ids if str(item).strip()]
        agent_config = {
            "agent_prompt": agent_prompt,
            "objectives": _parse_campaign_list_field(objectives),
            "exit_conditions": _parse_campaign_list_field(exit_conditions),
            "tone": tone,
            "silence_timeout_seconds": silence_timeout_seconds,
        }
        # Follow-up rules — optional. Stored under agent_config.followup_rules
        # so the FollowupSchedulerService can read them via effective_followup_rules.
        if followup_rules:
            try:
                parsed_rules = json.loads(followup_rules)
                if isinstance(parsed_rules, dict):
                    agent_config["followup_rules"] = parsed_rules
            except json.JSONDecodeError:
                # Silently drop on parse failure — defaults still apply at
                # read time, so the campaign creates without follow-up
                # rules rather than 400ing on a UI bug.
                pass
        if parsed_lead_ids:
            campaign = await OutboundCampaignService.create_campaign_from_leads(
                tr,
                db,
                name=name,
                lead_ids=parsed_lead_ids,
                doc_file=doc_file,
                from_number=from_number,
                agent_config=agent_config,
            )
        else:
            campaign = await OutboundCampaignService.create_campaign_prompt_only(
                tr,
                db,
                name=name,
                from_number=from_number,
                agent_config=agent_config,
            )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.post("/campaigns/{campaign_id}/leads")
async def attach_campaign_leads(
    campaign_id: uuid.UUID,
    lead_ids: str = Form(...),
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Attach consented leads to an existing draft campaign.

    ``lead_ids`` may be a JSON array string (``"[\"uuid\", ...]"``) or a
    comma-separated list. Each lead must be callable + scoped to the
    caller's tenant. Already-attached leads are silently skipped.
    """
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        raw_ids = json.loads(lead_ids) if lead_ids.strip().startswith("[") else [
            x.strip() for x in lead_ids.split(",")
        ]
        parsed = [uuid.UUID(str(item)) for item in raw_ids if str(item).strip()]
        campaign = await OutboundCampaignService.attach_leads(
            tr, db, campaign=campaign, lead_ids=parsed
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    except OutgoingLeadServiceError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.delete("/campaigns/{campaign_id}/leads/{lead_id}")
async def detach_campaign_lead(
    campaign_id: uuid.UUID,
    lead_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        campaign = await OutboundCampaignService.detach_lead(
            tr, db, campaign=campaign, lead_id=lead_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.get("/campaigns/{campaign_id}")
async def get_campaign(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return _campaign_response(campaign)


@router.post("/campaigns/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    public_base = f"{request.url.scheme}://{request.url.netloc}"
    try:
        campaign = await OutboundCampaignService.launch_campaign(
            campaign,
            db,
            public_base_url=public_base,
            path_prefix="/api/nokvo-one/agents",
            tenant_res=tr,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.post("/campaigns/{campaign_id}/cancel")
async def cancel_campaign(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        campaign = await OutboundCampaignService.cancel_campaign(campaign, db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.delete("/campaigns/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Hard-delete a campaign and its contact join rows.

    Running campaigns are protected — operators must cancel them first.
    Lead records and call-cost rows are NOT touched (leads outlive campaigns;
    cost ledger remains as a billing audit trail).
    """
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        await OutboundCampaignService.delete_campaign(campaign, db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    return None


# ───────────────────────── Follow-ups ─────────────────────────
#
# The follow-up agent surfaces here. Six endpoints, all admin + MFA gated:
#   GET    /followups/summary               → tile counts for the dashboard
#   GET    /followups?lead_id=…             → all rows for one lead (chip data)
#   POST   /followups/{id}/pause            → admin pause one pending row
#   POST   /followups/{id}/resume           → admin resume one paused row
#   DELETE /followups?lead_id=…             → cancel all pending for a lead
#   POST   /followups/manual                → admin schedules a one-off row
#
# The scheduler loop (app/services/followup_scheduler.py) does the actual
# call placement. These endpoints exist for visibility + manual override —
# the day-to-day flow needs none of them.


def _followup_response(row) -> dict:
    """Serializer. The schedule rows carry enums + UUIDs that JSON can't
    encode directly, so we flatten to a stable shape the frontend can
    consume in the chip + summary widgets."""
    return {
        "id": str(row.id),
        "tenant_id": row.tenant_id,
        "lead_id": str(row.lead_id),
        "campaign_id": str(row.campaign_id) if row.campaign_id else None,
        "source_call_id": row.source_call_id,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        "reason": row.reason.value if row.reason else None,
        "attempts": int(row.attempts or 0),
        "status": row.status.value if row.status else None,
        "placed_call_id": row.placed_call_id,
        "paused_at": row.paused_at.isoformat() if row.paused_at else None,
        "paused_by": str(row.paused_by) if row.paused_by else None,
        "cancelled_reason": row.cancelled_reason,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/followups/summary")
async def followups_summary(
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Tile counts for the dashboard — scheduled / today / exhausted /
    paused. One DB hit, scoped to the caller's tenant."""
    from app.services.followup_scheduler_service import FollowupSchedulerService

    tr = await _tenant_for_user(db, user)
    return await FollowupSchedulerService.summary_for_tenant(
        tenant_id=tr.tenant_id, db=db
    )


@router.get("/followups")
async def list_followups_for_lead(
    lead_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """All scheduled / completed / cancelled follow-ups for one lead.
    Newest first. Powers the per-lead chip in the Leads tab."""
    from app.services.followup_scheduler_service import FollowupSchedulerService

    # Tenant scoping: ensure the lead belongs to this admin's tenant before
    # returning its follow-up history. Skips the cross-tenant info leak.
    tr = await _tenant_for_user(db, user)
    lead_res = await db.execute(
        select(OutgoingLead).where(OutgoingLead.id == lead_id)
    )
    lead = lead_res.scalars().first()
    if lead is None or lead.tenant_id != tr.tenant_id:
        raise HTTPException(status_code=404, detail="Lead not found")
    rows = await FollowupSchedulerService.for_lead(lead_id=lead_id, db=db)
    return {"items": [_followup_response(r) for r in rows]}


@router.post("/followups/{followup_id}/pause")
async def pause_followup(
    followup_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Pause a pending follow-up. Status flips to ``paused``; the scheduler
    skips paused rows on every tick. Idempotent — pausing an already-paused
    row is a no-op."""
    from app.services.followup_scheduler_service import FollowupSchedulerService

    row = await FollowupSchedulerService.pause_row(
        row_id=followup_id, by_user_id=user.id, db=db
    )
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Follow-up not found or not pending",
        )
    return _followup_response(row)


@router.post("/followups/{followup_id}/resume")
async def resume_followup(
    followup_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Resume a paused follow-up. The scheduled_at is re-clamped to the
    campaign's current call window (admin may have shifted hours)."""
    from app.services.followup_scheduler_service import FollowupSchedulerService

    row = await FollowupSchedulerService.resume_row(row_id=followup_id, db=db)
    if row is None:
        raise HTTPException(
            status_code=404,
            detail="Follow-up not found or not paused",
        )
    return _followup_response(row)


@router.delete("/followups")
async def cancel_followups_for_lead(
    lead_id: uuid.UUID,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Cancel every pending + paused follow-up for one lead. Surfaces the
    count cancelled so the UI can show feedback. Audit trail is preserved
    in ``cancelled_reason='admin'``."""
    from app.services.followup_scheduler_service import FollowupSchedulerService

    tr = await _tenant_for_user(db, user)
    lead_res = await db.execute(
        select(OutgoingLead).where(OutgoingLead.id == lead_id)
    )
    lead = lead_res.scalars().first()
    if lead is None or lead.tenant_id != tr.tenant_id:
        raise HTTPException(status_code=404, detail="Lead not found")
    count = await FollowupSchedulerService.cancel_for_lead(
        lead_id=lead_id, reason="admin", db=db
    )
    return {"cancelled": count}


@router.post("/followups/manual")
async def schedule_manual_followup(
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Admin manually schedules a follow-up call for a lead.

    Body: {lead_id, campaign_id?, scheduled_at (ISO string), note?}

    The scheduled_at is clamped into the campaign's call window (or the
    default window if no campaign is attached). Reason is set to
    ``manual`` so the chip can mark it visibly distinct from auto rules.
    """
    from datetime import datetime
    from app.models.lead_followup_schedule import (
        FollowupReason,
        FollowupStatus,
        LeadFollowupSchedule,
    )
    from app.services.followup_scheduler_service import (
        clamp_to_call_window,
        effective_followup_rules,
    )

    body = await request.json()
    lead_id = body.get("lead_id")
    if not lead_id:
        raise HTTPException(status_code=400, detail="lead_id required")
    iso = body.get("scheduled_at")
    if not iso:
        raise HTTPException(status_code=400, detail="scheduled_at required")
    try:
        when = datetime.fromisoformat(iso)
    except ValueError:
        raise HTTPException(status_code=400, detail="scheduled_at must be ISO-8601")

    tr = await _tenant_for_user(db, user)
    lead_res = await db.execute(
        select(OutgoingLead).where(OutgoingLead.id == uuid.UUID(str(lead_id)))
    )
    lead = lead_res.scalars().first()
    if lead is None or lead.tenant_id != tr.tenant_id:
        raise HTTPException(status_code=404, detail="Lead not found")

    campaign = None
    if body.get("campaign_id"):
        camp_res = await db.execute(
            select(OutboundCampaign).where(
                OutboundCampaign.id == uuid.UUID(str(body["campaign_id"]))
            )
        )
        campaign = camp_res.scalars().first()
        if campaign is None or campaign.tenant_id != tr.tenant_id:
            raise HTTPException(status_code=404, detail="Campaign not found")

    rules = effective_followup_rules(campaign)
    when = clamp_to_call_window(when, rules)
    row = LeadFollowupSchedule(
        id=uuid.uuid4(),
        tenant_id=tr.tenant_id,
        lead_id=lead.id,
        campaign_id=campaign.id if campaign else None,
        source_call_id=None,
        scheduled_at=when,
        reason=FollowupReason.manual,
        attempts=0,
        status=FollowupStatus.pending,
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return _followup_response(row)
