"""CRM result webhooks — build, enqueue and sign verdict deliveries.

The outbound half of the APEX CRM API: when a contact on a CRM-fed campaign
reaches a terminal state, the verdict is POSTed to the campaign's configured
Result Webhook URL so the CRM can update its own record (mapped back via the
``external_id`` the CRM sent at ingest).

Delivery is at-least-once through ``crm_webhook_outbox`` (drained by
``crm_webhook_drainer`` with backoff) — never fire-and-forget, because the
verdict reaching the CRM is the product promise. Enqueue is fail-soft: it runs
inside call-teardown paths and must never break them.

Events:
  * ``lead.completed``   — call connected and was scored (qualified | not_qualified)
  * ``lead.unreachable`` — terminal no_answer / failed
  * ``lead.dnd_blocked`` — number scrubbed by DND/NCPR; it will never be dialed
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import select

from app.models.crm_webhook_outbox import CrmWebhookOutbox
from app.models.outbound_campaign import OutboundCampaign
from app.models.outgoing_lead import OutboundCampaignContact

logger = logging.getLogger(__name__)

# Retry backoff per attempt (seconds); after the last one the row goes ``dead``.
BACKOFF_SCHEDULE = [60, 300, 900, 3600, 21600]
DELIVERY_TIMEOUT_S = 10.0


# ── outcome mapping (shared with the public GET endpoints) ───────────────────
def outcome_status(row_status: str, qualified: bool, result: dict[str, Any] | None) -> str:
    """Contact row → the customer-facing outcome vocabulary."""
    if row_status == "completed":
        return "qualified" if qualified else "not_qualified"
    if row_status in ("no_answer", "failed"):
        return "unreachable"
    if row_status == "dnd_dropped":
        return "dnd_blocked"
    if row_status == "pending":
        return "queued"
    return "calling"  # dialing / ringing / answered


def event_for(outcome: str) -> str | None:
    """Terminal outcomes get an event; live ones don't."""
    return {
        "qualified": "lead.completed",
        "not_qualified": "lead.completed",
        "unreachable": "lead.unreachable",
        "dnd_blocked": "lead.dnd_blocked",
    }.get(outcome)


def _campaign_threshold(campaign: OutboundCampaign) -> int | None:
    from app.services.lead_score_service import extract_questionnaire

    parsed = extract_questionnaire(campaign.agent_config or {})
    return parsed[1] if parsed else None


def build_payload(
    campaign: OutboundCampaign, contact: OutboundCampaignContact, event: str
) -> dict[str, Any]:
    result = dict(contact.result or {})
    outcome = outcome_status(contact.status, bool(contact.qualified), result)
    breakdown = result.get("score_breakdown")
    answers = breakdown if isinstance(breakdown, list) else None
    completed_at = contact.updated_at or contact.answered_at
    return {
        "event": event,
        "campaign": {"id": str(campaign.id), "name": campaign.name},
        "lead": {
            "external_id": contact.external_id,
            "phone": contact.phone,
            "name": contact.name,
        },
        "outcome": {
            "status": outcome,
            "lead_score": contact.lead_score,
            "max_score": result.get("max_score"),
            "threshold": _campaign_threshold(campaign),
            "answers": answers,
            "call_note": result.get("call_note") or result.get("interest_reason"),
            "callback_requested": bool(result.get("callback_requested")),
            "attempts": int(contact.attempt or 0),
            "duration_s": float(contact.duration_s) if contact.duration_s is not None else None,
            "completed_at": completed_at.isoformat() if completed_at else None,
        },
    }


def build_test_payload(campaign: OutboundCampaign) -> dict[str, Any]:
    """The [Send Test Webhook] sample — same shape as a real lead.completed."""
    return {
        "event": "lead.completed",
        "test": True,
        "campaign": {"id": str(campaign.id), "name": campaign.name},
        "lead": {"external_id": "test-lead-1", "phone": "919876543210", "name": "Test Lead"},
        "outcome": {
            "status": "qualified",
            "lead_score": 3,
            "max_score": 4,
            "threshold": _campaign_threshold(campaign) or 2,
            "answers": [{"question": "Sample question", "answer": "Sample answer", "points": 1}],
            "call_note": "This is a test delivery from NOKVO APEX.",
            "callback_requested": False,
            "attempts": 1,
            "duration_s": 42.0,
            "completed_at": datetime.now(timezone.utc).isoformat(),
        },
    }


