"""Tenant diagnosis for Nova — "what's going on with my account right now".

``build_tenant_diagnosis`` assembles the org-scoped recent picture (wallet,
campaigns + per-status contact counts, recent call outcomes, distinct dial
errors) and returns TWO shapes:

  * ``llm_view``   — aggressively compacted for the prompt (<~1.5k tokens):
                     no UUIDs, no nulls, minute-resolution timestamps, calls as
                     one-liners, phones redacted to last-4. This is the ONLY
                     copy the model sees, always framed as UNTRUSTED DATA.
  * ``ticket_json`` — the full-detail operator copy (trace_ids, raw error
                     payload excerpts) persisted on the support ticket.

Every query filters by organization_id / tenant_id and is bounded (LIMIT).
Best-effort throughout: a failing block reports itself as unavailable instead
of sinking the whole diagnosis.
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tenant_resources import TenantResources

logger = logging.getLogger(__name__)

_RECENT_CALLS = 20
_RECENT_CAMPAIGNS = 3
_ERROR_SAMPLES = 5


def _redact_phone(phone: str | None) -> str:
    p = str(phone or "")
    return f"…{p[-4:]}" if len(p) >= 4 else "…"


def _ts(dt) -> str | None:
    try:
        return dt.strftime("%Y-%m-%d %H:%M") if dt else None
    except Exception:
        return None


async def _wallet(db: AsyncSession, org_id) -> dict[str, Any]:
    from app.services.minute_balance_service import apex_credit_summary

    w = await apex_credit_summary(db, org_id)
    return {
        "credits_remaining": round(w["credits_remaining"], 2),
        "estimated_minutes_remaining": w["estimated_minutes_remaining"],
    }


async def _campaigns(db: AsyncSession, tenant_id: str) -> list[dict[str, Any]]:
    from app.models.outbound_campaign import OutboundCampaign
    from app.services import campaign_contacts_v2 as v2

    rows = (await db.execute(
        select(OutboundCampaign)
        .where(OutboundCampaign.tenant_id == tenant_id)
        .order_by(OutboundCampaign.created_at.desc())
        .limit(_RECENT_CAMPAIGNS)
    )).scalars().all()
    out = []
    for c in rows:
        entry: dict[str, Any] = {
            "name": c.name,
            "status": str(getattr(c.status, "value", c.status)),
            "created": _ts(c.created_at),
            "total": c.total_count or 0,
        }
        try:
            s = await v2.summary(db, c.id)
            if s.get("total"):
                entry["counts"] = {
                    k: v for k, v in {
                        "qualified": s.get("qualified"),
                        "not_interested": s.get("not_interested"),
                        "no_pickup": s.get("no_pickup"),
                        "pending": s.get("pending"),
                    }.items() if v
                }
        except Exception:
            pass
        out.append(entry)
    return out


async def _recent_calls(db: AsyncSession, org_id) -> list[dict[str, Any]]:
    from app.models.call_cost import CallCost

    rows = (await db.execute(
        select(CallCost)
        .where(CallCost.organization_id == org_id)
        .order_by(CallCost.started_at.desc())
        .limit(_RECENT_CALLS)
    )).scalars().all()
    return [
        {
            "when": _ts(c.started_at),
            "kind": c.kind,
            "dur_s": int(float(c.duration_seconds or 0)),
            "trace_id": c.trace_id,  # stripped from the llm_view
        }
        for c in rows
    ]


async def _dial_errors(db: AsyncSession, tenant_id: str) -> list[dict[str, Any]]:
    """Distinct recent failure causes from contact snapshots (bounded scan of the
    newest failed rows via the campaign_id+status index)."""
    rows = (await db.execute(
        text(
            "SELECT occ.snapshot -> 'last_status_payload' AS payload, count(*) AS n "
            "FROM outbound_campaign_contacts occ "
            "JOIN outbound_campaigns c ON c.id = occ.campaign_id "
            "WHERE c.tenant_id = :t AND occ.status IN ('failed', 'no_answer') "
            "AND occ.snapshot ? 'last_status_payload' "
            "GROUP BY 1 ORDER BY n DESC LIMIT :lim"
        ),
        {"t": tenant_id, "lim": _ERROR_SAMPLES},
    )).all()
    out = []
    for payload, n in rows:
        p = payload or {}
        cause = str(p.get("hangup_cause") or p.get("HangupCause") or p.get("error") or "unknown")
        out.append({"cause": cause[:120], "count": int(n), "raw": p})
    return out


async def build_tenant_diagnosis(
    db: AsyncSession, tenant_res: TenantResources, organization_id
) -> dict[str, Any]:
    """→ {llm_view, ticket_json}. Never raises."""
    blocks: dict[str, Any] = {}
    for name, coro in (
        ("wallet", _wallet(db, organization_id)),
        ("campaigns", _campaigns(db, tenant_res.tenant_id)),
        ("recent_calls", _recent_calls(db, organization_id)),
        ("dial_errors", _dial_errors(db, tenant_res.tenant_id)),
    ):
        try:
            blocks[name] = await coro
        except Exception:
            logger.exception("NOVA-DIAG: %s block failed", name)
            blocks[name] = {"unavailable": True}

    # Compact LLM view: drop ids/raw payloads, collapse calls to one-liners.
    calls = blocks.get("recent_calls") or []
    llm_calls = [
        {k: v for k, v in c.items() if k != "trace_id" and v not in (None, 0, "")}
        for c in (calls if isinstance(calls, list) else [])
    ]
    errors = blocks.get("dial_errors") or []
    llm_errors = [
        {"cause": e["cause"], "count": e["count"]}
        for e in (errors if isinstance(errors, list) else [])
    ]
    llm_view = {
        "wallet": blocks.get("wallet"),
        "campaigns": blocks.get("campaigns"),
        "recent_calls": llm_calls,
        "dial_error_causes": llm_errors,
    }
    ticket_json = blocks
    return {"llm_view": llm_view, "ticket_json": ticket_json}
