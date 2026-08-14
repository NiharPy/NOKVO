"""Nokvo One voice pipeline router.

Mounted at /api/nokvo-one/agents. Exposes the tenant-isolated voice/RAG
endpoints described in the architecture spec:

  - GET  /runtime/status                          — pipeline diagnostics
  - WS   /voice/ws                                 — browser mic tester (admin/manager JWT)
  - GET  /phone-link                               — assigned Plivo DID + forwarding info
  - POST /phone-link                               — tenant saves their forward-from number
  - POST /plivo/voice/{link_id}                    — Plivo inbound answer (Stream XML)
  - WS   /plivo/media/{link_id}                    — Plivo inbound media stream
  - POST /plivo/outbound-answer/{call_link_id}     — Plivo outbound answer (Stream XML)
  - POST /plivo/outbound-status/{call_link_id}     — outbound call status
  - WS   /plivo/outbound-media/{call_link_id}      — outbound media stream
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
import base64
import contextlib
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
from fastapi.responses import PlainTextResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
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
from app.services.plivo_bridge_service import PlivoBridgeService, PlivoWebSocketAdapter
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

_ALLOWED_STATUSES = ["active", "suspended"]


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


# SHARED bulk-calling deps — accept BOTH Nokvo One and NOKVO APEX orgs. The
# deterministic bulk-calling surface is shared between the two products (APEX is
# the deterministic product, Nokvo One the non-deterministic), but every query is
# org-scoped so each product only ever sees its own campaigns/contacts. Used ONLY
# on the bulk-calling + transcript endpoints; every other Nokvo One endpoint stays
# nokvo_one-only via _admin_dep/_viewer_dep.
_BULK_PRODUCT_TIERS = ["nokvo_one", "nokvo_apex"]


def _bulk_admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=_ALLOWED_STATUSES,
        allowed_roles=["admin"],
        allowed_product_tiers=_BULK_PRODUCT_TIERS,
    )


def _bulk_viewer_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=_ALLOWED_STATUSES,
        allowed_roles=["admin", "manager"],
        allowed_product_tiers=_BULK_PRODUCT_TIERS,
    )


async def _tenant_for_user(db: AsyncSession, user: OrganizationUser) -> TenantResources:
    res = await db.execute(
        select(TenantResources).where(TenantResources.organization_id == user.organization_id)
    )
    tr = res.scalars().first()
    if not tr:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")
    return tr


async def _org_for_user(db: AsyncSession, user: OrganizationUser) -> Organization:
    res = await db.execute(select(Organization).where(Organization.id == user.organization_id))
    org = res.scalars().first()
    if not org:
        raise HTTPException(status_code=404, detail="Organization not found")
    return org


# Shown when a Nokvo One org hits an outbound surface while the kill switch is
# off — outbound has moved to the dedicated NOKVO APEX product.
_NOKVO_ONE_OUTBOUND_DISABLED_DETAIL = (
    "Outbound calling is no longer available on Nokvo One. It has moved to "
    "NOKVO APEX, our dedicated outbound product."
)


def _nokvo_one_outbound_blocked(org: Organization) -> bool:
    """True when this org is blocked from outbound by the Nokvo One kill switch.

    Outbound calling has moved to the dedicated NOKVO APEX product, so Nokvo One
    (``product_tier="nokvo_one"``) is inbound-only when ``NOKVO_ONE_OUTBOUND_ENABLED``
    is off (the default). APEX orgs (``product_tier="nokvo_apex"``) share the
    bulk-calling endpoints and are explicitly EXEMPT — they keep dialing."""
    return (
        (org.product_tier or "nokvo_one") == "nokvo_one"
        and not settings.NOKVO_ONE_OUTBOUND_ENABLED
    )


async def _require_outbound_enabled(db: AsyncSession, user: OrganizationUser) -> Organization:
    """Gate outbound calling. First the product kill switch (Nokvo One outbound
    has moved to NOKVO APEX), then the plan: only the Inbound + Outbound plan
    sets ``calling_enabled`` (see Razorpay payment-gated onboarding).

    The prepaid-balance gate is NOT here: deterministic / bulk-questionnaire
    campaigns are billed separately and must remain creatable + dialable with a
    zero prepaid balance. The balance gate lives in the dialer for
    NON-deterministic campaigns + the inbound webhook (see the prepaid overhaul)."""
    org = await _org_for_user(db, user)
    if _nokvo_one_outbound_blocked(org):
        raise HTTPException(status_code=403, detail=_NOKVO_ONE_OUTBOUND_DISABLED_DETAIL)
    if not org.calling_enabled:
        raise HTTPException(
            status_code=403,
            detail=(
                "Outbound calling isn't included in your plan. Upgrade to "
                "Inbound + Outbound to launch calling campaigns."
            ),
        )
    return org


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
        # Eagerly capture every ORM-bound primitive the closure will need
        # downstream. By the time _on_session_end fires (potentially many
        # minutes into the call, after multiple commits have flowed
        # through ``db``), the originally-loaded ``user`` and ``tr`` ORM
        # instances are detached / expired in ways that make attribute
        # access raise SQLAlchemy's MissingGreenlet — the implicit lazy-
        # load wants an async context that isn't bound at that moment.
        # Snapshot everything to scalars while we're still in the request
        # scope and thread those through.
        user_id_for_persist = user.id
        org_id_for_persist = tr.organization_id
        tenant_id_for_persist = tr.tenant_id

        async def _on_session_end(ws: WebSocket) -> None:
            await _classify_and_persist_tester_outcome(
                ws,
                db=db,
                tenant_res=tr,
                tenant_id=tenant_id_for_persist,
                organization_id=org_id_for_persist,
                call_id=call_id,
                user_id=user_id_for_persist,
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


async def _write_call_note_to_record(
    *,
    tenant_res: TenantResources,
    call_id: str,
    record_id: Any,
    campaign: OutboundCampaign | None,
) -> None:
    """Run the condenser once and write ``data.handoff_note`` onto a
    ``nokvo_one_tool_records`` row (the outbound tester's site-visit / lead).

    The two shared teardown condensers skip the outbound-tester path — the
    OutgoingLead-contact one needs a contact a tester doesn't have, and the
    tool-record one is gated to inbound — so without this the tester's records
    are left note-less. Best-effort: own session, bounded, never raises out.
    """
    try:
        rec_uuid = record_id if isinstance(record_id, uuid.UUID) else uuid.UUID(str(record_id))
    except (TypeError, ValueError):
        return
    try:
        from datetime import datetime as _dt, timezone as _tz

        from app.db.session import AsyncSessionLocal
        from app.models.nokvo_one_tool_record import NokvoOneToolRecord
        from app.services.call_condenser_service import condense_call

        note = await condense_call(
            tenant_res=tenant_res,
            call_id=call_id,
            campaign_name=campaign.name if campaign is not None else None,
            timeout_s=8.0,
        )
        if not note:
            return
        async with AsyncSessionLocal() as note_db:
            rec = await note_db.get(NokvoOneToolRecord, rec_uuid)
            if rec is None:
                return
            data = dict(rec.data or {})
            data["handoff_note"] = note
            data["handoff_note_generated_at"] = _dt.now(_tz.utc).isoformat()
            rec.data = data
            flag_modified(rec, "data")
            note_db.add(rec)
            await note_db.commit()
        logger.info(
            "NOKVO-OUTBOUND-TESTER: handoff note written to record %s (%d chars)",
            rec_uuid, len(note),
        )
    except Exception:
        logger.exception("NOKVO-OUTBOUND-TESTER: handoff note write failed")


async def _classify_and_persist_tester_outcome(
    websocket: WebSocket,
    *,
    db: AsyncSession,
    tenant_res: TenantResources,
    call_id: str,
    user_id: uuid.UUID,
    campaign: OutboundCampaign | None,
    outbound_context: Any | None,
    tenant_id: str | None = None,
    organization_id: uuid.UUID | None = None,
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

    # Resolve identifiers from the eagerly-captured primitives when the
    # caller passed them — these are the only safe path to read tenant /
    # organization at session end. Falling back to the ORM attributes
    # would trigger SQLAlchemy's MissingGreenlet because the long-lived
    # ``tenant_res`` may be detached / expired by the time this closure
    # fires. Both kwargs are optional for back-compat with older callers.

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

    # Did the WS teardown (maybe_create_real_estate_lead_from_call, which runs
    # BEFORE this hook) already file a record for this call? A booked SITE VISIT
    # must stay site-visit-ONLY, and an already-captured LEAD must not be
    # duplicated. We still classify + send the banner, and below we make sure
    # that record carries the post-call note.
    pre_state: dict[str, Any] = {}
    try:
        pre_state = await AgentSessionStore.get_state(tenant_res, call_id) or {}
    except Exception:
        pre_state = {}
    existing_site_visit_id = pre_state.get("auto_site_visit_id")
    existing_lead_id = pre_state.get("auto_lead_id")

    lead_id: str | None = str(existing_lead_id) if existing_lead_id else None
    site_visit_id: str | None = str(existing_site_visit_id) if existing_site_visit_id else None
    # The single record this call is really about — gets the post-call note (Fix B).
    note_target_id: Any = existing_site_visit_id or existing_lead_id

    if existing_site_visit_id or existing_lead_id:
        logger.info(
            "NOKVO-OUTBOUND-TESTER: teardown already filed a %s for call %s — not duplicating",
            "site visit" if existing_site_visit_id else "lead", call_id,
        )
    elif outcome.outcome != OUTCOME_NOT_INTERESTED:
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
                organization_id=organization_id,
                created_by_user_id=user_id,
                record_type="lead",
                status=outcome.outcome,
                data={k: v for k, v in data.items() if v not in (None, "")},
                contact_phone=str(captured_phone) if captured_phone else None,
            )
            db.add(record)
            await db.commit()
            lead_id = str(record.id)
            note_target_id = record.id
        except Exception:
            logger.exception("NOKVO-OUTBOUND-TESTER: failed to persist outcome lead")

    try:
        await websocket.send_json(
            {
                "type": "outcome",
                "outcome": outcome.outcome,
                "reason": outcome.reason,
                "lead_id": lead_id,
                # Present when the prospect booked a visit — the banner is then a
                # site-visit confirmation, not a lead capture.
                "site_visit_id": site_visit_id,
                "uncategorized": outcome.is_uncategorized,
                "campaign_id": str(campaign.id) if campaign is not None else None,
                "campaign_name": campaign.name if campaign is not None else None,
            }
        )
    except Exception:
        # WS already closed by client — outcome is still persisted, the
        # frontend can fall back to refreshing the Leads page to see it.
        pass

    # Fix B: make sure the record this call produced (the booked site visit, or
    # the lead) carries the post-call note. The shared teardown condensers skip
    # the outbound-tester path, so it would otherwise be note-less. Best-effort,
    # bounded; the banner is already sent so this only adds teardown latency.
    if note_target_id is not None:
        await _write_call_note_to_record(
            tenant_res=tenant_res,
            call_id=call_id,
            record_id=note_target_id,
            campaign=campaign,
        )


# ────────────────────────── Phone link configuration ──────────────────────────


class PhoneLinkConfigRequest(BaseModel):
    # The tenant's own number that they forward to the assigned Plivo DID. Stored
    # for display / caller-ID matching (the DID + webhook are auto-provisioned).
    forward_from_number: str | None = None


class WhatsAppLinkRequest(BaseModel):
    # The tenant's WhatsApp Business (WABA) sender number, already onboarded in
    # Plivo/Meta. Stored as provider_status.plivo.whatsapp_number; the brochure +
    # location confirmation templates send from it.
    whatsapp_number: str


class WhatsAppSetupRequest(BaseModel):
    # Concierge onboarding: the client requests WhatsApp setup and never sees
    # Plivo/Meta. The operator fulfils it (Plivo Console) and records the sender.
    business_name: str
    contact_number: str            # the number they want as their WhatsApp sender
    display_name: str | None = None


class LeadOauthStartRequest(BaseModel):
    provider: str = Field(pattern="^meta_ads$")
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


async def _phone_link_summary(request: Request, tr: TenantResources) -> dict[str, Any]:
    """Call-forwarding view: the auto-provisioned Plivo DID the tenant forwards their
    own number to, the number's status, the (auto-configured) webhook URLs, and a
    webhook_health block answering "is Plivo actually reaching us"."""
    from app.services.plivo_service import PlivoService
    from app.services.public_url import public_base_url, ws_base_url

    summary = PlivoService.phone_link_response(tr)
    link_id = summary.get("link_id")
    base_http = public_base_url(request)
    base_ws = ws_base_url(request)
    if link_id:
        summary["inbound_webhook_url"] = f"{base_http}/api/nokvo-one/agents/plivo/voice/{link_id}"
        summary["inbound_media_url"] = f"{base_ws}/api/nokvo-one/agents/plivo/media/{link_id}"
        # Webhook health: stored vs expected answer_url (catches a stale Plivo
        # Application after a base-URL change) + the latest webhook/stream
        # breadcrumbs recorded by the handlers.
        plivo_cfg = dict((tr.provider_status or {}).get("plivo") or {})
        stored = plivo_cfg.get("answer_url")
        expected = PlivoService.expected_answer_url(str(link_id), base_http)
        summary["webhook_health"] = {
            "answer_url_stored": stored,
            "answer_url_expected": expected,
            "in_sync": bool(stored) and stored == expected,
            "last_inbound_webhook": await _read_webhook_event(str(link_id), "voice"),
            "last_stream_event": await _read_webhook_event(str(link_id), "stream"),
            "last_stream_error": await _read_webhook_event(str(link_id), "stream_error"),
        }
    did = summary.get("plivo_number")
    if did:
        did_intl = did if str(did).startswith("+") else f"+{did}"
        summary["forwarding_instructions"] = (
            f"Forward your business number to {did_intl} so incoming calls reach your AI agent."
        )
        # Standard GSM call-forwarding codes. Use the international (+) format —
        # carriers often reject the bare number with 'Setting Registration Failed'.
        summary["forwarding_steps"] = [
            "On iPhone, prefer: Settings → Phone → Call Forwarding → turn ON → enter the number below.",
            f"Or dial to forward ALL calls: **21*{did_intl}#  then press call.",
            f"Or forward only when busy / unanswered / off: **004*{did_intl}#  then press call.",
            "Turn it off any time: ##21# (all) or ##004# (conditional), then press call.",
            "If you see 'Setting Registration Failed': call-forwarding isn't enabled on your SIM — ask your carrier to activate it (often off by default on prepaid/Jio).",
            "Test: call your business number from another phone — the agent should answer.",
        ]
    else:
        summary["forwarding_instructions"] = (
            "Your Plivo number is being provisioned (carrier verification) — a number to forward to will appear here shortly."
        )
        summary["forwarding_steps"] = []
    return summary


@router.get("/phone-link")
async def get_phone_link(
    request: Request,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    return await _phone_link_summary(request, tr)


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
    plivo_cfg = dict(provider_status.get("plivo") or {})
    plivo_cfg["forward_from_number"] = (payload.forward_from_number or "").strip() or None
    provider_status["plivo"] = plivo_cfg
    tr.provider_status = provider_status
    flag_modified(tr, "provider_status")
    db.add(tr)
    await db.commit()
    await db.refresh(tr)
    return await _phone_link_summary(request, tr)


# ────────────────────────── WhatsApp sender connect ──────────────────────────
# Lets a tenant connect the WhatsApp Business number they've onboarded in
# Plivo/Meta so the brochure + location confirmation messages send from it.
# The send path (WhatsAppService) already resolves this number; this is the
# missing "connect" step (the WhatsApp analog of /phone-link for voice).


@router.get("/whatsapp-link")
async def get_whatsapp_link(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    from app.services.plivo_service import PlivoService

    tr = await _tenant_for_user(db, user)
    return PlivoService.whatsapp_link_response(tr)


@router.post("/whatsapp-link/request")
async def request_whatsapp_link_setup(
    payload: WhatsAppSetupRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Concierge onboarding entry point — the client requests WhatsApp setup.
    Stores the request + alerts ops; the operator fulfils it out-of-band. The
    client never sees Plivo."""
    from app.services.plivo_service import PlivoError, PlivoService

    tr = await _tenant_for_user(db, user)
    try:
        return await PlivoService.request_whatsapp_setup(
            tr, db,
            business_name=payload.business_name,
            contact_number=payload.contact_number,
            display_name=payload.display_name,
            requested_by=getattr(user, "email", None),
        )
    except PlivoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/whatsapp-link")
async def connect_whatsapp_link(
    payload: WhatsAppLinkRequest,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    from app.services.plivo_service import PlivoError, PlivoService

    tr = await _tenant_for_user(db, user)
    try:
        return await PlivoService.connect_whatsapp_number(
            tr, db, whatsapp_number=payload.whatsapp_number
        )
    except PlivoError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/whatsapp-link")
async def disconnect_whatsapp_link(
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    from app.services.plivo_service import PlivoService

    tr = await _tenant_for_user(db, user)
    return await PlivoService.disconnect_whatsapp_number(tr, db)


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
    counts = await OutgoingLeadService.lead_counts_by_form(tr, db)
    responses = []
    for form in forms:
        data = _form_response(form, request)
        form_counts = counts.get(str(form.id)) or {"total": 0, "callable": 0}
        data["lead_count"] = form_counts["total"]
        data["callable_lead_count"] = form_counts["callable"]
        responses.append(data)
    return responses


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
    capture_form_id: uuid.UUID | None = None,
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    leads = await OutgoingLeadService.list_leads(
        tr, db, eligible_only=eligible_only, limit=limit, capture_form_id=capture_form_id
    )
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


# ────────────────────────── Plivo inbound ──────────────────────────


def _plivo_stream_xml(media_url: str, status_url: str | None = None) -> str:
    """Plivo answer XML: bidirectional <Stream> bridging the caller's audio to our
    media WS (L16 @ the configured rate). Playback flows back via `playAudio`.
    `statusCallbackUrl` makes Plivo POST stream lifecycle/errors so we can diagnose."""
    rate = settings.PLIVO_DEFAULT_SAMPLE_RATE or 8000
    status_attr = (
        f' statusCallbackUrl="{status_url}" statusCallbackMethod="POST"' if status_url else ""
    )
    # No <?xml?> declaration — matches Plivo's documented example exactly.
    return (
        "<Response>"
        f'<Stream bidirectional="true" keepCallAlive="true" audioTrack="inbound" '
        f'contentType="audio/x-l16;rate={rate}"{status_attr}>{media_url}</Stream>'
        "</Response>"
    )


def _plivo_busy_xml() -> str:
    """Answer XML for an at-capacity tenant: a short spoken notice, then hang up.
    No <Stream> is opened, so the caller never reaches dead air."""
    return (
        "<Response>"
        "<Speak>All our lines are busy at the moment. "
        "Please call back in a few minutes.</Speak>"
        "<Hangup/>"
        "</Response>"
    )


# ── Plivo webhook signature validation (X-Plivo-Signature-V2) ──────────
# V2 scheme: base64(HMAC-SHA256(auth_token, full_url + nonce)). The URL Plivo
# signed is the PUBLIC one it called — reconstruct it via public_url, never
# request.url (which is the internal host behind a proxy). Mode is
# PLIVO_VALIDATE_SIGNATURES: off | warn | enforce (default warn so the first
# real call confirms whether the master or subaccount token signs).


def _verify_plivo_signature(url: str, nonce: str, signature: str, tokens: list[str]) -> str | None:
    """Return the index-label of the token that validates ('master'/'tenant'),
    or None when nothing matches. Constant-time comparison per candidate."""
    if not nonce or not signature:
        return None
    labels = ("master", "tenant")
    for i, token in enumerate(tokens):
        if not token:
            continue
        expected = base64.b64encode(
            hmac.new(token.encode("utf-8"), (url + nonce).encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        # Plivo may send several comma-separated signatures during token rotation.
        for candidate in signature.split(","):
            if hmac.compare_digest(expected, candidate.strip()):
                return labels[i] if i < len(labels) else str(i)
    return None


async def _check_plivo_signature(request: Request, *, tenant_token: str | None = None) -> bool:
    """Validate per PLIVO_VALIDATE_SIGNATURES. Returns True when the request
    may proceed (valid, warn-mode mismatch, or validation disabled)."""
    mode = (settings.PLIVO_VALIDATE_SIGNATURES or "off").strip().lower()
    if mode not in ("warn", "enforce") or not settings.PLIVO_AUTH_TOKEN:
        return True
    from app.services.public_url import public_base_url

    signature = request.headers.get("x-plivo-signature-v2") or ""
    nonce = request.headers.get("x-plivo-signature-v2-nonce") or ""
    path_qs = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    public_url = f"{public_base_url(request)}{path_qs}"
    matched = _verify_plivo_signature(
        public_url, nonce, signature, [settings.PLIVO_AUTH_TOKEN, tenant_token or ""]
    )
    if matched:
        logger.debug("PLIVO-SIG ok (%s token) %s", matched, request.url.path)
        return True
    # Log the COMPUTED signing URL we validated against — a mismatch here is almost
    # always a public_base_url / PLIVO_WEBHOOK_BASE_URL contract error (the signed
    # URL Plivo used differs from what we reconstruct). With this, "enforce broke
    # our calls" is diagnosable + one-env-var fixable (or set the mode to warn).
    logger.warning(
        "PLIVO-SIG %s: signature mismatch path=%s nonce=%s sig_present=%s computed_url=%s request_url=%s",
        mode, request.url.path, bool(nonce), bool(signature), public_url, str(request.url),
    )
    return mode != "enforce"


async def _outbound_signing_token(call_link_id: str, db: AsyncSession) -> str | None:
    """The auth token Plivo signed an outbound webhook with — the call was NOT
    placed on the master account, so master-only validation always mismatches.
    Bulk campaigns sign with the dedicated bulk token; everything else with the
    tenant's subaccount token. Returned as the extra ``_check_plivo_signature``
    candidate so outbound answer/status webhooks validate under enforce mode."""
    from app.services.plivo_service import PlivoService

    try:
        campaign, _contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
        if not campaign:
            return None
        tr = await _tenant_by_tenant_id(db, campaign.tenant_id)
        if not tr:
            return None
        if bool((campaign.agent_config or {}).get("bulk_csv")):
            auth = PlivoService.bulk_calling_auth(tr)
            return auth[1] if auth else None
        cfg = dict((tr.provider_status or {}).get("plivo") or {})
        return PlivoService._subaccount_token(cfg)
    except Exception:
        return None


# Retain fire-and-forget tasks — the event loop holds only weak refs, so an
# untracked create_task can be GC'd mid-flight (same pattern as the campaign
# service's _ingest_tasks and the stream service's _background_tasks).
_bg_tasks: set[asyncio.Task] = set()


def _retain_task(task: asyncio.Task) -> None:
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


# ── Webhook observability ──────────────────────────────────────────────
# Best-effort Redis breadcrumbs (TTL 7 days) so /phone-link can answer the
# two diagnostic questions for "inbound calls don't connect": did Plivo's
# voice webhook reach us, and did the media <Stream> then connect or error.
_WEBHOOK_EVENT_TTL_S = 7 * 24 * 3600


def _webhook_event_key(link_id: str, kind: str) -> str:
    return f"plivo:wh:{link_id}:{kind}"


def _record_webhook_event(link_id: str, kind: str, data: dict) -> None:
    """Fire-and-forget: never blocks or fails the webhook response."""
    import asyncio as _asyncio
    from datetime import datetime as _dt, timezone as _tz

    async def _write() -> None:
        try:
            from app.services.agent_session_store import AgentSessionStore

            client = AgentSessionStore.client()
            payload = {"ts": _dt.now(_tz.utc).isoformat(), **data}
            await client.set(
                _webhook_event_key(link_id, kind), json.dumps(payload)[:2000],
                ex=_WEBHOOK_EVENT_TTL_S,
            )
        except Exception:
            logger.debug("PLIVO webhook event record failed", exc_info=True)

    try:
        _retain_task(_asyncio.get_running_loop().create_task(_write()))
    except RuntimeError:
        pass


async def _read_webhook_event(link_id: str, kind: str) -> dict | None:
    try:
        from app.services.agent_session_store import AgentSessionStore

        raw = await AgentSessionStore.client().get(_webhook_event_key(link_id, kind))
        return json.loads(raw) if raw else None
    except Exception:
        return None


@router.post("/plivo/voice/{link_id}", response_class=PlainTextResponse)
async def plivo_inbound_webhook(
    link_id: str, request: Request, db: AsyncSession = Depends(deps.get_db)
):
    tr = await _tenant_by_link_id(db, link_id)
    if not tr:
        return PlainTextResponse(
            "<Response><Hangup/></Response>", media_type="application/xml", status_code=404
        )
    _tenant_token = None
    try:
        from app.services.plivo_service import PlivoService as _PS

        _tenant_token = _PS._subaccount_token(_PS._plivo_config(tr))
    except Exception:
        _tenant_token = None
    if not await _check_plivo_signature(request, tenant_token=_tenant_token):
        return PlainTextResponse(
            "<Response><Hangup/></Response>", media_type="application/xml", status_code=403
        )
    # The caller's number (ANI) — Plivo posts it as `From`. We pass it to the media
    # WS so the agent can auto-fill the phone slot instead of asking the caller to
    # recite digits (which telephony STT mangles).
    from urllib.parse import quote
    try:
        form = dict(await request.form())
    except Exception:
        form = {}
    from_number = str(form.get("From") or form.get("from") or "").strip()

    # Prepaid-minute gate: don't answer when the org is out of minutes. Both plans
    # answer inbound, so the balance is the ONLY inbound gate. An in-progress call
    # is unaffected (this fires before the call is bridged).
    from app.services.minute_balance_service import has_balance as _has_balance

    if not await _has_balance(db, tr.organization_id):
        logger.info(
            "PLIVO-INBOUND empty prepaid balance tenant=%s link_id=%s → hangup",
            tr.tenant_id, link_id,
        )
        _record_webhook_event(link_id, "voice", {"from": from_number, "no_balance": True})
        return PlainTextResponse(
            "<Response><Hangup/></Response>", media_type="application/xml"
        )

    # Per-tenant concurrency cap: reserve a slot BEFORE bridging audio. At the
    # limit, play a short busy notice and never open a media stream — the caller
    # hears a message instead of dead air. The token is handed to the media WS
    # via the URL and released when the call ends.
    from app.services.call_concurrency import acquire as _acquire_call_slot, POOL_INBOUND_FOLLOWUP

    call_token = await _acquire_call_slot(tr.tenant_id, pool=POOL_INBOUND_FOLLOWUP)
    if call_token is None:
        logger.info("PLIVO-INBOUND at-capacity tenant=%s link_id=%s → busy", tr.tenant_id, link_id)
        _record_webhook_event(link_id, "voice", {"from": from_number, "busy": True})
        return PlainTextResponse(_plivo_busy_xml(), media_type="text/xml")

    # URLs must be PUBLIC — behind a TLS-terminating proxy request.url says
    # http://internal-host, which yields ws:// media URLs Plivo can't open
    # (call answers → dead air). public_url prefers PLIVO_WEBHOOK_BASE_URL.
    from urllib.parse import urlencode
    from app.services.public_url import public_base_url, ws_base_url

    media_url = f"{ws_base_url(request)}/api/nokvo-one/agents/plivo/media/{link_id}"
    _media_q: dict[str, str] = {}
    if from_number:
        _media_q["caller"] = from_number
    if call_token:
        _media_q["ct"] = call_token
    if _media_q:
        media_url = f"{media_url}?{urlencode(_media_q)}"
    status_url = f"{public_base_url(request)}/api/nokvo-one/agents/plivo/stream-status/{link_id}"
    xml = _plivo_stream_xml(media_url, status_url)
    logger.info("PLIVO-INBOUND voice webhook link_id=%s from=%s xml=%s", link_id, from_number, xml)
    _record_webhook_event(link_id, "voice", {"from": from_number})
    return PlainTextResponse(xml, media_type="text/xml")


@router.post("/plivo/stream-status/{link_id}")
async def plivo_stream_status(link_id: str, request: Request):
    """Plivo posts <Stream> lifecycle events (connected / error / disconnected) here.

    A stream error here is THE diagnostic for "call answers then dead air" —
    log it loudly and persist it so /phone-link's webhook_health surfaces it."""
    if not await _check_plivo_signature(request):
        raise HTTPException(status_code=403, detail="Plivo signature verification failed")
    try:
        payload = dict(await request.form())
    except Exception:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    event = str(
        payload.get("Event") or payload.get("event") or payload.get("Status") or payload.get("status") or ""
    ).lower()
    is_error = any(token in event for token in ("error", "fail")) or bool(
        payload.get("ErrorCode") or payload.get("error")
    )
    if is_error:
        logger.error("PLIVO-STREAM-STATUS error link_id=%s payload=%s", link_id, payload)
        _record_webhook_event(link_id, "stream_error", {"event": event, "payload": str(payload)[:800]})
    else:
        logger.info("PLIVO-STREAM-STATUS link_id=%s payload=%s", link_id, payload)
    _record_webhook_event(link_id, "stream", {"event": event or "unknown"})
    return {"ok": True}


@router.websocket("/plivo/media/{link_id}")
async def plivo_inbound_media_websocket(websocket: WebSocket, link_id: str):
    caller_phone = websocket.query_params.get("caller") or None
    call_token = websocket.query_params.get("ct") or None
    print(f"PLIVO-MEDIA ws connect link_id={link_id} caller={caller_phone}", flush=True)
    async for db in deps.get_db():
        tr = await _tenant_by_link_id(db, link_id)
        if not tr:
            print(f"PLIVO-MEDIA no tenant for link_id={link_id} → closing", flush=True)
            await websocket.close(code=1008)
            return
        try:
            await PlivoBridgeService.run_session(websocket, tr, db=db, caller_phone=caller_phone)
        finally:
            # Release the concurrency slot the answer webhook reserved (inbound pool).
            if call_token:
                from app.services.call_concurrency import release as _release_call_slot, POOL_INBOUND_FOLLOWUP
                await _release_call_slot(tr.tenant_id, call_token, pool=POOL_INBOUND_FOLLOWUP)
        return


# ────────────────────────── Plivo outbound ──────────────────────────


# Sentinel distinguishing "at capacity, decline this call" from "proceed" — both
# of which would otherwise be a bare None.
_SLOT_DENIED = object()


async def _abandon_and_requeue(call_link_id: str, campaign_id, db: AsyncSession) -> None:
    """Put a connected-but-unservable contact back in the dial queue and let the
    pacer see it. Best-effort; never raises into a call path."""
    try:
        from app.services import campaign_contacts_v2 as v2

        if await v2.requeue_abandoned(db, call_link_id) is not None:
            await db.commit()
            from app.services.outbound_pacer import invalidate as _pacer_invalidate

            await _pacer_invalidate(campaign_id)
    except Exception:
        logger.exception("PLIVO-OUTBOUND: requeue after abandon failed for %s", call_link_id)
        with contextlib.suppress(Exception):
            await db.rollback()


async def _outbound_capacity_available(call_link_id: str, db: AsyncSession):
    """Is there room for one more conversation on this campaign's tenant?

    Returns ``None`` to proceed (room available, or this call isn't slot-managed)
    or :data:`_SLOT_DENIED` when the org is already at its plan concurrency — the
    caller then hangs up before any audio and requeues the contact.

    This deliberately CHECKS rather than reserves. The slot is acquired on the
    media WebSocket, which is also what releases it in its ``finally``; splitting
    acquire and release across the two would leak a slot for the full stale-token
    window whenever Plivo never opens the stream — and on a Core plan (one slot)
    a single leak wedges the whole campaign. The check closes the structural hole
    (the dialer used to place five calls into a one-slot plan) while the paired
    acquire/release on the WS stays the authority and catches the rare simultaneous
    -answer race.

    Fail-OPEN on any error: a lookup blip must never drop a live call."""
    try:
        campaign, contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
        if not contact or campaign is None or contact.get("is_followup"):
            return None
        tr = await _tenant_by_tenant_id(db, campaign.tenant_id)
        if tr is None:
            return None

        from app.services.apex_plans import get_apex_concurrency as _apex_conc
        from app.services.call_concurrency import active_count as _active, POOL_OUTBOUND

        org = await db.get(Organization, tr.organization_id)
        if org is None or (org.product_tier or "") != "nokvo_apex":
            return None  # non-APEX keeps the global cap, enforced on the WS
        limit = _apex_conc(org)
        if await _active(tr.tenant_id, pool=POOL_OUTBOUND) < limit:
            return None

        # At capacity. With the dialer's 1:1 ring-ahead this should not happen; when
        # it does it is the pacer over-reaching, so make it loud and feed it back as
        # abandon-rate signal rather than silently losing the contact.
        logger.warning(
            "PLIVO-OUTBOUND at-capacity on answer tenant=%s limit=%s call_link_id=%s "
            "→ hangup + requeue",
            tr.tenant_id, limit, call_link_id,
        )
        await _abandon_and_requeue(call_link_id, campaign.id, db)
        return _SLOT_DENIED
    except Exception:
        logger.exception("PLIVO-OUTBOUND: capacity check failed for %s — proceeding", call_link_id)
        return None


@router.post("/plivo/outbound-answer/{call_link_id}", response_class=PlainTextResponse)
async def plivo_outbound_answer(
    call_link_id: str, request: Request, db: AsyncSession = Depends(deps.get_db)
):
    """Plivo fetches this when the outbound call connects → returns the <Stream> XML
    that bridges audio to the agent's outbound media WS.

    This is the FIRST moment we know a human picked up, and the last moment we can
    decline without them hearing anything. So capacity is checked HERE: at the plan's
    concurrency the ring is cut before a single audio frame and the contact goes back
    in the dial queue. Previously the only check was on the media WebSocket, which
    closed the socket after the callee had already said "hello" — dead air, then a
    hangup, filed as a missed call."""
    from app.services.public_url import ws_base_url

    _sig_token = await _outbound_signing_token(call_link_id, db)
    if not await _check_plivo_signature(request, tenant_token=_sig_token):
        return PlainTextResponse(
            "<Response><Hangup/></Response>", media_type="application/xml", status_code=403
        )

    if await _outbound_capacity_available(call_link_id, db) is _SLOT_DENIED:
        # A cut ring reads to the callee as a dropped call rather than dead air.
        return PlainTextResponse(
            "<Response><Hangup/></Response>", media_type="application/xml"
        )

    media_url = f"{ws_base_url(request)}/api/nokvo-one/agents/plivo/outbound-media/{call_link_id}"
    return PlainTextResponse(_plivo_stream_xml(media_url), media_type="application/xml")


@router.post("/plivo/outbound-status/{call_link_id}")
async def plivo_outbound_status(
    call_link_id: str, request: Request, db: AsyncSession = Depends(deps.get_db)
):
    if not await _check_plivo_signature(
        request, tenant_token=await _outbound_signing_token(call_link_id, db)
    ):
        raise HTTPException(status_code=403, detail="Plivo signature verification failed")
    # Parse the payload FIRST (with a hard {} fallback — a malformed body must
    # never NameError) so it can be stashed even when the row isn't found yet.
    try:
        payload = dict(await request.form())
    except Exception:
        try:
            payload = await request.json()
        except Exception:
            payload = {}
    campaign, contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
    is_followup = bool(contact and contact.get("is_followup"))
    if not campaign and not is_followup:
        # The webhook can race the call-placement commit (Plivo posts status
        # before mark_in_flight lands). One short retry, then stash the event
        # for the reconciliation sweep and return 200 — a non-200 here means
        # the event is lost forever.
        await asyncio.sleep(2.0)
        campaign, contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
        is_followup = bool(contact and contact.get("is_followup"))
        if not campaign and not is_followup:
            try:
                from app.services.agent_session_store import AgentSessionStore

                await AgentSessionStore.client().set(
                    f"plivo:orphan-status:{call_link_id}",
                    json.dumps(payload)[:4000],
                    ex=900,
                )
            except Exception:
                logger.debug("PLIVO orphan-status stash failed", exc_info=True)
            logger.warning(
                "PLIVO-OUTBOUND-STATUS: no campaign/followup for call_link_id=%s — deferred",
                call_link_id,
            )
            return {"ok": True, "deferred": True, "call_link_id": call_link_id}
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
    # A call just ended → refill the throttled batch (dial the next pending lead).
    # Best-effort: handle_call_status already committed; never fail the webhook.
    if campaign is not None and not is_followup and normalized == "call.hangup":
        from app.services.public_url import public_base_url as _pub

        await OutboundCampaignService.dial_next_pending(
            campaign,
            db,
            public_base_url=_pub(request),
            path_prefix="/api/nokvo-one/agents",
        )
    return {"ok": True, "call_link_id": call_link_id}


@router.websocket("/plivo/outbound-media/{call_link_id}")
async def plivo_outbound_media_websocket(websocket: WebSocket, call_link_id: str):
    async for db in deps.get_db():
        campaign, contact = await OutboundCampaignService.get_by_call_link_id(call_link_id, db)
        is_followup = bool(contact and contact.get("is_followup"))
        if not contact:
            await websocket.close(code=1008)
            return
        # Defensive: a LEAD follow-up must run as its campaign's agent (project
        # knowledge + site_visit objective). The scheduler already skips orphan
        # leads (no campaign), so reaching here without one means a knowledge-less
        # call — close politely rather than run a generic agent. Clinic customer
        # follow-ups (customer_id, no lead_id) are exempt — they build their own
        # context downstream.
        if is_followup and contact.get("lead_id") and campaign is None:
            logger.info(
                "PLIVO-OUTBOUND: lead follow-up with no campaign (call_link_id=%s) — closing",
                call_link_id,
            )
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
        # Customer-targeted follow-up (clinic manual path): the synthetic
        # contact carries tenant_id directly; fall back to the customer row.
        if tenant_id is None and contact.get("tenant_id"):
            tenant_id = str(contact["tenant_id"])
        if tenant_id is None and contact.get("customer_id"):
            from app.models.customer_base import CustomerBase as _CB
            try:
                _cust = await db.get(_CB, uuid.UUID(str(contact["customer_id"])))
            except (TypeError, ValueError):
                _cust = None
            tenant_id = _cust.tenant_id if _cust else None
        if not tenant_id:
            await websocket.close(code=1008)
            return
        tr = await _tenant_by_tenant_id(db, tenant_id)
        if not tr:
            await websocket.close(code=1008)
            return
        # Concurrency pool: a follow-up call draws from the shared INBOUND pool
        # (so disabling follow-up frees that pool for inbound callers); a campaign
        # call draws from the dedicated OUTBOUND pool.
        from app.services.call_concurrency import (
            acquire as _acquire_call_slot,
            POOL_OUTBOUND,
            POOL_INBOUND_FOLLOWUP,
        )
        _out_pool = POOL_INBOUND_FOLLOWUP if is_followup else POOL_OUTBOUND
        # Per-plan outbound concurrency for NOKVO APEX (Core=1, Growth=2, Pinnacle=4,
        # Enterprise per-deal). Non-APEX products keep the global cap (limit=None).
        # This acquire is the authority — and it is PAIRED with the release in the
        # finally below, which is why the reservation was not moved to the answer
        # webhook: a split acquire/release leaks the slot for the whole stale-token
        # window whenever Plivo never opens the stream. The answer webhook does a
        # cheap capacity CHECK instead, so reaching the cap here now means a genuine
        # simultaneous-answer race rather than the dialer structurally over-placing.
        _out_limit = None
        if _out_pool == POOL_OUTBOUND:
            try:
                from app.services.apex_plans import get_apex_concurrency as _apex_conc

                _org = await db.get(Organization, tr.organization_id)
                if _org is not None and (_org.product_tier or "") == "nokvo_apex":
                    _out_limit = _apex_conc(_org)
            except Exception:
                logger.warning("APEX per-plan concurrency lookup failed; using global cap", exc_info=True)
        _out_call_token = await _acquire_call_slot(tr.tenant_id, pool=_out_pool, limit=_out_limit)
        if _out_call_token is None:
            logger.info(
                "PLIVO-OUTBOUND at-capacity tenant=%s call_link_id=%s → hangup + requeue",
                tr.tenant_id, call_link_id,
            )
            # The callee already picked up, so this contact must go back in the
            # queue rather than be filed as a miss they never answered.
            if campaign is not None and not is_followup:
                await _abandon_and_requeue(call_link_id, campaign.id, db)
            await websocket.close(code=1013)
            return
        # Outbound campaigns can be authored in any supported language —
        # pull it off the persisted agent_config (default "en" for legacy
        # campaigns). Follow-ups inherit the parent campaign's language
        # when one is attached.
        campaign_language = str(
            ((campaign.agent_config or {}).get("language") if campaign else "en")
            or "en"
        ).strip().lower() or "en"
        adapter = PlivoWebSocketAdapter(websocket, language=campaign_language)

        # For follow-up calls, fetch the lead's prior handoff_note so the
        # preamble can quote it verbatim. One DB read; follow-ups are
        # low-frequency so caching isn't worth it.
        #
        # Outbound campaign leads live in OutgoingLead (dedicated column);
        # inbound leads & site-visits live in nokvo_one_tool_records (note in
        # the data JSONB). Check OutgoingLead first, then fall back to the
        # tool_record, so a follow-up to either source reads its note back.
        followup_handoff_note: str | None = None
        if is_followup and contact.get("lead_id"):
            try:
                _lead_uuid = uuid.UUID(str(contact["lead_id"]))
            except (TypeError, ValueError):
                _lead_uuid = None
            if _lead_uuid is not None:
                from app.models.outgoing_lead import OutgoingLead as _OL

                ho_res = await db.execute(select(_OL).where(_OL.id == _lead_uuid))
                ho_lead = ho_res.scalars().first()
                if ho_lead is not None and ho_lead.handoff_note:
                    followup_handoff_note = str(ho_lead.handoff_note).strip() or None
                if not followup_handoff_note:
                    from app.models.nokvo_one_tool_record import NokvoOneToolRecord as _TR

                    # NEVER name this ``tr`` — that's the TenantResources bound
                    # above, still used by run_session and the slot release below;
                    # shadowing it crashed every follow-up without a handoff note.
                    _note_rec = await db.get(_TR, _lead_uuid)
                    if _note_rec is not None:
                        _tr_note = (_note_rec.data or {}).get("handoff_note")
                        if _tr_note:
                            followup_handoff_note = str(_tr_note).strip() or None
        elif is_followup and contact.get("customer_id"):
            # Customer follow-up: the prior-call summary lives on the
            # CustomerBase row (written by the condenser at every call end).
            from app.models.customer_base import CustomerBase as _CB

            try:
                _cust_uuid = uuid.UUID(str(contact["customer_id"]))
            except (TypeError, ValueError):
                _cust_uuid = None
            if _cust_uuid is not None:
                _cust_row = await db.get(_CB, _cust_uuid)
                if _cust_row is not None and _cust_row.last_call_summary:
                    followup_handoff_note = str(_cust_row.last_call_summary).strip() or None

        campaign_context = {
            "campaign_id": str(campaign.id) if campaign else None,
            "goal": campaign.name if campaign else "Follow-up call",
            "contact": contact,
            "language": campaign_language,
            "is_followup": is_followup,
            "attempt_n": int(contact.get("_attempt_n") or 0) + 1 if is_followup else None,
            "source_call_id": contact.get("_source_call_id") if is_followup else None,
            "handoff_note": followup_handoff_note,
            # Admin's typed purpose for a manual customer follow-up — rendered
            # as the REASON FOR THIS CALL block in the outbound prompt.
            "admin_note": (contact.get("_admin_note") or "").strip() or None,
            "customer_id": contact.get("customer_id"),
            # Deliberately NO ``opening_message``: the outbound opener is generated
            # deterministically by ``generate_outbound_opener_text`` in the stream
            # service (personalised, language-aware, prosody-tagged). ``_play_opener``
            # speaks ``opening_message`` VERBATIM, so passing a kickoff INSTRUCTION
            # here made the agent read it aloud ("Start the outbound campaign call
            # for …, introduce yourself …"). Only ever set ``opening_message`` to
            # real, ready-to-speak opener text — never an instruction.
        }
        # The media WS connecting IS the answer signal — Plivo's status webhook is
        # only wired as the hangup_url, so a 'call.answered' event never arrives and
        # the contact would otherwise hang up as status="failed". Stamp it answered
        # now (campaign calls only; follow-ups track answered via OutgoingLead).
        # Idempotent + isolated; best-effort, never blocks the call on a DB hiccup.
        if campaign is not None and not is_followup:
            try:
                await OutboundCampaignService.mark_answered(campaign.id, call_link_id)
            except Exception:
                logger.exception("PLIVO-OUTBOUND: mark_answered failed (continuing)")
        try:
            await NokvoOneVoiceStreamService.run_session(
                adapter,
                tr,
                db=db,
                language=campaign_language,
                call_id=call_link_id,
                campaign_context=campaign_context,
            )
        finally:
            adapter.close_audio()
            # Release the per-tenant concurrency slot (same pool we acquired).
            from app.services.call_concurrency import release as _release_call_slot
            await _release_call_slot(tr.tenant_id, _out_call_token, pool=_out_pool)
        return


# ────────────────────────── Campaigns ──────────────────────────


def _questionnaire_max_score(agent_config: dict[str, Any]) -> int | None:
    """Max achievable lead score for a campaign's questionnaire (weighted/graded
    points via the ONE shared helper), or ``None`` when there's no questionnaire."""
    questions = ((agent_config or {}).get("questionnaire") or {}).get("questions") or []
    if not questions:
        return None
    from app.services.agent_outbound_context import questionnaire_max_points

    return questionnaire_max_points(questions) or None


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
        # Per-row (V2) storage vs the legacy inline blob. When true the UI reads
        # counts from /summary and rows from the paginated /contacts endpoint
        # instead of the (now-empty) inline ``contacts`` array. ``not contacts``
        # (not ``is None``): an EMPTY blob is V2 residue, not a legacy campaign —
        # flagging it legacy made the UI read the empty array and show nothing.
        "v2": not c.contacts,
        "agent_config": c.agent_config or {},
        # Numbers removed from the dial list because they're on the DND/NCPR
        # register (bulk CSV campaigns only). Empty when nothing was scrubbed.
        "dnd_dropped": (c.agent_config or {}).get("dnd_dropped") or [],
        # Lead-capture questionnaire (bulk campaigns) + its max score, so the UI
        # can render the builder + switch Qualified Leads to score-based. Null
        # when the campaign has none (UI falls back to the interest filter). The
        # per-contact lead_score/qualified/score_breakdown ride in "contacts".
        "questionnaire": (c.agent_config or {}).get("questionnaire"),
        # Max achievable lead score — weighted/graded points, not just the
        # question count. Shared helper so the denominator never drifts from the
        # scorer. Null when the campaign has no questionnaire.
        "max_score": _questionnaire_max_score(c.agent_config or {}),
        # Campaign type: deterministic (questionnaire + lead score) vs
        # non-deterministic (free-form pitch). Defaults to the questionnaire
        # presence for rows created before the explicit flag existed.
        "deterministic": bool(
            (c.agent_config or {}).get(
                "deterministic", bool((c.agent_config or {}).get("questionnaire"))
            )
        ),
        # Whether the follow-up agent is on for this campaign (defaults on).
        "followup_enabled": bool(
            ((c.agent_config or {}).get("followup_rules") or {}).get("enabled", True)
        ),
        # Undialed contacts that a relaunch would call (no call_id, not ended).
        "pending_to_dial": sum(
            1 for ct in (c.contacts or []) if not ct.get("call_id") and not ct.get("ended")
        ),
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


def _parse_questionnaire_field(raw: str | None) -> dict | None:
    """Parse the bulk-campaign questionnaire multipart field (a JSON string) into
    the ONE canonical normalized shape, or ``None`` when absent.

    Raises :class:`ValueError` (mapped to a 400 via ``_safe_detail`` at the call
    site) on malformed JSON or an ``answer`` question missing its expected
    answer, so the admin sees a clear message in the same channel as other
    campaign validation errors. Delegates shape rules to the single normalizer.
    """
    text = (raw or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(
            "The questionnaire couldn't be read — please re-add the questions and try again."
        ) from exc
    from app.services.agent_outbound_context import _coerce_questionnaire

    return _coerce_questionnaire(data, strict=True)


@router.get("/outbound-number")
async def outbound_number(
    user: OrganizationUser = Depends(_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """The org's allotted outbound caller-ID number + whether outbound is on its
    plan, so the campaign UI can show "calling from <number>" read-only and gate
    the launch/create-and-call actions."""
    from app.services.plivo_service import PlivoService

    tr = await _tenant_for_user(db, user)
    org = await _org_for_user(db, user)
    number = PlivoService.outbound_caller_id(tr)
    plivo_cfg = dict((tr.provider_status or {}).get("plivo") or {})
    return {
        "number": number,
        "status": plivo_cfg.get("number_status") or ("active" if number else "not_provisioned"),
        "calling_enabled": bool(org.calling_enabled),
    }


# ───────────────────────── Bulk CSV calling (add-on) ─────────────────────────
# Only offered on the Inbound + Outbound plan, and not self-serve: a tenant
# requests access (leaving a contact number) and an operator grants it from the
# SuperAdmin console by provisioning a dedicated Plivo number. See
# app/models/bulk_calling_request.py.


async def _latest_bulk_request(db: AsyncSession, organization_id):
    from app.models.bulk_calling_request import BulkCallingRequest

    res = await db.execute(
        select(BulkCallingRequest)
        .where(BulkCallingRequest.organization_id == organization_id)
        .order_by(BulkCallingRequest.created_at.desc())
        .limit(1)
    )
    return res.scalars().first()


@router.get("/bulk-calling/status")
async def bulk_calling_status(
    user: OrganizationUser = Depends(_bulk_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Gate state for the Bulk CSV Calling card:
      * ``plan_eligible``  — on the Inbound + Outbound plan (can request).
      * ``enabled``        — operator has provisioned the dedicated number.
      * ``request_status`` — pending | approved | denied | null (latest request).
    """
    from app.services.plivo_service import PlivoService

    tr = await _tenant_for_user(db, user)
    org = await _org_for_user(db, user)
    latest = await _latest_bulk_request(db, org.id)
    return {
        "plan_eligible": bool(org.calling_enabled),
        "enabled": PlivoService.bulk_calling_enabled(tr),
        "request_status": latest.status if latest else None,
        "contact_number": latest.contact_number if latest else None,
        "requested_at": latest.created_at.isoformat() if (latest and latest.created_at) else None,
    }


class BulkCallingRequestPayload(BaseModel):
    contact_number: str = Field(min_length=4, max_length=32)
    note: str | None = Field(default=None, max_length=2000)


@router.post("/bulk-calling/request", status_code=status.HTTP_201_CREATED)
async def request_bulk_calling(
    payload: BulkCallingRequestPayload,
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """An admin requests Bulk CSV Calling access. Gated to the Inbound + Outbound
    plan; rejected if already enabled or a request is already pending."""
    from app.models.bulk_calling_request import BulkCallingRequest
    from app.services.plivo_service import PlivoService

    org = await _require_outbound_enabled(db, user)
    tr = await _tenant_for_user(db, user)
    if PlivoService.bulk_calling_enabled(tr):
        raise HTTPException(status_code=409, detail="Bulk calling is already enabled for your account.")
    latest = await _latest_bulk_request(db, org.id)
    if latest and latest.status == "pending":
        raise HTTPException(
            status_code=409,
            detail="You already have a pending request — we'll reach out shortly.",
        )
    contact = payload.contact_number.strip()
    if not contact:
        raise HTTPException(status_code=400, detail="A contact number is required.")
    db.add(
        BulkCallingRequest(
            organization_id=org.id,
            tenant_id=tr.tenant_id,
            requested_by_user_id=user.id,
            contact_number=contact[:32],
            note=(payload.note or "").strip()[:2000] or None,
        )
    )
    await db.commit()
    return {"ok": True, "request_status": "pending"}


@router.post("/bulk-calling/campaigns", status_code=status.HTTP_201_CREATED)
async def create_bulk_calling_campaign(
    request: Request,
    name: str = Form(...),
    # Lead source: "csv" (upload, the default) or "webhook" (CRM push via the
    # campaign-scoped API — no file; the campaign starts running-idle and dials
    # as leads arrive at POST /apex/v1/leads/{key}).
    lead_source: str = Form("csv"),
    contacts_file: UploadFile | None = File(None),
    content: str = Form(...),
    company_name: str | None = Form(None),
    caller_name: str | None = Form(None),
    language: str | None = Form(None),
    objectives: str | None = Form(None),
    exit_conditions: str | None = Form(None),
    tone: str | None = Form(None),
    silence_timeout_seconds: float | None = Form(None),
    questionnaire: str | None = Form(None),
    deterministic: bool = Form(False),
    # Deterministic-campaign calling schedule. The dialer ENFORCES this: it only
    # places calls inside 09:00–19:00 IST (TRAI), on the first ``working_days``
    # weekdays (5→Mon–Fri), and stops at ``calls_per_day`` placements/day.
    # ``hours_per_day`` is kept for the capacity readout (capped at 10 = the band).
    working_days: int | None = Form(None),
    hours_per_day: int | None = Form(None),
    calls_per_day: int | None = Form(None),
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Upload a CSV/XLSX of phone numbers and immediately start calling the whole
    list from the tenant's dedicated bulk number. Gated on the operator grant.

    Like the lead-based path, the operator supplies guided DETAILS
    (``company_name``, ``caller_name``, ``language``, ``tone``, goal) plus the
    ``content`` (what to say / offer); the engineered system prompt does the rest.
    """
    from app.services.plivo_service import PlivoService
    from app.services.public_url import public_base_url as _pub

    await _require_outbound_enabled(db, user)
    tr = await _tenant_for_user(db, user)
    org = await _org_for_user(db, user)
    if not PlivoService.bulk_calling_enabled(tr):
        raise HTTPException(
            status_code=403,
            detail=(
                "Bulk calling isn't enabled yet. Request access and we'll provision "
                "your dedicated calling number."
            ),
        )
    if not (name or "").strip():
        raise HTTPException(status_code=400, detail="Give the campaign a name.")
    # Two campaign types: a NON-deterministic campaign improvises from free-form
    # offer details (content); a DETERMINISTIC campaign runs a fixed intro +
    # scored questionnaire (content is not used). Require the field each type needs.
    if not deterministic and not (content or "").strip():
        raise HTTPException(
            status_code=400,
            detail="Tell the agent what to say — add the campaign content / offer details.",
        )
    agent_config = {
        # Content is the agent's KNOWLEDGE (agent_prompt), not a spoken line — do
        # NOT route it into pitch_summary, or the deterministic opener would read
        # the whole blob aloud. The agent pitches it conversationally from turn 2.
        "agent_prompt": content,
        "company_name": company_name,
        "caller_name": caller_name,
        "language": language,
        "objectives": _parse_campaign_list_field(objectives),
        "exit_conditions": _parse_campaign_list_field(exit_conditions),
        "tone": tone,
        "silence_timeout_seconds": silence_timeout_seconds,
    }
    try:
        # Parse the optional lead-capture questionnaire here so a validation
        # error (malformed JSON / answer row missing its expected answer) surfaces
        # via the same ValueError → _safe_detail 400 channel as the campaign body.
        parsed_questionnaire = _parse_questionnaire_field(questionnaire)
        if deterministic and not parsed_questionnaire:
            raise ValueError(
                "A deterministic campaign needs a questionnaire with at least one "
                "question. Add a question or switch to a non-deterministic campaign."
            )
        agent_config["deterministic"] = bool(deterministic)
        _apex_deterministic = False
        if parsed_questionnaire:
            # APEX deterministic campaigns speak scripted lines every call. Pre-
            # translate the intro + questions + outro into en/hi/te ONCE now so
            # call-time can serve the exact per-language string verbatim → TTS
            # cache hit + native multilingual. Best-effort (falls back to authored
            # text); one LLM call. Already-filled languages (admin hand-edits from
            # the translate-preview flow) are preserved — only blanks are filled.
            _apex_deterministic = deterministic and org.product_tier == "nokvo_apex"
            if _apex_deterministic:
                from app.services.questionnaire_translation import translate_questionnaire

                parsed_questionnaire = await translate_questionnaire(parsed_questionnaire)
            elif deterministic:
                # No pre-translation → no text_i18n → verbatim delivery never
                # fires and every question is LLM-rephrased. Loud, not silent.
                logger.warning(
                    "NOKVO-CAMPAIGN: deterministic questionnaire NOT pre-translated "
                    "(org tier is not nokvo_apex) — verbatim delivery disabled for it"
                )
            agent_config["questionnaire"] = parsed_questionnaire
        # Calling schedule. Stored on the config (no migration), surfaced via
        # _campaign_response's agent_config passthrough. For NOKVO APEX the dialer
        # ENFORCES it (09:00–19:00 IST window + weekday gate from working_days +
        # calls_per_day daily cap); for Nokvo One it stays a capacity readout only.
        # working_days is days/week (1–7, matching the PATCH clamp and the
        # dialer's weekday map).
        if working_days is not None or hours_per_day is not None or calls_per_day is not None:
            agent_config["call_window"] = {
                "working_days": max(1, min(7, int(working_days or 1))),
                "hours_per_day": max(1, min(10, int(hours_per_day or 1))),
                "timezone": "Asia/Kolkata",
                "window_local": "09:00-19:00",
            }
            if calls_per_day:
                agent_config["call_window"]["calls_per_day"] = max(1, int(calls_per_day))
        # Content guard — the FINAL config (post-translation, so hand-edited
        # i18n is covered too) must not disparage or impersonate another
        # company. Raises ValueError → the 400 channel below; nothing persists.
        from app.services.campaign_moderation_service import require_campaign_content_allowed

        await require_campaign_content_allowed(
            org,
            campaign_name=name.strip(),
            agent_config=agent_config,
            actor_email=user.email,
        )
        if lead_source == "webhook":
            campaign = await OutboundCampaignService.create_webhook_bulk_campaign(
                tr,
                db,
                name=name.strip(),
                agent_config=agent_config,
            )
        else:
            if contacts_file is None:
                raise HTTPException(
                    status_code=400,
                    detail="Attach a contacts file (CSV or XLSX), or switch the lead source to CRM / Webhook.",
                )
            campaign = await OutboundCampaignService.create_and_launch_bulk_campaign(
                tr,
                db,
                name=name.strip(),
                contacts_file=contacts_file,
                agent_config=agent_config,
                public_base_url=_pub(request),
                path_prefix="/api/nokvo-one/agents",
            )
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc
    if _apex_deterministic and parsed_questionnaire:
        # Fire-and-forget TTS pre-warm: synthesize every scripted line (intro /
        # questions / outro × en/hi/te) into the byte-cache now, so even the
        # campaign's FIRST call in each language plays cached audio. Best-effort —
        # it must never delay or fail the creation response.
        try:
            from app.services.apex_tts_prewarm import prewarm_campaign_tts

            _retain_task(asyncio.create_task(prewarm_campaign_tts(tr, parsed_questionnaire)))
        except Exception:
            logger.debug("APEX-TTS-PREWARM: failed to schedule", exc_info=True)
    resp = _campaign_response(campaign)
    if lead_source == "webhook":
        # Auto-mint the campaign's API key so the creation response IS the
        # integration panel: ingest URL + key, shown once (Reveal re-fetches
        # from KeyVault later). The key is the whole credential — the operator
        # pastes ingest_url into their CRM's webhook box and is integrated.
        from app.services import apex_campaign_key_service as keysvc

        record, raw_key = await keysvc.mint_for_campaign(
            db, org, campaign, tr, created_by_user_id=user.id
        )
        await db.commit()
        resp["api"] = {
            "ingest_url": f"{_pub(request)}/apex/v1/leads/{raw_key}",
            "api_key": raw_key,
            "key_prefix": record.key_prefix,
        }
    return resp


class QuestionnaireTranslatePayload(BaseModel):
    questionnaire: dict


@router.post("/bulk-calling/questionnaire/translate")
async def translate_bulk_questionnaire(
    payload: QuestionnaireTranslatePayload,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Translation PREVIEW for the campaign form: coerce the draft questionnaire
    and fill ``text_i18n`` / ``intro_i18n`` / ``outro_i18n`` (en/hi/te) so the
    admin can review and hand-edit the generated lines before launching. Nothing
    is persisted — the form submits the (possibly edited) i18n back with the
    campaign, and creation-time translation skips complete entries / fills only
    the blanks (see translate_questionnaire's merge), so edits survive."""
    from app.services.agent_outbound_context import _coerce_questionnaire
    from app.services.questionnaire_translation import translate_questionnaire

    await _require_outbound_enabled(db, user)
    try:
        questionnaire = _coerce_questionnaire(payload.questionnaire, strict=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_safe_detail(exc)) from exc
    if not questionnaire:
        raise HTTPException(
            status_code=422,
            detail="Add at least one question before translating the questionnaire.",
        )
    if (await _org_for_user(db, user)).product_tier == "nokvo_apex":
        questionnaire = await translate_questionnaire(questionnaire)
    return {"questionnaire": questionnaire}


class QuestionnaireStyleRewritePayload(BaseModel):
    questionnaire: dict
    style: str
    company_name: str | None = None
    caller_name: str | None = None
    content: str | None = None


@router.post("/bulk-calling/questionnaire/style-rewrite")
async def style_rewrite_bulk_questionnaire(
    payload: QuestionnaireStyleRewritePayload,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Conversation-style PREVIEW for the campaign form: rewrite the draft
    questionnaire's wording (questions + any admin intro/outro) into the chosen
    style so the admin reviews and hand-edits the exact lines before launching.
    The styled text replaces each line's ``text`` (original kept in
    ``*_source``), so translation/prewarm/verbatim delivery downstream run on
    it unchanged. Nothing is persisted — the form submits the (possibly edited)
    result with the campaign. ``warnings`` lists lines that KEPT their current
    wording (model missed them or the rewrite failed validation) so the UI can
    flag a partially styled script."""
    from app.services.agent_outbound_context import _coerce_questionnaire
    from app.services.questionnaire_style import normalize_style, rewrite_questionnaire

    await _require_outbound_enabled(db, user)
    try:
        questionnaire = _coerce_questionnaire(payload.questionnaire, strict=True)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_safe_detail(exc)) from exc
    if not questionnaire:
        raise HTTPException(
            status_code=422,
            detail="Add at least one question before restyling the questionnaire.",
        )
    style = normalize_style(payload.style)
    if str(payload.style or "").strip() and style != str(payload.style).strip().lower():
        raise HTTPException(status_code=422, detail="Unknown conversation style.")
    warnings: list[dict] = []
    if (
        settings.APEX_STYLE_REWRITE_ENABLED
        and (await _org_for_user(db, user)).product_tier == "nokvo_apex"
    ):
        questionnaire, warnings = await rewrite_questionnaire(
            questionnaire,
            style,
            company_name=payload.company_name,
            caller_name=payload.caller_name,
            content=payload.content,
        )
    return {"questionnaire": questionnaire, "style": style, "warnings": warnings}


class BulkCampaignUpdatePayload(BaseModel):
    name: str | None = None
    content: str | None = None
    company_name: str | None = None
    caller_name: str | None = None
    language: str | None = None
    tone: str | None = None
    questionnaire: dict | None = None
    working_days: int | None = None
    hours_per_day: int | None = None
    calls_per_day: int | None = None


@router.patch("/bulk-calling/campaigns/{campaign_id}")
async def update_bulk_calling_campaign(
    campaign_id: uuid.UUID,
    payload: BulkCampaignUpdatePayload,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Edit a campaign in place: name, script (intro/questions/outro), branding,
    language, and calling schedule. Only the provided fields change; contacts
    and status are untouched (Add CSV / Stop / Resume / Re-run manage those).
    Live calls pick the new config up on their next context load — the cache is
    invalidated here. A question whose TEXT changed loses its pre-translated
    lines (they describe the old text) and is re-translated; unchanged questions
    keep their (possibly hand-edited) translations. Question ids are stable so
    already-scored leads keep a valid score breakdown."""
    from app.services.agent_outbound_context import (
        _coerce_questionnaire,
        invalidate as invalidate_outbound_context,
    )
    from app.services.questionnaire_translation import (
        drop_stale_i18n,
        translate_questionnaire,
    )

    await _require_outbound_enabled(db, user)
    tr = await _tenant_for_user(db, user)
    org = await _org_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")

    cfg = dict(campaign.agent_config or {})
    if payload.name is not None:
        if not payload.name.strip():
            raise HTTPException(status_code=400, detail="Give the campaign a name.")
        campaign.name = payload.name.strip()
    for attr, key in (
        ("content", "agent_prompt"),
        ("company_name", "company_name"),
        ("caller_name", "caller_name"),
        ("language", "language"),
        ("tone", "tone"),
    ):
        value = getattr(payload, attr)
        if value is not None:
            cfg[key] = value.strip()

    _questionnaire_changed = False
    if payload.questionnaire is not None:
        try:
            questionnaire = _coerce_questionnaire(payload.questionnaire, strict=True)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=_safe_detail(exc)) from exc
        _deterministic = bool(cfg.get("deterministic") or cfg.get("questionnaire"))
        if _deterministic and not questionnaire:
            raise HTTPException(
                status_code=422,
                detail="A deterministic campaign needs a questionnaire with at least one question.",
            )
        if questionnaire:
            drop_stale_i18n(questionnaire, cfg.get("questionnaire"))
            if org.product_tier == "nokvo_apex":
                questionnaire = await translate_questionnaire(questionnaire)
            cfg["questionnaire"] = questionnaire
            _questionnaire_changed = True

    if any(v is not None for v in (payload.working_days, payload.hours_per_day, payload.calls_per_day)):
        # Merge into the EXISTING window — ``dialed_today`` is the runtime
        # daily-cap counter and must survive an edit.
        window = dict(cfg.get("call_window") or {})
        window.setdefault("timezone", "Asia/Kolkata")
        window.setdefault("window_local", "09:00-19:00")
        if payload.working_days is not None:
            window["working_days"] = max(1, min(7, int(payload.working_days)))
        if payload.hours_per_day is not None:
            window["hours_per_day"] = max(1, min(10, int(payload.hours_per_day)))
        if payload.calls_per_day is not None:
            if payload.calls_per_day > 0:
                window["calls_per_day"] = int(payload.calls_per_day)
            else:
                window.pop("calls_per_day", None)  # 0 = remove the cap
        cfg["call_window"] = window

    # Content guard on the MERGED result — edits land on RUNNING campaigns
    # (live calls reload config), so "create clean → launch → PATCH in the
    # abuse" must die here, before the commit. Schedule-only edits skip the
    # verdict: their text was already checked when it entered.
    _text_changed = _questionnaire_changed or payload.name is not None or any(
        getattr(payload, attr) is not None
        for attr in ("content", "company_name", "caller_name")
    )
    if _text_changed:
        from app.services.campaign_moderation_service import require_campaign_content_allowed

        try:
            await require_campaign_content_allowed(
                org,
                campaign_name=campaign.name,
                agent_config=cfg,
                actor_email=user.email,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=_safe_detail(exc)) from exc

    campaign.agent_config = cfg
    db.add(campaign)
    await db.commit()
    await db.refresh(campaign)
    # Live sessions read a cached context — drop it so the edit applies to the
    # very next call, not whenever the TTL happens to lapse.
    invalidate_outbound_context(campaign.id)
    # Re-translated lines are NEW strings → cold TTS-cache keys. Mirror the
    # create path's fire-and-forget pre-warm so the first calls after an edit
    # play cached audio instead of paying live synthesis per line.
    if _questionnaire_changed:
        try:
            from app.services.apex_tts_prewarm import prewarm_campaign_tts

            _retain_task(asyncio.create_task(prewarm_campaign_tts(tr, cfg.get("questionnaire"))))
        except Exception:
            logger.debug("APEX-TTS-PREWARM: failed to schedule on edit", exc_info=True)
    return _campaign_response(campaign)


class BulkRerunPayload(BaseModel):
    # Which outcome groups to re-dial: "no_pickup" (never connected — the
    # default, matching the original Re-run) and/or "busy" (answered but asked
    # to be called back). Pending contacts resume on any re-run.
    buckets: list[str] | None = None


@router.post("/bulk-calling/campaigns/{campaign_id}/rerun")
async def rerun_bulk_calling_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    payload: BulkRerunPayload | None = None,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Re-dial the selected outcome groups of a bulk campaign (didn't pick up /
    busy-call-back / both) and resume dialing. Defaults to the unreached, which
    is the original Re-run behavior."""
    from app.services.plivo_service import PlivoService
    from app.services.public_url import public_base_url as _pub

    await _require_outbound_enabled(db, user)
    buckets = list((payload.buckets if payload else None) or ["no_pickup"])
    if not set(buckets) <= {"no_pickup", "busy"} or not buckets:
        raise HTTPException(
            status_code=422,
            detail="buckets must be 'no_pickup', 'busy', or both.",
        )
    tr = await _tenant_for_user(db, user)
    if not PlivoService.bulk_calling_enabled(tr):
        raise HTTPException(
            status_code=403,
            detail="Bulk calling isn't enabled for your account.",
        )
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        campaign = await OutboundCampaignService.rerun_bulk_campaign(
            campaign,
            db,
            tenant_res=tr,
            public_base_url=_pub(request),
            path_prefix="/api/nokvo-one/agents",
            buckets=buckets,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.post("/bulk-calling/campaigns/{campaign_id}/resume")
async def resume_bulk_calling_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Resume a STOPPED bulk campaign — continues dialing the contacts still
    pending. Results are kept and nothing is re-armed (that's Re-run)."""
    from app.services.plivo_service import PlivoService
    from app.services.public_url import public_base_url as _pub

    await _require_outbound_enabled(db, user)
    tr = await _tenant_for_user(db, user)
    if not PlivoService.bulk_calling_enabled(tr):
        raise HTTPException(
            status_code=403,
            detail="Bulk calling isn't enabled for your account.",
        )
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        campaign = await OutboundCampaignService.resume_campaign(
            campaign,
            db,
            tenant_res=tr,
            public_base_url=_pub(request),
            path_prefix="/api/nokvo-one/agents",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.post("/bulk-calling/campaigns/{campaign_id}/add-contacts")
async def add_bulk_calling_contacts(
    campaign_id: uuid.UUID,
    request: Request,
    contacts_file: UploadFile = File(...),
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Add a new CSV/XLSX of contacts to an EXISTING bulk campaign and resume
    dialing. Only genuinely-new numbers are added (dedupe on phone); the ingest
    runs in the background, so this returns immediately."""
    from app.services.plivo_service import PlivoService
    from app.services.public_url import public_base_url as _pub

    await _require_outbound_enabled(db, user)
    tr = await _tenant_for_user(db, user)
    if not PlivoService.bulk_calling_enabled(tr):
        raise HTTPException(status_code=403, detail="Bulk calling isn't enabled for your account.")
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    raw = await contacts_file.read()
    try:
        await OutboundCampaignService.add_contacts_to_campaign(
            campaign, db, raw=raw, filename=contacts_file.filename, tenant_res=tr,
            public_base_url=_pub(request), path_prefix="/api/nokvo-one/agents",
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    return {"ok": True, "campaign_id": str(campaign.id), "status": "adding"}


# ── campaign API config (CRM in / CRM out) ───────────────────────────────────
async def _api_config_campaign(campaign_id: uuid.UUID, user, db):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return tr, campaign


@router.get("/bulk-calling/campaigns/{campaign_id}/api-config")
async def get_campaign_api_config(
    campaign_id: uuid.UUID,
    request: Request,
    user: OrganizationUser = Depends(_bulk_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Everything the campaign card's API panel shows: key state (masked),
    result-webhook config, the live ingest feed and recent delivery attempts."""
    from sqlalchemy import select as _select

    from app.models.crm_webhook_outbox import CrmWebhookOutbox
    from app.services import apex_campaign_key_service as keysvc
    from app.services import crm_webhook_service
    from app.services.public_url import public_base_url as _pub

    tr, campaign = await _api_config_campaign(campaign_id, user, db)
    record = await keysvc.active_key_for_campaign(db, campaign.id)
    whcfg = keysvc.webhook_config(campaign)
    deliveries = (
        (
            await db.execute(
                _select(CrmWebhookOutbox)
                .where(CrmWebhookOutbox.campaign_id == campaign.id)
                .order_by(CrmWebhookOutbox.created_at.desc())
                .limit(20)
            )
        )
        .scalars()
        .all()
    )
    return {
        "lead_source": (campaign.agent_config or {}).get("lead_source") or "csv",
        "has_key": record is not None,
        "key_prefix": record.key_prefix if record else None,
        "ingest_url_masked": (
            f"{_pub(request)}/apex/v1/leads/{record.key_prefix}{'•' * 8}" if record else None
        ),
        "webhook_url": whcfg.get("url"),
        "has_webhook_secret": bool(whcfg.get("secret_name") or whcfg.get("secret_fallback")),
        "feed": await crm_webhook_service.read_ingest_feed(campaign.id),
        "deliveries": [
            {
                "event": d.event,
                "status": d.status,
                "attempt": int(d.attempt or 0),
                "last_status_code": d.last_status_code,
                "last_error": d.last_error,
                "created_at": d.created_at.isoformat() if d.created_at else None,
                "delivered_at": d.delivered_at.isoformat() if d.delivered_at else None,
            }
            for d in deliveries
        ],
    }


class CampaignApiConfigPayload(BaseModel):
    # None/"" clears the result webhook.
    webhook_url: str | None = None


@router.put("/bulk-calling/campaigns/{campaign_id}/api-config")
async def put_campaign_api_config(
    campaign_id: uuid.UUID,
    payload: CampaignApiConfigPayload,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Set/clear the Result Webhook URL. First set generates the ``whsec_…``
    signing secret and returns it (retrievable again — it lives in KeyVault)."""
    from app.services import apex_campaign_key_service as keysvc

    tr, campaign = await _api_config_campaign(campaign_id, user, db)
    url = (payload.webhook_url or "").strip() or None
    if url and not url.startswith("https://") and not url.startswith("http://localhost"):
        raise HTTPException(status_code=400, detail="The result webhook URL must be https.")
    raw_secret = await keysvc.set_result_webhook(db, campaign, tr, url=url)
    await db.commit()
    return {"ok": True, "webhook_url": url, "webhook_secret": raw_secret}


@router.post("/bulk-calling/campaigns/{campaign_id}/api-key/reveal")
async def reveal_campaign_api_key(
    campaign_id: uuid.UUID,
    request: Request,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    from app.services import apex_campaign_key_service as keysvc
    from app.services.public_url import public_base_url as _pub

    tr, campaign = await _api_config_campaign(campaign_id, user, db)
    if await keysvc.active_key_for_campaign(db, campaign.id) is None:
        raise HTTPException(status_code=404, detail="This campaign has no API key yet — rotate to create one.")
    raw = await keysvc.reveal(tr, campaign)
    if not raw:
        raise HTTPException(
            status_code=409,
            detail="The key can't be revealed in this environment — rotate to get a new one.",
        )
    return {"api_key": raw, "ingest_url": f"{_pub(request)}/apex/v1/leads/{raw}"}


@router.post("/bulk-calling/campaigns/{campaign_id}/api-key/rotate")
async def rotate_campaign_api_key(
    campaign_id: uuid.UUID,
    request: Request,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Rotate (or first-mint — this is also how a CSV campaign turns on API
    access) the campaign key. The old key stops working immediately."""
    from app.services import apex_campaign_key_service as keysvc
    from app.services.public_url import public_base_url as _pub

    tr, campaign = await _api_config_campaign(campaign_id, user, db)
    org = await _org_for_user(db, user)
    record, raw = await keysvc.rotate(db, org, campaign, tr, created_by_user_id=user.id)
    await db.commit()
    return {
        "api_key": raw,
        "key_prefix": record.key_prefix,
        "ingest_url": f"{_pub(request)}/apex/v1/leads/{raw}",
    }


@router.post("/bulk-calling/campaigns/{campaign_id}/webhook-test")
async def test_campaign_webhook(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """[Send Test Webhook]: POST a signed sample ``lead.completed`` to the
    configured Result Webhook URL synchronously and echo what came back — the
    operator's 'did my hookup work?' button."""
    from app.services import apex_campaign_key_service as keysvc
    from app.services import crm_webhook_service

    tr, campaign = await _api_config_campaign(campaign_id, user, db)
    whcfg = keysvc.webhook_config(campaign)
    url = whcfg.get("url")
    if not url:
        raise HTTPException(status_code=400, detail="Set a Result Webhook URL first.")
    secret = await keysvc.signing_secret(campaign)
    status_code, error = await crm_webhook_service.post_signed(
        url, secret, crm_webhook_service.build_test_payload(campaign)
    )
    return {
        "ok": bool(status_code and 200 <= status_code < 300),
        "status_code": status_code,
        "error": error,
    }


@router.get("/campaigns")
async def list_campaigns(
    user: OrganizationUser = Depends(_bulk_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    tr = await _tenant_for_user(db, user)
    items = await OutboundCampaignService.list_campaigns(tr, db)
    return [_campaign_response(c) for c in items]


@router.post("/campaigns", status_code=status.HTTP_201_CREATED)
async def create_campaign(
    request: Request,
    name: str = Form(...),
    lead_ids: str | None = Form(None),
    capture_form_id: str | None = Form(None),
    launch: bool = Form(False),
    excel_file: UploadFile | None = File(None),
    doc_file: UploadFile | None = File(None),
    from_number: str | None = Form(None),
    content: str = Form(...),
    company_name: str | None = Form(None),
    caller_name: str | None = Form(None),
    language: str | None = Form(None),
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

    The operator supplies guided campaign DETAILS (``company_name``,
    ``caller_name``, ``language``, ``tone``, the ``objectives`` goal) plus the
    campaign CONTENT (``content`` — what to say / the offer details). The
    engineered outbound system prompt (``agent_outbound_context``) turns those
    into the agent's behaviour — there is no hand-written delivery prompt.
    ``content`` becomes both the layered campaign knowledge and the short pitch.

    ``lead_ids`` is optional. When omitted, the campaign is created in
    prompt-only mode and consented leads can be attached later via
    ``POST /campaigns/{id}/leads``.

    ``capture_form_id`` is a shortcut for "build this campaign from every
    callable lead of one lead form" (Option A — sort leads by their
    originating form/project). When given and ``lead_ids`` is empty, the
    server resolves the form's callable leads itself, so it isn't bounded by
    the leads-list page size.
    """
    tr = await _tenant_for_user(db, user)
    org = await _org_for_user(db, user)
    # Outbound has moved to NOKVO APEX — Nokvo One can't create outbound campaigns
    # at all (not even drafts). APEX uses the /bulk-calling endpoints, not this one.
    if _nokvo_one_outbound_blocked(org):
        raise HTTPException(status_code=403, detail=_NOKVO_ONE_OUTBOUND_DISABLED_DETAIL)
    # "Create & call" (launch=true): gate on the plan BEFORE creating, so we never
    # leave a campaign that can't be launched.
    if launch:
        await _require_outbound_enabled(db, user)
    if not (content or "").strip():
        raise HTTPException(
            status_code=400,
            detail=(
                "Tell the agent what to say — add the campaign content / offer "
                "details and try again."
            ),
        )
    try:
        parsed_lead_ids: list[uuid.UUID] = []
        if lead_ids:
            raw_ids = json.loads(lead_ids) if lead_ids.strip().startswith("[") else [x.strip() for x in lead_ids.split(",")]
            parsed_lead_ids = [uuid.UUID(str(item)) for item in raw_ids if str(item).strip()]
        elif capture_form_id and capture_form_id.strip():
            parsed_lead_ids = await OutgoingLeadService.callable_lead_ids_for_form(
                tr, db, uuid.UUID(capture_form_id.strip())
            )
            if not parsed_lead_ids:
                raise OutgoingLeadServiceError(
                    "That form has no callable leads yet (leads need call consent "
                    "and a valid phone number). Nothing to add."
                )
        agent_config = {
            # Content is the agent's KNOWLEDGE (agent_prompt), not a spoken line —
            # do NOT route it into pitch_summary, or the deterministic opener would
            # read the whole blob aloud. The agent pitches it from turn 2.
            "agent_prompt": content,
            "company_name": company_name,
            "caller_name": caller_name,
            "language": language,
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
        # Content guard — same gate as the bulk-calling paths: no campaign that
        # disparages or impersonates another company gets persisted, not even
        # as a draft. Raises ValueError → the 400 channel below.
        from app.services.campaign_moderation_service import require_campaign_content_allowed

        await require_campaign_content_allowed(
            org,
            campaign_name=name,
            agent_config=agent_config,
            actor_email=user.email,
        )
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

    # "Create & call": launch immediately (calls go out from the allotted number,
    # throttled). The plan gate already ran above. If dialing can't start (e.g. no
    # provisioned number), the campaign stays a draft and we surface 409.
    if launch and campaign.contacts:
        from app.services.public_url import public_base_url as _pub

        try:
            campaign = await OutboundCampaignService.launch_campaign(
                campaign,
                db,
                public_base_url=_pub(request),
                path_prefix="/api/nokvo-one/agents",
                tenant_res=tr,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    return _campaign_response(campaign)


@router.post("/campaigns/{campaign_id}/leads")
async def attach_campaign_leads(
    campaign_id: uuid.UUID,
    lead_ids: str | None = Form(None),
    capture_form_id: str | None = Form(None),
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Attach consented leads to an existing draft campaign.

    Provide either ``lead_ids`` (a JSON array string ``"[\"uuid\", ...]"`` or a
    comma-separated list) or ``capture_form_id`` to attach every callable lead
    of one lead form. Each lead must be callable + scoped to the caller's
    tenant. Already-attached leads are silently skipped.
    """
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        if lead_ids and lead_ids.strip():
            raw_ids = json.loads(lead_ids) if lead_ids.strip().startswith("[") else [
                x.strip() for x in lead_ids.split(",")
            ]
            parsed = [uuid.UUID(str(item)) for item in raw_ids if str(item).strip()]
        elif capture_form_id and capture_form_id.strip():
            parsed = await OutgoingLeadService.callable_lead_ids_for_form(
                tr, db, uuid.UUID(capture_form_id.strip())
            )
            if not parsed:
                raise OutgoingLeadServiceError(
                    "That form has no callable leads yet (leads need call consent "
                    "and a valid phone number). Nothing to add."
                )
        else:
            raise OutgoingLeadServiceError("Provide lead_ids or capture_form_id.")
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


# ── V2 per-row reads (summary / keyset pagination / streaming CSV) ────────────
async def _resolve_bulk_campaign(campaign_id: uuid.UUID, user: OrganizationUser, db: AsyncSession):
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    return campaign


@router.get("/campaigns/{campaign_id}/summary")
async def campaign_summary(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(_bulk_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Per-bucket contact counts via one indexed GROUP BY (never loads rows).
    Powers the APEX dashboard tiles at any campaign size."""
    await _resolve_bulk_campaign(campaign_id, user, db)
    from app.services import campaign_contacts_v2 as v2

    return await v2.summary(db, campaign_id)


@router.get("/campaigns/{campaign_id}/contacts")
async def campaign_contacts_page(
    campaign_id: uuid.UUID,
    bucket: str = "qualified",
    cursor: str | None = None,
    limit: int = 100,
    user: OrganizationUser = Depends(_bulk_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Keyset-paginated contacts for a bucket (qualified / not_interested /
    no_pickup / pending). ``cursor`` = last row id from the previous page."""
    await _resolve_bulk_campaign(campaign_id, user, db)
    from app.services import campaign_contacts_v2 as v2

    return await v2.page_contacts(db, campaign_id, bucket, cursor=cursor, limit=limit)


@router.get("/campaigns/{campaign_id}/contacts.csv")
async def campaign_contacts_csv(
    campaign_id: uuid.UUID,
    bucket: str = "qualified",
    user: OrganizationUser = Depends(_bulk_viewer_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Stream a bucket's contacts as CSV (phone col A, name col B) straight from
    the DB — keyset-paged so a 1M list never lands in memory."""
    await _resolve_bulk_campaign(campaign_id, user, db)

    def _esc(v: Any) -> str:
        s = str(v or "").strip()
        return f'"{s.replace(chr(34), chr(34) * 2)}"' if any(ch in s for ch in ',"\n\r') else s

    async def _gen():
        # Own session — a StreamingResponse generator runs AFTER the request's db
        # dependency is closed, so we can't reuse it for the stream.
        from app.db.session import AsyncSessionLocal
        from app.services import campaign_contacts_v2 as v2

        yield "Phone,Name\r\n"
        async with AsyncSessionLocal() as sdb:
            async for phone, name in v2.iter_csv_rows(sdb, campaign_id, bucket):
                yield f"{_esc(phone)},{_esc(name)}\r\n"

    return StreamingResponse(
        _gen(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="campaign-{bucket}.csv"'},
    )


@router.post("/campaigns/{campaign_id}/launch")
async def launch_campaign(
    campaign_id: uuid.UUID,
    request: Request,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    await _require_outbound_enabled(db, user)  # 403 unless on the Inbound + Outbound plan
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    from app.services.public_url import public_base_url as _pub

    public_base = _pub(request)
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
    # _bulk_admin_dep (NOT _admin_dep): cancel is part of the shared bulk-calling
    # surface — the APEX ■ Stop button calls it, and _admin_dep's nokvo_one-only
    # tier gate 403'd every APEX org before role/MFA were even checked.
    user: OrganizationUser = Depends(_bulk_admin_dep()),
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


class CampaignFollowupToggle(BaseModel):
    enabled: bool


@router.patch("/campaigns/{campaign_id}/followup")
async def set_campaign_followup(
    campaign_id: uuid.UUID,
    payload: CampaignFollowupToggle,
    user: OrganizationUser = Depends(_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Turn the follow-up agent on/off for a campaign."""
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    campaign = await OutboundCampaignService.set_followup_enabled(campaign, payload.enabled, db)
    return _campaign_response(campaign)


@router.delete("/campaigns/{campaign_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_campaign(
    campaign_id: uuid.UUID,
    user: OrganizationUser = Depends(_bulk_admin_dep()),
    _mfa: OrganizationUser = Depends(deps.RequireMFACompleted()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Hard-delete a campaign and its contact join rows.

    Shared bulk-calling surface (``_bulk_admin_dep`` — Nokvo One AND NOKVO APEX,
    org-scoped so each product only deletes its own). Running campaigns are
    protected → 409; operators must cancel them first. Lead records and call-cost
    rows are NOT touched (leads outlive campaigns; the cost ledger remains as a
    billing audit trail).
    """
    tr = await _tenant_for_user(db, user)
    campaign = await OutboundCampaignService.get_campaign(campaign_id, tr, db)
    if not campaign:
        raise HTTPException(status_code=404, detail="Campaign not found")
    try:
        await OutboundCampaignService.delete_campaign(campaign, db)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=_safe_detail(exc)) from exc
    except IntegrityError as exc:
        # A row somewhere still references the campaign (a FK this delete path
        # doesn't know about yet). Surface a readable 409 instead of a 500 and
        # leave the campaign intact for diagnosis.
        await db.rollback()
        logger.exception("NOKVO-CAMPAIGN: delete blocked by a foreign key (campaign=%s)", campaign_id)
        raise HTTPException(
            status_code=409,
            detail="This campaign still has linked records and can't be deleted. Contact support.",
        ) from exc
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
        "lead_id": str(row.lead_id) if row.lead_id else None,
        "customer_id": str(getattr(row, "customer_id", None)) if getattr(row, "customer_id", None) else None,
        "campaign_id": str(row.campaign_id) if row.campaign_id else None,
        "source_call_id": row.source_call_id,
        "scheduled_at": row.scheduled_at.isoformat() if row.scheduled_at else None,
        "reason": row.reason.value if row.reason else None,
        "note": getattr(row, "note", None),
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


@router.get("/followups/pipeline")
async def followups_pipeline(
    user: OrganizationUser = Depends(_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Full Follow-Up Pipeline payload for the dedicated page: status counts,
    the pending queue bucketed by due window, today's actionable queue (with
    each lead's handoff note + overdue flag), a recent-called sample, and the
    month's conversion ROI. One service call, scoped to the caller's tenant."""
    from app.services.followup_scheduler_service import FollowupSchedulerService

    tr = await _tenant_for_user(db, user)
    return await FollowupSchedulerService.pipeline_for_tenant(
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
    # Envelope: include the lead's condensed handoff note alongside the
    # follow-up rows so the LeadsView drawer can render the manager-readable
    # summary above the schedule list in one round-trip.
    return {
        "items": [_followup_response(r) for r in rows],
        "lead": {
            "id": str(lead.id),
            "handoff_note": lead.handoff_note,
            "handoff_note_generated_at": (
                lead.handoff_note_generated_at.isoformat()
                if lead.handoff_note_generated_at else None
            ),
        },
    }


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
