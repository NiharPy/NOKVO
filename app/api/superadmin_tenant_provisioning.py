"""SuperAdmin console API — view every org + per-call cost + plan upgrade.

This router is the rebuilt SuperAdmin console surface. Organizations now
self-serve through Razorpay payment-gated onboarding, so the old operator
workflows (manual Azure provisioning, org approval, WhatsApp concierge,
hand-entered usage events) are gone. What remains is what an operator actually
needs:

  * ``GET  /superadmin/tenants``            — every org with real minutes used,
    revenue (subscription + post-paid usage), COGS, and margin.
  * ``GET  /superadmin/tenants/{org_id}``   — drill-down: the per-call
    STT/LLM/TTS/Plivo cost breakdown + rollups.
  * ``POST /superadmin/tenants/{org_id}/upgrade`` — flip an org between the
    Inbound-only and Inbound+Outbound plans (capability only — no Razorpay
    billing change).

Auth is unchanged: every endpoint is gated to SuperAdmin roles via
:class:`app.api.deps.RequireRole`.

Money model
-----------
* **Revenue** = active monthly Razorpay subscription (``Subscription.amount_paise``)
  + cumulative post-paid usage billed to the tenant (``CallCost.rupees``, the
  tiered tariff). Subscription is a monthly figure; usage is all-time — both are
  surfaced separately so the console can label them.
* **COGS** = what Nokvo pays vendors per call (``CallCost.cost_total_inr``, the
  STT+LLM+TTS+Plivo breakdown). NULL on calls recorded before instrumentation.
* **Margin** = revenue − COGS.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequireRole
from app.db.session import get_db
from app.models.audit import SuperAdminAuditLog
from app.models.call_cost import CallCost
from app.models.organization import Organization
from app.models.subscription import Subscription
from app.models.tenant_resources import TenantResources
from app.models.user import SuperAdminUser
from app.services.razorpay_service import PLAN_CATALOG

router = APIRouter()

# SuperAdmin roles allowed to read the console. Mutations narrow this further.
_READ_ROLES = ["founder", "engineering", "billing", "readonly"]
_WRITE_ROLES = ["founder", "engineering"]


def _month_start_utc() -> datetime:
    now = datetime.now(timezone.utc)
    return datetime(now.year, now.month, 1, tzinfo=timezone.utc)


def _inr(value) -> float:
    """Coerce a Decimal/None ledger value to a 2-dp rupee float for the API."""
    if value is None:
        return 0.0
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _minutes(seconds) -> float:
    if not seconds:
        return 0.0
    return round(float(Decimal(str(seconds)) / Decimal("60")), 2)


# ── CallCost aggregation ─────────────────────────────────────────────────────
# One grouped query per time-window returns the per-org rollup the console
# needs. Keyed by organization_id (str) → dict of sums.

def _callcost_select(since: datetime | None):
    stmt = select(
        CallCost.organization_id,
        func.coalesce(func.sum(CallCost.duration_seconds), 0),
        func.coalesce(func.sum(CallCost.rupees), 0),
        func.coalesce(func.sum(CallCost.cost_total_inr), 0),
        func.coalesce(func.sum(CallCost.cost_stt_inr), 0),
        func.coalesce(func.sum(CallCost.cost_llm_inr), 0),
        func.coalesce(func.sum(CallCost.cost_tts_inr), 0),
        func.coalesce(func.sum(CallCost.cost_telephony_inr), 0),
        func.count(CallCost.id),
    ).group_by(CallCost.organization_id)
    if since is not None:
        stmt = stmt.where(CallCost.started_at >= since)
    return stmt


async def _callcost_rollup(db: AsyncSession, since: datetime | None) -> dict[str, dict]:
    rows = await db.execute(_callcost_select(since))
    out: dict[str, dict] = {}
    for org_id, secs, rupees, cogs_total, cogs_stt, cogs_llm, cogs_tts, cogs_tel, count in rows.all():
        out[str(org_id)] = {
            "minutes": _minutes(secs),
            "usage_revenue_inr": _inr(rupees),
            "cogs_inr": _inr(cogs_total),
            "cogs_stt_inr": _inr(cogs_stt),
            "cogs_llm_inr": _inr(cogs_llm),
            "cogs_tts_inr": _inr(cogs_tts),
            "cogs_telephony_inr": _inr(cogs_tel),
            "call_count": int(count or 0),
        }
    return out


async def _subscription_map(db: AsyncSession) -> dict[str, dict]:
    """Per-org active subscription: monthly amount + plan. An org should have
    at most one active sub, but we sum defensively in case of overlap."""
    rows = await db.execute(
        select(Subscription.organization_id, Subscription.plan, Subscription.amount_paise)
        .where(Subscription.status == "active")
    )
    out: dict[str, dict] = {}
    for org_id, plan, amount_paise in rows.all():
        entry = out.setdefault(str(org_id), {"plan": plan, "monthly_inr": 0.0})
        entry["monthly_inr"] = round(entry["monthly_inr"] + (int(amount_paise or 0) / 100.0), 2)
        if plan:
            entry["plan"] = plan
    return out


def _plan_label(plan: str | None) -> str:
    if plan and plan in PLAN_CATALOG:
        return PLAN_CATALOG[plan]["label"]
    return "—"


@router.get("")
async def list_tenants(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    """Every organization with minutes used, revenue, COGS, and margin."""
    org_rows = await db.execute(select(Organization).order_by(Organization.created_at.desc()))
    organizations = org_rows.scalars().all()

    tenant_rows = await db.execute(
        select(TenantResources.organization_id, TenantResources.tenant_id, TenantResources.provisioning_status)
    )
    tenant_map = {str(r[0]): {"tenant_id": r[1], "provisioning_status": r[2]} for r in tenant_rows.all()}

    all_time = await _callcost_rollup(db, None)
    mtd = await _callcost_rollup(db, _month_start_utc())
    subs = await _subscription_map(db)

    items = []
    tot_minutes = tot_revenue = tot_cogs = tot_margin = 0.0
    for org in organizations:
        oid = str(org.id)
        at = all_time.get(oid, {})
        mt = mtd.get(oid, {})
        sub = subs.get(oid, {})

        subscription_inr = sub.get("monthly_inr", 0.0)
        usage_inr = at.get("usage_revenue_inr", 0.0)
        revenue_inr = round(subscription_inr + usage_inr, 2)
        cogs_inr = at.get("cogs_inr", 0.0)
        margin_inr = round(revenue_inr - cogs_inr, 2)

        tenant = tenant_map.get(oid, {})
        items.append({
            "organization_id": oid,
            "organization_name": org.name,
            "admin_email": org.admin_email,
            "status": org.status,
            "plan_type": org.plan_type,
            "plan_label": _plan_label(org.plan_type),
            "calling_enabled": bool(org.calling_enabled),
            "region": org.region,
            "created_at": org.created_at,
            "tenant_id": tenant.get("tenant_id"),
            "provisioning_status": tenant.get("provisioning_status") or "pending",
            "minutes_used": at.get("minutes", 0.0),
            "minutes_used_mtd": mt.get("minutes", 0.0),
            "call_count": at.get("call_count", 0),
            "revenue": {
                "subscription_monthly_inr": subscription_inr,
                "usage_inr": usage_inr,
                "total_inr": revenue_inr,
            },
            "cogs_inr": cogs_inr,
            "cogs_mtd_inr": mt.get("cogs_inr", 0.0),
            "margin_inr": margin_inr,
        })
        tot_minutes += at.get("minutes", 0.0)
        tot_revenue += revenue_inr
        tot_cogs += cogs_inr
        tot_margin += margin_inr

    return {
        "organizations": items,
        "summary": {
            "count": len(items),
            "total_minutes": round(tot_minutes, 2),
            "total_revenue_inr": round(tot_revenue, 2),
            "total_cogs_inr": round(tot_cogs, 2),
            "total_margin_inr": round(tot_margin, 2),
        },
    }


def _call_row(cc: CallCost) -> dict:
    """Serialize one CallCost row with its STT/LLM/TTS/Plivo breakdown."""
    return {
        "call_id": cc.call_id,
        "kind": cc.kind,
        "started_at": cc.started_at,
        "ended_at": cc.ended_at,
        "duration_seconds": float(cc.duration_seconds or 0),
        "minutes": _minutes(cc.duration_seconds),
        # Revenue billed to the tenant (tiered tariff).
        "revenue_inr": _inr(cc.rupees),
        # COGS — NULL on pre-instrumentation calls (rendered as total-only).
        "instrumented": cc.cost_total_inr is not None,
        "cost_stt_inr": _inr(cc.cost_stt_inr),
        "cost_llm_inr": _inr(cc.cost_llm_inr),
        "cost_tts_inr": _inr(cc.cost_tts_inr),
        "cost_telephony_inr": _inr(cc.cost_telephony_inr),
        "cost_total_inr": _inr(cc.cost_total_inr),
        "llm_input_tokens": cc.llm_input_tokens,
        "llm_output_tokens": cc.llm_output_tokens,
        "llm_cached_tokens": cc.llm_cached_tokens,
        "stt_seconds": float(cc.stt_seconds) if cc.stt_seconds is not None else None,
        "tts_characters": cc.tts_characters,
        "trace_id": cc.trace_id,
    }


@router.get("/{organization_id}")
async def get_tenant_detail(
    organization_id: str,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    """Drill-down for one org: per-call STT/LLM/TTS/Plivo breakdown + rollups."""
    org = (
        await db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    oid = str(org.id)

    at = (await _callcost_rollup(db, None)).get(oid, {})
    mt = (await _callcost_rollup(db, _month_start_utc())).get(oid, {})
    sub = (await _subscription_map(db)).get(oid, {})

    recent_rows = await db.execute(
        select(CallCost)
        .where(CallCost.organization_id == oid)
        .order_by(CallCost.started_at.desc())
        .limit(50)
    )
    recent = [_call_row(cc) for cc in recent_rows.scalars().all()]

    subscription_inr = sub.get("monthly_inr", 0.0)
    usage_inr = at.get("usage_revenue_inr", 0.0)
    revenue_inr = round(subscription_inr + usage_inr, 2)
    cogs_inr = at.get("cogs_inr", 0.0)

    return {
        "organization_id": oid,
        "organization_name": org.name,
        "admin_email": org.admin_email,
        "admin_name": org.admin_name,
        "status": org.status,
        "plan_type": org.plan_type,
        "plan_label": _plan_label(org.plan_type),
        "calling_enabled": bool(org.calling_enabled),
        "region": org.region,
        "created_at": org.created_at,
        "subscription": {
            "plan": sub.get("plan"),
            "plan_label": _plan_label(sub.get("plan")),
            "monthly_inr": subscription_inr,
        },
        "totals": {
            "minutes_used": at.get("minutes", 0.0),
            "minutes_used_mtd": mt.get("minutes", 0.0),
            "call_count": at.get("call_count", 0),
            "revenue": {
                "subscription_monthly_inr": subscription_inr,
                "usage_inr": usage_inr,
                "total_inr": revenue_inr,
            },
            "cogs": {
                "total_inr": cogs_inr,
                "stt_inr": at.get("cogs_stt_inr", 0.0),
                "llm_inr": at.get("cogs_llm_inr", 0.0),
                "tts_inr": at.get("cogs_tts_inr", 0.0),
                "telephony_inr": at.get("cogs_telephony_inr", 0.0),
                "mtd_total_inr": mt.get("cogs_inr", 0.0),
            },
            "margin_inr": round(revenue_inr - cogs_inr, 2),
        },
        "recent_calls": recent,
    }


class PlanChangePayload(BaseModel):
    plan: str  # "inbound_only" | "inbound_outbound"


@router.post("/{organization_id}/upgrade")
async def change_organization_plan(
    organization_id: str,
    payload: PlanChangePayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Switch an org between Inbound-only and Inbound+Outbound (capability only).

    Sets ``calling_enabled`` + ``plan_type`` from the plan's catalog entry; does
    NOT touch the Razorpay subscription (a manual grant). Mirrors the plan
    application in ``nokvo_one_payments.activate_and_provision``. Use plan
    ``inbound_only`` to downgrade (revoke outbound).
    """
    if payload.plan not in PLAN_CATALOG:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown plan '{payload.plan}'. Valid: {', '.join(PLAN_CATALOG)}",
        )

    org = (
        await db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    before = {"plan_type": org.plan_type, "calling_enabled": bool(org.calling_enabled)}

    org.plan_type = payload.plan
    org.calling_enabled = bool(PLAN_CATALOG[payload.plan]["outbound"])
    after = {"plan_type": org.plan_type, "calling_enabled": bool(org.calling_enabled)}
    db.add(org)

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="organization_plan_changed",
            risk_level="medium",
            target_type="organization",
            target_id=str(org.id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            before_state=before,
            after_state=after,
            metadata_={"plan": payload.plan, "label": PLAN_CATALOG[payload.plan]["label"]},
        )
    )
    await db.commit()

    return {
        "organization_id": str(org.id),
        "plan_type": org.plan_type,
        "plan_label": _plan_label(org.plan_type),
        "calling_enabled": bool(org.calling_enabled),
    }
