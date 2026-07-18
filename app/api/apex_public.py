"""APEX public CRM API — key-scoped lead ingest + status reads.

The key IS the campaign: every route resolves its one campaign from the
``nkap_…`` key (``deps.get_campaign_api_key``), so no URL carries a campaign
id and a leaked key exposes exactly one campaign.

Routes (mounted at ``/apex/v1``):
  - POST /leads/{token}    — catch-hook ingest; the token is the full key, so a
                             CRM webhook box that can't set headers still works
                             by pasting ONE url. Alias-tolerant body.
  - POST /leads            — same handler, key in headers (developer form).
  - GET  /leads            — cursor list of leads + verdicts (polling CRMs).
  - GET  /leads/{ref}      — one lead by external_id (or phone) → status/verdict.

Ingest is idempotent by construction: rows dedupe on ``(campaign_id, phone)``
via ON CONFLICT DO NOTHING, so CRM retries/double-fires are harmless.
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
from app.models.outgoing_lead import OutboundCampaignContact
from app.services import campaign_contacts_v2 as v2
from app.services import crm_webhook_service
from app.services.outbound_campaign_service import (
    OutboundCampaignService,
    _canonical_phone,
)
from app.services.public_url import public_base_url

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_BATCH = 500

# Field aliases (case-insensitive) so raw CRM webhook payloads usually work
# with zero mapping — a large share of the "insanely easy" promise.
_PHONE_KEYS = ("phone", "mobile", "phone_number", "mobile_number", "contact_number", "phone_no", "msisdn", "whatsapp")
_NAME_KEYS = ("name", "full_name", "fullname", "lead_name", "contact_name", "customer_name")
_FIRST_KEYS = ("first_name", "firstname")
_LAST_KEYS = ("last_name", "lastname")
_EXTERNAL_KEYS = ("external_id", "lead_id", "record_id", "crm_id", "contact_id", "id")
_LIST_KEYS = ("leads", "data", "contacts", "records")


def _fold(obj: dict[str, Any]) -> dict[str, Any]:
    return {str(k).strip().lower(): v for k, v in obj.items()}


def _first(folded: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for k in keys:
        v = folded.get(k)
        if v is not None and str(v).strip():
            return v
    return None


def _extract_lead(obj: Any) -> tuple[dict[str, Any] | None, str | None]:
    """One raw CRM object → ``{phone, name, external_id, meta}`` or an error.
    Phone lands in the same canonical bare-digit form the CSV path uses
    (``_canonical_phone``) so API and CSV contacts dedupe against each other."""
    if not isinstance(obj, dict):
        return None, "lead must be a JSON object"
    folded = _fold(obj)
    raw_phone = _first(folded, _PHONE_KEYS)
    if raw_phone is None:
        return None, f"no phone field found (accepted: {', '.join(_PHONE_KEYS)})"
    phone = _canonical_phone(str(raw_phone))
    if not phone or len(phone) < 8 or len(phone) > 15:
        return None, f"unusable phone number: {raw_phone!r}"
    name = _first(folded, _NAME_KEYS)
    if name is None:
        first, last = _first(folded, _FIRST_KEYS), _first(folded, _LAST_KEYS)
        name = " ".join(str(p).strip() for p in (first, last) if p) or None
    external_id = _first(folded, _EXTERNAL_KEYS)
    consumed = set(_PHONE_KEYS) | set(_NAME_KEYS) | set(_FIRST_KEYS) | set(_LAST_KEYS) | set(_EXTERNAL_KEYS)
    meta = {k: v for k, v in folded.items() if k not in consumed and isinstance(v, (str, int, float, bool))}
    return {
        "phone": phone,
        "name": str(name).strip()[:200] if name else None,
        "external_id": str(external_id).strip()[:200] if external_id is not None else None,
        "meta": meta or None,
    }, None


async def _read_body(request: Request) -> Any:
    """JSON first; fall back to form-encoding (some CRM webhook boxes only
    speak x-www-form-urlencoded)."""
    try:
        return await request.json()
    except Exception:
        try:
            form = await request.form()
            return {k: v for k, v in form.items()}
        except Exception:
            return None


def _leads_from_body(body: Any) -> list[Any]:
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        folded = _fold(body)
        for k in _LIST_KEYS:
            if isinstance(folded.get(k), list):
                return folded[k]
        return [body]
    return []


async def _enforce_rate_limit(record) -> None:
    """Fixed-window per-key RPM (best-effort — Redis trouble never blocks ingest)."""
    from app.services.agent_session_store import AgentSessionStore

    try:
        key = f"apex:rl:{record.id}:{int(time.time() // 60)}"
        r = AgentSessionStore.client()
        n = await r.incr(key)
        await r.expire(key, 90)
    except Exception:
        return
    if n > int(record.rate_limit_rpm or 60):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded for this API key — retry shortly.",
        )


def _mask_phone(phone: str) -> str:
    return ("•" * max(0, len(phone) - 4)) + phone[-4:] if phone else ""


async def _kick_dialer(campaign_id: uuid.UUID) -> None:
    """Post-ingest nudge so the first call happens promptly (the ticker would
    get there, but 'lead arrives → phone rings' is the demo). All the real
    gates (window/cap/balance/DND) live inside the dial path."""
    try:
        async with AsyncSessionLocal() as db:
            campaign = (
                await db.execute(select(OutboundCampaign).where(OutboundCampaign.id == campaign_id))
            ).scalars().first()
            if campaign is None or campaign.status != CampaignStatus.running:
                return
            await OutboundCampaignService.dial_next_pending(
                campaign, db,
                public_base_url=public_base_url(),
                path_prefix="/api/nokvo-one/agents",
            )
    except Exception:
        logger.exception("APEX-API: post-ingest dial kick failed campaign=%s", campaign_id)


@router.post("/leads/{token}", status_code=status.HTTP_202_ACCEPTED)
@router.post("/leads", status_code=status.HTTP_202_ACCEPTED)
async def ingest_leads(
    request: Request,
    auth=Depends(deps.get_campaign_api_key),
    db: AsyncSession = Depends(deps.get_db),
):
    record, org, tenant_res, campaign = auth
    await _enforce_rate_limit(record)
    if not settings.CAMPAIGN_CONTACTS_V2:
        raise HTTPException(status_code=503, detail="Lead ingest is not available right now.")

    body = await _read_body(request)
    raw_leads = _leads_from_body(body)
    if not raw_leads:
        raise HTTPException(
            status_code=400,
            detail='Send a lead object (e.g. {"phone": "+91…", "name": "…"}) or {"leads": [...]}.',
        )
    if len(raw_leads) > _MAX_BATCH:
        raise HTTPException(status_code=400, detail=f"Batch too large — max {_MAX_BATCH} leads per request.")

    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for i, obj in enumerate(raw_leads):
        lead, err = _extract_lead(obj)
        if err:
            errors.append({"index": i, "error": err})
            await crm_webhook_service.push_ingest_event(campaign.id, {"kind": "error", "error": err})
        else:
            rows.append(lead)

    # Legacy blob campaign → one-way upgrade into the per-row store (mirrors
    # add_contacts_to_campaign) so an old campaign can still take API leads.
    if campaign.contacts is not None:
        await v2.migrate_blob_campaign(db, campaign)

    inserted, duplicates = await v2.ingest_api_rows(db, campaign.id, rows)
    if inserted:
        await v2.bump_campaign_counter(db, campaign.id, "total_count", inserted)
        await db.commit()

    warning: str | None = None
    # An exhausted (completed) campaign wakes up when the CRM pushes — the
    # evergreen behavior. Same slot discipline as add_contacts_to_campaign: the
    # tenant runs ONE campaign at a time, so if another is mid-flight the leads
    # stay pending and the ticker resumes this campaign once the slot frees.
    if inserted and campaign.status == CampaignStatus.completed:
        try:
            await OutboundCampaignService._assert_no_other_active_campaign(
                db, tenant_res.tenant_id, exclude_id=campaign.id
            )
            campaign.status = CampaignStatus.running
            campaign.completed_at = None
            db.add(campaign)
            await db.commit()
        except ValueError:
            await db.rollback()
            warning = "queued_behind_active_campaign"
    elif campaign.status == CampaignStatus.draft:
        warning = "campaign_not_launched"

    if warning is None:
        try:
            from app.services.minute_balance_service import has_balance

            if not await has_balance(db, org.id):
                warning = "balance_exhausted"
        except Exception:
            pass

    for lead in rows:
        await crm_webhook_service.push_ingest_event(
            campaign.id,
            {
                "kind": "accepted",
                "name": lead.get("name"),
                "phone_masked": _mask_phone(lead["phone"]),
                "external_id": lead.get("external_id"),
            },
        )

    if inserted and campaign.status == CampaignStatus.running:
        asyncio.get_running_loop().create_task(_kick_dialer(campaign.id))

    out: dict[str, Any] = {"accepted": inserted, "duplicates": duplicates, "errors": errors}
    if warning:
        out["warning"] = warning
    return out


def _lead_json(row: OutboundCampaignContact) -> dict[str, Any]:
    result = dict(row.result or {})
    outcome = crm_webhook_service.outcome_status(row.status, bool(row.qualified), result)
    ts = row.updated_at or row.created_at
    return {
        "external_id": row.external_id,
        "phone": row.phone,
        "name": row.name,
        "status": outcome,
        "qualified": bool(row.qualified),
        "lead_score": row.lead_score,
        "max_score": result.get("max_score"),
        "answers": result.get("score_breakdown"),
        "call_note": result.get("call_note") or result.get("interest_reason"),
        "callback_requested": bool(result.get("callback_requested")),
        "attempts": int(row.attempt or 0),
        "duration_s": float(row.duration_s) if row.duration_s is not None else None,
        "updated_at": ts.isoformat() if ts else None,
    }


# Outcome vocabulary → row predicate for the list filter.
_STATUS_SQL = {
    "qualified": "qualified = true",
    "not_qualified": "status = 'completed' AND qualified = false",
    "unreachable": "status IN ('no_answer', 'failed')",
    "dnd_blocked": "status = 'dnd_dropped'",
    "queued": "status = 'pending'",
    "calling": "status IN ('dialing', 'ringing', 'answered')",
}


@router.get("/leads")
async def list_leads(
    since: str | None = None,
    status_filter: str | None = None,
    limit: int = 100,
    auth=Depends(deps.get_campaign_api_key),
    db: AsyncSession = Depends(deps.get_db),
):
    """Cursor list for polling CRMs: leads ordered by last change, oldest first,
    so a client can walk forward with ``since=<next_cursor>``."""
    _record, _org, _tr, campaign = auth
    limit = max(1, min(int(limit or 100), 200))
    where = ["campaign_id = :c"]
    params: dict[str, Any] = {"c": str(campaign.id)}
    if since:
        where.append("COALESCE(updated_at, created_at) > CAST(:since AS timestamptz)")
        params["since"] = since
    if status_filter:
        pred = _STATUS_SQL.get(status_filter)
        if pred is None:
            raise HTTPException(status_code=400, detail=f"Unknown status — use one of: {', '.join(_STATUS_SQL)}")
        where.append(pred)
    rows = (
        await db.execute(
            select(OutboundCampaignContact)
            .where(text(" AND ".join(where)).bindparams(**params))
            .order_by(text("COALESCE(updated_at, created_at) ASC"))
            .limit(limit)
        )
    ).scalars().all()
    items = [_lead_json(r) for r in rows]
    next_cursor = items[-1]["updated_at"] if len(items) == limit else None
    return {"leads": items, "next_cursor": next_cursor}


@router.get("/leads/{ref}")
async def get_lead(
    ref: str,
    auth=Depends(deps.get_campaign_api_key),
    db: AsyncSession = Depends(deps.get_db),
):
    """Lookup by the CRM's ``external_id``; falls back to phone so a bare
    number pasted in a browser works too."""
    _record, _org, _tr, campaign = auth
    row = (
        await db.execute(
            select(OutboundCampaignContact)
            .where(
                OutboundCampaignContact.campaign_id == campaign.id,
                OutboundCampaignContact.external_id == ref,
            )
            .order_by(OutboundCampaignContact.created_at.desc())
        )
    ).scalars().first()
    if row is None:
        phone = _canonical_phone(ref)
        if phone:
            row = (
                await db.execute(
                    select(OutboundCampaignContact).where(
                        OutboundCampaignContact.campaign_id == campaign.id,
                        OutboundCampaignContact.phone == phone,
                    )
                )
            ).scalars().first()
    if row is None:
        raise HTTPException(status_code=404, detail="No lead with that external_id or phone in this campaign.")
    return _lead_json(row)
