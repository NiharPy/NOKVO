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
                        "busy": s.get("busy"),  # answered, asked to be called back
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


# ─────────────────────── campaign performance (Nova analyst) ──────────────────

_PERFORMANCE_CAMPAIGNS = 5
_LOW_MINUTES_WARNING = 30


async def _campaign_stats(db: AsyncSession, tenant_id: str, limit: int) -> list[dict[str, Any]]:
    """Recent campaigns with per-bucket counts AND computed rates. All the math
    happens HERE — the model narrates numbers, it never derives them (same
    philosophy as the lead scorer's server-side point summing)."""
    from app.models.outbound_campaign import OutboundCampaign
    from app.services import campaign_contacts_v2 as v2

    rows = (await db.execute(
        select(OutboundCampaign)
        .where(OutboundCampaign.tenant_id == tenant_id)
        .order_by(OutboundCampaign.created_at.desc())
        .limit(limit)
    )).scalars().all()
    out: list[dict[str, Any]] = []
    for c in rows:
        entry: dict[str, Any] = {
            "name": c.name,
            "status": str(getattr(c.status, "value", c.status)),
            "created": _ts(c.created_at),
            "total": c.total_count or 0,
        }
        try:
            s = await v2.summary(db, c.id)
        except Exception:
            out.append(entry)
            continue
        qualified = int(s.get("qualified") or 0)
        not_interested = int(s.get("not_interested") or 0)
        busy = int(s.get("busy") or 0)
        no_pickup = int(s.get("no_pickup") or 0)
        pending = int(s.get("pending") or 0)
        dialed = qualified + not_interested + busy + no_pickup
        entry["counts"] = {
            "qualified": qualified, "not_interested": not_interested,
            "busy": busy, "no_pickup": no_pickup, "pending": pending,
        }
        if dialed:
            connected = qualified + not_interested + busy
            entry["rates_pct"] = {
                "qualified": round(qualified / dialed * 100, 1),
                "connected": round(connected / dialed * 100, 1),
                "no_pickup": round(no_pickup / dialed * 100, 1),
                "busy": round(busy / dialed * 100, 1),
            }
        window = (c.agent_config or {}).get("call_window") or {}
        if window.get("working_days"):
            entry["schedule"] = {k: window[k] for k in ("working_days", "calls_per_day") if window.get(k)}
        out.append(entry)
    return out


def _performance_recommendations(campaigns: list[dict[str, Any]], wallet: dict[str, Any]) -> list[str]:
    """Deterministic, numbers-in-place recommendations Nova relays to the user."""
    recs: list[str] = []
    for c in campaigns:
        counts = c.get("counts") or {}
        if c.get("status") == "cancelled" and counts.get("pending"):
            recs.append(
                f"'{c['name']}' is stopped with {counts['pending']} contacts still pending — "
                "Resume it to finish the list."
            )
        if counts.get("no_pickup"):
            recs.append(
                f"Re-run '{c['name']}' for the {counts['no_pickup']} contacts who didn't pick up — "
                "unanswered retries cost nothing."
            )
        if counts.get("busy"):
            recs.append(
                f"{counts['busy']} contact(s) in '{c['name']}' asked to be called back — they're on "
                "the Busy tab; use 'Call busy' to re-dial them (connected retries consume credits)."
            )
    # Best performer worth cloning: completed, meaningfully sampled, top qualified rate.
    scored = [
        c for c in campaigns
        if c.get("status") == "completed" and (c.get("rates_pct") or {}).get("qualified") is not None
        and sum((c.get("counts") or {}).values()) - (c.get("counts") or {}).get("pending", 0) >= 10
    ]
    if scored:
        best = max(scored, key=lambda c: c["rates_pct"]["qualified"])
        if best["rates_pct"]["qualified"] > 0:
            recs.append(
                f"Your best performer is '{best['name']}' ({best['rates_pct']['qualified']}% qualified) — "
                "the Duplicate button on its campaign card reuses the same script and questions for a new list."
            )
    minutes = (wallet or {}).get("estimated_minutes_remaining")
    if isinstance(minutes, (int, float)) and minutes < _LOW_MINUTES_WARNING:
        recs.append(
            f"Call Credits are low (about {int(minutes)} minutes left) — top up before the next run."
        )
    return recs[:6]


async def build_campaign_performance(
    db: AsyncSession, tenant_res: TenantResources, organization_id
) -> dict[str, Any]:
    """→ campaign stats + rates + deterministic recommendations for the
    get_campaign_performance tool. Never raises."""
    try:
        wallet = await _wallet(db, organization_id)
    except Exception:
        wallet = {}
    try:
        campaigns = await _campaign_stats(db, tenant_res.tenant_id, _PERFORMANCE_CAMPAIGNS)
    except Exception:
        logger.exception("NOVA-PERF: campaign stats failed")
        return {"campaigns": [], "note": "Performance data is unavailable right now — try again shortly."}
    return {
        "wallet": wallet,
        "campaigns": campaigns,
        "recommendations": _performance_recommendations(campaigns, wallet),
        "note": (
            "Rates are % of contacts dialed so far. Relay the recommendations that fit "
            "what the user asked; quote the numbers as given."
        ),
    }


# ─────────────────────── panel-open briefing (no LLM) ────────────────────────

async def build_briefing(
    db: AsyncSession, tenant_res: TenantResources, organization_id, role: str | None
) -> dict[str, Any]:
    """Deterministic highlights for the panel-open greeting — composed strings,
    zero LLM cost, safe to call on every open. Members get none (campaigns are
    admin domain). Never raises."""
    if (role or "") != "admin":
        return {"highlights": []}
    highlights: list[str] = []
    try:
        campaigns = await _campaign_stats(db, tenant_res.tenant_id, _PERFORMANCE_CAMPAIGNS)
    except Exception:
        logger.exception("NOVA-BRIEF: campaign stats failed")
        campaigns = []
    running = next((c for c in campaigns if c.get("status") in ("running", "ingesting")), None)
    if running:
        counts = running.get("counts") or {}
        done = sum(counts.values()) - counts.get("pending", 0)
        total = running.get("total") or (done + counts.get("pending", 0))
        bit = f"'{running['name']}' is dialing — {done} of {total} contacts done"
        if counts.get("qualified"):
            bit += f", {counts['qualified']} qualified so far"
        highlights.append(bit + ".")
    else:
        done_recent = next((c for c in campaigns if c.get("status") == "completed"), None)
        if done_recent and done_recent.get("counts"):
            k = done_recent["counts"]
            highlights.append(
                f"'{done_recent['name']}' finished: {k.get('qualified', 0)} qualified, "
                f"{k.get('busy', 0)} callback request(s), {k.get('no_pickup', 0)} didn't pick up."
            )
    busy_total = sum((c.get("counts") or {}).get("busy", 0) for c in campaigns)
    if busy_total:
        highlights.append(f"{busy_total} contact(s) asked to be called back — they're on the Busy tab.")
    try:
        wallet = await _wallet(db, organization_id)
        minutes = wallet.get("estimated_minutes_remaining")
        if isinstance(minutes, (int, float)) and minutes < _LOW_MINUTES_WARNING:
            highlights.append(f"Call Credits are low — about {int(minutes)} minutes left.")
    except Exception:
        pass
    return {"highlights": highlights[:3]}