async def enqueue_for_link(db, call_link_id: str, *, event: str | None = None) -> bool:
    """Queue a delivery for the contact behind ``call_link_id``. Loads the row
    and its campaign, no-ops unless the campaign has a Result Webhook URL, and
    derives the event from the row's terminal state when not given. Fail-soft —
    call-teardown paths must never break on webhook bookkeeping. Commits."""
    try:
        row = (
            await db.execute(
                select(OutboundCampaignContact).where(
                    OutboundCampaignContact.call_link_id == call_link_id
                )
            )
        ).scalars().first()
        if row is None:
            return False
        campaign = (
            await db.execute(
                select(OutboundCampaign).where(OutboundCampaign.id == row.campaign_id)
            )
        ).scalars().first()
        if campaign is None:
            return False
        from app.services.apex_campaign_key_service import webhook_config

        cfg = webhook_config(campaign)
        if not (cfg.get("enabled") and cfg.get("url")):
            return False
        if event is None:
            event = event_for(outcome_status(row.status, bool(row.qualified), row.result))
        if not event:
            return False
        db.add(
            CrmWebhookOutbox(
                id=uuid.uuid4(),
                campaign_id=campaign.id,
                call_link_id=call_link_id,
                event=event,
                payload=build_payload(campaign, row, event),
                status="pending",
                attempt=0,
                next_attempt_at=datetime.now(timezone.utc),
            )
        )
        await db.commit()
        return True
    except Exception:
        logger.exception("CRM-WEBHOOK: enqueue failed for call_link=%s", call_link_id)
        try:
            await db.rollback()
        except Exception:
            pass
        return False


# ── ingest event feed (the "waiting for your first lead…" live UI) ──────────
_FEED_KEY = "apex:ingest_feed:{campaign_id}"
_FEED_MAX = 50
_FEED_TTL_S = 7 * 24 * 3600


async def push_ingest_event(campaign_id, entry: dict[str, Any]) -> None:
    """Best-effort rolling log of the last N ingest events per campaign (Redis
    list, no migration). Read by the campaign card's live feed — the moment of
    'did my Zoho hookup work?' answered without opening logs."""
    from app.services.agent_session_store import AgentSessionStore

    try:
        key = _FEED_KEY.format(campaign_id=campaign_id)
        r = AgentSessionStore.client()
        await r.lpush(key, json.dumps({"ts": datetime.now(timezone.utc).isoformat(), **entry}))
        await r.ltrim(key, 0, _FEED_MAX - 1)
        await r.expire(key, _FEED_TTL_S)
    except Exception:
        logger.debug("CRM-WEBHOOK: ingest feed push failed", exc_info=True)


async def read_ingest_feed(campaign_id, limit: int = _FEED_MAX) -> list[dict[str, Any]]:
    from app.services.agent_session_store import AgentSessionStore

    try:
        raw = await AgentSessionStore.client().lrange(
            _FEED_KEY.format(campaign_id=campaign_id), 0, max(0, limit - 1)
        )
        return [json.loads(x) for x in raw]
    except Exception:
        return []


# ── signed delivery (drainer + [Send Test Webhook]) ──────────────────────────
def sign(secret: str, body: bytes, ts: int) -> str:
    """Stripe-style: ``X-Nokvo-Signature: t=<unix>,v1=<hmac_sha256(secret, f"{t}.{body}")>``."""
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256)
    return f"t={ts},v1={mac.hexdigest()}"


async def post_signed(
    url: str, secret: str | None, payload: dict[str, Any]
) -> tuple[int | None, str | None]:
    """POST one signed delivery. Returns ``(status_code, error)`` — never raises."""
    body = json.dumps(payload, separators=(",", ":")).encode()
    headers = {"Content-Type": "application/json", "User-Agent": "Nokvo-Apex-Webhooks/1.0"}
    if secret:
        headers["X-Nokvo-Signature"] = sign(secret, body, int(time.time()))
    try:
        async with httpx.AsyncClient(timeout=DELIVERY_TIMEOUT_S, follow_redirects=False) as client:
            resp = await client.post(url, content=body, headers=headers)
        return resp.status_code, None
    except Exception as exc:
        return None, str(exc)[:500]
