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

import uuid
from datetime import datetime, timezone
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import RequireRole
from app.core.config import settings
from app.db.session import get_db
from app.models.audit import SuperAdminAuditLog
from app.models.call_cost import CallCost
from app.core.secret_crypto import encrypt_secret
from app.models.feedback import TenantFeedback
from app.models.llm_pool_key import LlmPoolKey
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.superadmin_todo import SuperAdminTodo
from app.models.subscription import Subscription
from app.models.tenant_resources import TenantResources
from app.models.user import SuperAdminUser
from app.services.apex_plans import TRIAL_MAX_CONCURRENCY
from app.services.email_service import EmailService
from app.services.langsmith_diagnostics import LangSmithDiagnostics
from app.services.llm_pool import LLMPool
from app.services.plivo_service import PlivoService
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
        func.coalesce(func.sum(CallCost.billed_minutes), 0),
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
    rate = float(settings.COGS_PER_MINUTE_INR or 0)
    for org_id, billed_minutes, rupees, cogs_total, cogs_stt, cogs_llm, cogs_tts, cogs_tel, count in rows.all():
        minutes = int(billed_minutes or 0)
        out[str(org_id)] = {
            "minutes": minutes,
            "usage_revenue_inr": _inr(rupees),
            # COGS = flat blended rate × billed minutes (complete + consistent;
            # margin = revenue − this). The actual per-component vendor costs are
            # surfaced alongside for detail but can be NULL on older calls.
            "cogs_inr": round(rate * minutes, 2),
            "cogs_actual_inr": _inr(cogs_total),
            "cogs_per_minute_inr": rate,
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

    # APEX balances for the apex orgs, in one pass. Read the LIVE Call Credits wallet
    # (rupee-valued) — NOT the dead Phase-3 minutes ledger — and surface its estimated
    # minutes as ``remaining_minutes`` so the console shows the real spendable balance
    # (incl. the 50% bonus on paid bundles).
    from app.services.minute_balance_service import apex_credit_summary
    apex_balances: dict[str, dict] = {}
    for _org in organizations:
        if (_org.product_tier or "") == "nokvo_apex":
            _s = await apex_credit_summary(db, _org.id)
            apex_balances[str(_org.id)] = {
                "remaining_minutes": _s["estimated_minutes_remaining"],
                "credits_remaining": _s["credits_remaining"],
                "credits_used": _s["credits_used"],
            }

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
            "product_tier": org.product_tier,  # "nokvo_one" | "nokvo_apex" | "nokvo_prime"
            "is_apex": (org.product_tier or "") == "nokvo_apex",
            "apex_minutes": apex_balances.get(oid),  # {credited,used,remaining} for apex; else None
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
        # ₹0 visibility counters: completion count + TTS byte-cache efficiency.
        "llm_requests": cc.llm_requests,
        "tts_cache_hits": cc.tts_cache_hits,
        "tts_cache_chars": cc.tts_cache_chars,
        "trace_id": cc.trace_id,
    }


# NOTE: defined BEFORE the "/{organization_id}" route below so FastAPI doesn't
# treat "feedback" as an organization id.
@router.get("/feedback")
async def list_feedback(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
    limit: int = 500,
):
    """All tenant-submitted feedback / feature requests, newest first."""
    limit = max(1, min(int(limit or 500), 2000))
    rows = await db.execute(
        select(TenantFeedback, Organization.name, OrganizationUser.email, Organization.product_tier)
        .join(Organization, Organization.id == TenantFeedback.organization_id, isouter=True)
        .join(OrganizationUser, OrganizationUser.id == TenantFeedback.submitted_by_user_id, isouter=True)
        .order_by(TenantFeedback.created_at.desc())
        .limit(limit)
    )
    items = []
    for fb, org_name, user_email, product_tier in rows.all():
        items.append({
            "id": str(fb.id),
            "organization_id": str(fb.organization_id),
            "organization_name": org_name or "—",
            "submitted_by_email": user_email or "—",
            "category": fb.category,
            "message": fb.message,
            "status": fb.status,
            "product_tier": product_tier,
            "is_apex": (product_tier or "") == "nokvo_apex",
            "created_at": fb.created_at.isoformat() if fb.created_at else None,
        })
    return {"items": items}


# ── APEX support tickets (raised in-product via Nova) ────────────────────────
# Defined BEFORE "/{organization_id}" so the path word isn't treated as an id.

class ApexTicketUpdateRequest(BaseModel):
    status: str  # 'open' | 'in_progress' | 'resolved'
    resolution_note: str | None = None


@router.get("/apex-tickets")
async def list_apex_tickets(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
    limit: int = 500,
):
    """APEX support tickets, newest first, with the Nova-attached diagnosis."""
    from app.models.apex_support_ticket import ApexSupportTicket

    limit = max(1, min(int(limit or 500), 2000))
    rows = await db.execute(
        select(ApexSupportTicket, Organization.name)
        .join(Organization, Organization.id == ApexSupportTicket.organization_id, isouter=True)
        .order_by(ApexSupportTicket.created_at.desc())
        .limit(limit)
    )
    items = []
    for t, org_name in rows.all():
        items.append({
            "id": str(t.id),
            "organization_id": str(t.organization_id),
            "organization_name": org_name or "—",
            "requested_by_email": t.requested_by_email or "—",
            "subject": t.subject,
            "description": t.description,
            "diagnosis": t.diagnosis,
            "status": t.status,
            "created_at": t.created_at.isoformat() if t.created_at else None,
            "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            "resolution_note": t.resolution_note,
        })
    return {"items": items}


@router.patch("/apex-tickets/{ticket_id}")
async def update_apex_ticket(
    ticket_id: uuid.UUID,
    payload: ApexTicketUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    from app.models.apex_support_ticket import APEX_TICKET_STATUSES, ApexSupportTicket

    status_value = (payload.status or "").strip().lower()
    if status_value not in APEX_TICKET_STATUSES:
        raise HTTPException(status_code=400, detail=f"status must be one of {sorted(APEX_TICKET_STATUSES)}")
    ticket = (
        await db.execute(select(ApexSupportTicket).where(ApexSupportTicket.id == ticket_id))
    ).scalars().first()
    if ticket is None:
        raise HTTPException(status_code=404, detail="Ticket not found")
    ticket.status = status_value
    if status_value == "resolved":
        ticket.resolved_at = datetime.now(timezone.utc)
        if payload.resolution_note:
            ticket.resolution_note = payload.resolution_note.strip()[:4000]
    else:
        ticket.resolved_at = None
    db.add(ticket)
    await db.commit()
    return {"id": str(ticket.id), "status": ticket.status}


# ── NOKVO APEX plan accounts: access requests + create + activate ────────────
# APEX has no self-serve signup — prospects submit an access request (public form) and
# SuperAdmin creates the account here (stamps the plan, provisions except numbers, emails
# the Razorpay payment link), then manually activates once paid. Gated on ENABLE_APEX_PLANS.

def _require_apex_plans_enabled() -> None:
    if not settings.ENABLE_APEX_PLANS:
        raise HTTPException(status_code=404, detail="APEX plans are not enabled")


class ApexCreateAccountRequest(BaseModel):
    plan_code: str
    company_name: str
    admin_email: str
    admin_name: str | None = None
    phone: str | None = None
    admin_password: str | None = None
    # Required only for the enterprise plan (negotiated per deal).
    enterprise_rate: float | None = None
    enterprise_concurrency: int | None = None
    # Optional Free Trial concurrency override; omit to keep the plan default of 1.
    trial_concurrency: int | None = Field(default=None, ge=1, le=TRIAL_MAX_CONCURRENCY)
    # Free minutes granted on top of the plan's own credit. Never billed. Capped so a
    # slipped digit can't gift a fortune (the wallet is real money at the org's rate).
    complimentary_minutes: int = Field(default=0, ge=0, le=100_000)
    # Optional link back to the access request being converted.
    request_id: uuid.UUID | None = None


@router.get("/apex/access-requests")
async def list_apex_access_requests(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Public APEX access requests, newest first. Optional ``status`` filter."""
    _require_apex_plans_enabled()
    from app.models.apex_access_request import ApexAccessRequest

    q = select(ApexAccessRequest).order_by(ApexAccessRequest.created_at.desc())
    if status:
        q = q.where(ApexAccessRequest.status == status.strip().lower())
    rows = (await db.execute(q.limit(500))).scalars().all()
    return [
        {
            "id": str(r.id),
            "company_name": r.company_name,
            "contact_name": r.contact_name,
            "email": r.email,
            "phone": r.phone,
            "requested_plan": r.requested_plan,
            "notes": r.notes,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "converted_org_id": str(r.converted_org_id) if r.converted_org_id else None,
        }
        for r in rows
    ]


@router.post("/apex/accounts")
async def create_apex_account_endpoint(
    payload: ApexCreateAccountRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Create an APEX account on a plan, provision (except numbers), and email the payment
    link. High-risk: opens a recurring subscription + provisions resources."""
    _require_apex_plans_enabled()
    from app.services.apex_account_service import ApexAccountError, create_apex_account

    email = (payload.admin_email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="A valid admin_email is required")
    try:
        result = await create_apex_account(
            db,
            plan_code=payload.plan_code,
            company_name=payload.company_name,
            admin_email=email,
            admin_name=payload.admin_name,
            phone=payload.phone,
            admin_password=payload.admin_password,
            enterprise_rate=payload.enterprise_rate,
            enterprise_concurrency=payload.enterprise_concurrency,
            trial_concurrency=payload.trial_concurrency,
            complimentary_minutes=payload.complimentary_minutes,
            request_id=payload.request_id,
        )
    except ApexAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="apex_account_created",
            risk_level="high",
            target_type="organization",
            target_id=result["organization_id"],
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            metadata_={
                "plan_code": result["plan_code"],
                "status": result["status"],
                # Gifted minutes are real wallet money — keep them on the audit record.
                "complimentary_minutes": result.get("complimentary_minutes", 0),
            },
        )
    )
    await db.commit()
    return result


@router.post("/apex/accounts/{organization_id}/activate")
async def activate_apex_account_endpoint(
    organization_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Manually activate a paid APEX account (→ active, provisions numbers best-effort)."""
    _require_apex_plans_enabled()
    from app.services.apex_account_service import ApexAccountError, activate_apex_account

    try:
        result = await activate_apex_account(db, organization_id)
    except ApexAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="apex_account_activated",
            risk_level="medium",
            target_type="organization",
            target_id=str(organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            metadata_={"numbers_provisioned": result.get("numbers_provisioned")},
        )
    )
    await db.commit()
    return result


class ApexChangePlanRequest(BaseModel):
    plan_code: str
    # Required only when moving TO the enterprise plan (negotiated per deal).
    enterprise_rate: float | None = None
    enterprise_concurrency: int | None = None
    # Optional Free Trial concurrency override; omit to keep the plan default of 1.
    trial_concurrency: int | None = Field(default=None, ge=1, le=TRIAL_MAX_CONCURRENCY)
    # Replace the Razorpay mandate when the monthly amount changes (open the new
    # subscription + email the link, cancel the old one). False re-stamps the plan only.
    rebill: bool = True
    # The operator must confirm they understand a re-bill charges the customer a new amount.
    acknowledge: bool = False


@router.get("/apex/plans")
async def apex_plan_catalog_for_console(
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    """The APEX plan catalog, for the console's plan picker (same data as the public
    endpoint — served here so the console needs no unauthenticated call)."""
    _require_apex_plans_enabled()
    from app.services.apex_plans import APEX_PLANS, plan_public_view

    return {"plans": [plan_public_view(p) for p in APEX_PLANS.values()]}


@router.post("/apex/accounts/{organization_id}/plan")
async def change_apex_account_plan(
    organization_id: uuid.UUID,
    payload: ApexChangePlanRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Upgrade / downgrade an APEX account's plan (or re-stamp the same plan to repair a
    drifted row). Re-stamps rate, concurrency, included minutes, platform fee, bonuses and
    support tier; the wallet is never touched. High-risk: with ``rebill`` it cancels the
    live Razorpay subscription and opens one at the new monthly amount."""
    _require_apex_plans_enabled()
    from app.services.apex_account_service import ApexAccountError, change_apex_plan

    if not payload.acknowledge:
        raise HTTPException(
            status_code=400,
            detail="Confirm the plan change (acknowledge=true) — it re-prices the account.",
        )
    try:
        result = await change_apex_plan(
            db,
            organization_id,
            plan_code=payload.plan_code,
            enterprise_rate=payload.enterprise_rate,
            enterprise_concurrency=payload.enterprise_concurrency,
            trial_concurrency=payload.trial_concurrency,
            rebill=payload.rebill,
        )
    except ApexAccountError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="apex_plan_changed",
            risk_level="high",
            target_type="organization",
            target_id=str(organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            metadata_={
                "from": result["before"],
                "to": result["after"],
                "rebilled": result["rebilled"],
                "monthly_inr": result["monthly_inr"],
                "previous_monthly_inr": result["previous_monthly_inr"],
                # Surfaced on the audit trail too: an uncancelled old subscription
                # double-bills the customer until someone kills it by hand.
                "old_subscription_cancel_error": result.get("old_subscription_cancel_error"),
            },
        )
    )
    await db.commit()
    return result


@router.get("/apex/webhook-events")
async def list_razorpay_webhook_events(
    status: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Recent Razorpay webhook events (audit) — newest first, optional status filter, so a
    dropped/failed payment event can be found and replayed."""
    _require_apex_plans_enabled()
    from app.models.razorpay_webhook_event import RazorpayWebhookEvent

    q = select(RazorpayWebhookEvent).order_by(RazorpayWebhookEvent.created_at.desc())
    if status:
        q = q.where(RazorpayWebhookEvent.status == status.strip().lower())
    rows = (await db.execute(q.limit(200))).scalars().all()
    return [
        {
            "id": str(r.id),
            "event_type": r.event_type,
            "subscription_id": r.subscription_id,
            "payment_id": r.payment_id,
            "invoice_id": r.invoice_id,
            "status": r.status,
            "error": r.error,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


@router.post("/apex/webhook-events/{event_id}/resync")
async def resync_razorpay_webhook_event(
    event_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Replay a stored webhook event's activation (idempotent downstream). Use when a
    payment event failed to credit/advance an account."""
    _require_apex_plans_enabled()
    from app.models.razorpay_webhook_event import RazorpayWebhookEvent
    from app.api.nokvo_one_payments import _bg_activate

    ev = (
        await db.execute(select(RazorpayWebhookEvent).where(RazorpayWebhookEvent.id == event_id))
    ).scalars().first()
    if ev is None:
        raise HTTPException(status_code=404, detail="Webhook event not found")
    if not ev.subscription_id:
        raise HTTPException(status_code=400, detail="Event has no subscription id to replay")
    sub = (
        await db.execute(select(Subscription).where(Subscription.razorpay_subscription_id == ev.subscription_id))
    ).scalars().first()
    if sub is None:
        raise HTTPException(status_code=404, detail="No subscription found for this event")
    try:
        await _bg_activate(sub.organization_id, ev.subscription_id, ev.invoice_id)
        ev.status = "processed"
        ev.processed_at = datetime.now(timezone.utc)
        ev.error = None
    except Exception as exc:  # keep the audit row; surface the failure
        ev.status = "failed"
        ev.error = str(exc)[:2000]
        db.add(ev)
        await db.commit()
        raise HTTPException(status_code=500, detail="Resync failed — see event error") from exc
    db.add(ev)
    await db.commit()
    return {"id": str(ev.id), "status": ev.status}


# ── SuperAdmin to-do list (optionally tagged to a feedback row) ──────────────
# All defined BEFORE "/{organization_id}" so the path words aren't treated as ids.

def _todo_dict(t: SuperAdminTodo, fb: TenantFeedback | None = None) -> dict:
    out = {
        "id": str(t.id),
        "title": t.title,
        "notes": t.notes,
        "status": t.status,
        "feedback_id": str(t.feedback_id) if t.feedback_id else None,
        "created_at": t.created_at.isoformat() if t.created_at else None,
        "completed_at": t.completed_at.isoformat() if t.completed_at else None,
        "feedback": None,
    }
    if fb is not None:
        out["feedback"] = {"id": str(fb.id), "category": fb.category, "message": fb.message}
    return out


class TodoCreateRequest(BaseModel):
    title: str
    notes: str | None = None
    feedback_id: str | None = None


class TodoUpdateRequest(BaseModel):
    title: str | None = None
    notes: str | None = None
    status: str | None = None  # 'open' | 'done'


# ── USD→INR FX rate (per-call COGS pricing) ──────────────────────────────────
# NOTE: like /feedback, these are defined BEFORE the "/{organization_id}" route
# so FastAPI doesn't read "fx-rate" as an organization id.


class FxRateRequest(BaseModel):
    usd_to_inr: float


@router.get("/fx-rate")
async def get_fx_rate(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    """The USD→INR rate every per-call COGS calculation converts vendor USD
    list prices with, plus override metadata."""
    from app.services.platform_settings import get_usd_to_inr

    return await get_usd_to_inr(db)


@router.put("/fx-rate")
async def set_fx_rate(
    payload: FxRateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Change the platform FX rate. Applies to THIS instance immediately and
    reaches every other replica via its refresher within ~a minute; historical
    call_costs rows keep the rate they were priced at (ledger freezes per row)."""
    from app.services.platform_settings import set_usd_to_inr

    try:
        return await set_usd_to_inr(db, payload.usd_to_inr, updated_by=current_user.email)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/fx-rate")
async def clear_fx_rate(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Remove the override — pricing returns to the config default."""
    from app.services.platform_settings import clear_usd_to_inr

    return await clear_usd_to_inr(db, updated_by=current_user.email)


@router.get("/todos")
async def list_todos(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    """All to-dos (open first, newest first), with any tagged feedback summary."""
    rows = await db.execute(
        select(SuperAdminTodo, TenantFeedback)
        .join(TenantFeedback, TenantFeedback.id == SuperAdminTodo.feedback_id, isouter=True)
        .order_by(SuperAdminTodo.status.asc(), SuperAdminTodo.created_at.desc())
    )
    return {"items": [_todo_dict(t, fb) for t, fb in rows.all()]}


@router.post("/todos", status_code=201)
async def create_todo(
    payload: TodoCreateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    title = (payload.title or "").strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required.")
    feedback_id = None
    if payload.feedback_id:
        try:
            feedback_id = uuid.UUID(str(payload.feedback_id))
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid feedback_id.")
    todo = SuperAdminTodo(
        title=title[:500],
        notes=(payload.notes or "").strip() or None,
        feedback_id=feedback_id,
        created_by_superadmin_id=current_user.id,
    )
    db.add(todo)
    await db.commit()
    await db.refresh(todo)
    return _todo_dict(todo)


@router.patch("/todos/{todo_id}")
async def update_todo(
    todo_id: uuid.UUID,
    payload: TodoUpdateRequest,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    todo = (await db.execute(select(SuperAdminTodo).where(SuperAdminTodo.id == todo_id))).scalars().first()
    if todo is None:
        raise HTTPException(status_code=404, detail="To-do not found.")
    if payload.title is not None:
        todo.title = payload.title.strip()[:500] or todo.title
    if payload.notes is not None:
        todo.notes = payload.notes.strip() or None
    if payload.status in ("open", "done"):
        todo.status = payload.status
        todo.completed_at = datetime.now(timezone.utc) if payload.status == "done" else None
    db.add(todo)
    await db.commit()
    await db.refresh(todo)
    return _todo_dict(todo)


@router.delete("/todos/{todo_id}", status_code=204)
async def delete_todo(
    todo_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    todo = (await db.execute(select(SuperAdminTodo).where(SuperAdminTodo.id == todo_id))).scalars().first()
    if todo is not None:
        await db.delete(todo)
        await db.commit()
    return None


# ── Broadcast email ──────────────────────────────────────────────────────────
# NOTE: defined BEFORE "/{organization_id}" so "broadcast" isn't parsed as an id.
class BroadcastRequest(BaseModel):
    subject: str
    heading: str | None = None
    message: str
    eyebrow: str | None = None
    cta_label: str | None = None
    cta_url: str | None = None
    # When set, send only to this address (a self-test before the real blast).
    test_email: str | None = None


async def _broadcast_recipients(db: AsyncSession) -> list[tuple[str, str, str]]:
    """Onboarded (active) tenants with an admin email, deduped by address."""
    rows = await db.execute(
        select(Organization.id, Organization.name, Organization.admin_email)
        .where(Organization.status == "active")
        .order_by(Organization.name.asc())
    )
    seen: set[str] = set()
    out: list[tuple[str, str, str]] = []
    for oid, name, email in rows.all():
        if not email:
            continue
        key = email.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        out.append((str(oid), name, email.strip()))
    return out


@router.get("/broadcast/recipients")
async def broadcast_recipients(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    recips = await _broadcast_recipients(db)
    return {
        "count": len(recips),
        "recipients": [
            {"organization_id": oid, "organization_name": name, "admin_email": email}
            for oid, name, email in recips
        ],
    }


@router.post("/broadcast")
async def send_broadcast(
    payload: BroadcastRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    subject = (payload.subject or "").strip()
    message = (payload.message or "").strip()
    if not subject:
        raise HTTPException(status_code=400, detail="A subject is required.")
    if not message:
        raise HTTPException(status_code=400, detail="A message body is required.")

    heading = (payload.heading or subject).strip()
    eyebrow = (payload.eyebrow or "Announcement").strip() or "Announcement"
    cta_url = (payload.cta_url or "").strip() or None
    cta_label = (payload.cta_label or "").strip() or None
    if cta_url and not cta_label:
        cta_label = "Open Nokvo One"

    test_mode = bool(payload.test_email and payload.test_email.strip())
    if test_mode:
        recipients = [("", "", payload.test_email.strip())]
    else:
        recipients = await _broadcast_recipients(db)

    sent = 0
    failed: list[dict] = []
    for _oid, _name, email in recipients:
        try:
            await EmailService.send_broadcast_email(
                email,
                subject=subject,
                heading=heading,
                body_text=message,
                eyebrow=eyebrow,
                cta_label=cta_label,
                cta_url=cta_url,
            )
            sent += 1
        except Exception as exc:  # noqa: BLE001 — one bad address can't abort the blast
            failed.append({"email": email, "error": str(exc)})

    if not test_mode:
        db.add(
            SuperAdminAuditLog(
                superadmin_id=current_user.id,
                action="tenant_broadcast_sent",
                risk_level="medium",
                target_type="broadcast",
                target_id=None,
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                request_id=request.headers.get("x-request-id"),
                metadata_={
                    "subject": subject,
                    "recipients": len(recipients),
                    "sent": sent,
                    "failed": len(failed),
                    "had_cta": bool(cta_url),
                },
            )
        )
        await db.commit()

    return {"sent": sent, "failed": failed, "total": len(recipients), "test": test_mode}


# ── LangSmith diagnostics ────────────────────────────────────────────────────
# NOTE: defined BEFORE "/{organization_id}" so "langsmith" isn't parsed as an id.
@router.get("/langsmith/runs")
async def langsmith_runs(
    limit: int = 25,
    q: str | None = None,
    errors_only: bool = False,
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    """Recent voice calls from LangSmith (root ``voice_call`` runs)."""
    return await LangSmithDiagnostics.list_recent_calls(
        limit=max(1, min(limit, 100)), q=q, errors_only=errors_only
    )


@router.get("/langsmith/runs/{run_id}")
async def langsmith_run_detail(
    run_id: str,
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    """Stitched conversation + metadata for one call, for diagnosis."""
    try:
        return await LangSmithDiagnostics.get_call_detail(run_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"LangSmith lookup failed: {exc}")


# ── LLM pool keys ────────────────────────────────────────────────────────────
# NOTE: defined BEFORE "/{organization_id}" so "llm-keys" isn't parsed as an id.
_LLM_POOLS = {"mini", "nano"}


def _mask_key(plaintext: str) -> str:
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return "••••"
    return f"{plaintext[:4]}…{plaintext[-4:]}"


class LlmKeyRequest(BaseModel):
    pool: str = "mini"
    label: str | None = None
    endpoint: str
    api_key: str | None = None  # required on create; omit on edit to keep existing
    deployment: str | None = None
    tpm: int | None = None
    enabled: bool = True


def _llm_key_row(k: LlmPoolKey) -> dict:
    from app.core.secret_crypto import decrypt_secret

    try:
        masked = _mask_key(decrypt_secret(k.api_key_enc))
    except Exception:
        masked = "••••"
    return {
        "id": str(k.id),
        "pool": k.pool,
        "label": k.label,
        "endpoint": k.endpoint,
        "api_key_masked": masked,
        "deployment": k.deployment,
        "tpm": k.tpm,
        "enabled": bool(k.enabled),
        "created_at": k.created_at,
    }


@router.get("/llm-keys")
async def list_llm_keys(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    rows = (await db.execute(select(LlmPoolKey).order_by(LlmPoolKey.created_at.desc()))).scalars().all()
    # Live pool composition (env + DB), so the operator sees what's actually serving.
    live = {
        p: [
            {"key_id": m.key_id, "endpoint": m.endpoint, "deployment": m.deployment, "tpm": m.tpm}
            for m in LLMPool.members(p)
        ]
        for p in ("mini", "nano")
    }
    return {"items": [_llm_key_row(k) for k in rows], "live_pools": live}


@router.post("/llm-keys", status_code=201)
async def create_llm_key(
    payload: LlmKeyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    pool = (payload.pool or "mini").strip().lower()
    if pool not in _LLM_POOLS:
        raise HTTPException(status_code=400, detail="pool must be 'mini' or 'nano'.")
    endpoint = (payload.endpoint or "").strip().rstrip("/")
    api_key = (payload.api_key or "").strip()
    if not endpoint or not api_key:
        raise HTTPException(status_code=400, detail="endpoint and api_key are required.")
    key = LlmPoolKey(
        pool=pool,
        label=(payload.label or "").strip() or None,
        endpoint=endpoint,
        api_key_enc=encrypt_secret(api_key),
        deployment=(payload.deployment or "").strip() or None,
        tpm=payload.tpm,
        enabled=payload.enabled,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    await LLMPool.reload_db_members()  # take effect on this instance immediately
    return _llm_key_row(key)


@router.patch("/llm-keys/{key_id}")
async def update_llm_key(
    key_id: uuid.UUID,
    payload: LlmKeyRequest,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    key = (await db.execute(select(LlmPoolKey).where(LlmPoolKey.id == key_id))).scalars().first()
    if key is None:
        raise HTTPException(status_code=404, detail="Key not found.")
    if payload.pool:
        pool = payload.pool.strip().lower()
        if pool not in _LLM_POOLS:
            raise HTTPException(status_code=400, detail="pool must be 'mini' or 'nano'.")
        key.pool = pool
    if payload.endpoint:
        key.endpoint = payload.endpoint.strip().rstrip("/")
    if payload.api_key and payload.api_key.strip():
        key.api_key_enc = encrypt_secret(payload.api_key.strip())
    key.label = (payload.label or "").strip() or None
    key.deployment = (payload.deployment or "").strip() or None
    key.tpm = payload.tpm
    key.enabled = payload.enabled
    db.add(key)
    await db.commit()
    await db.refresh(key)
    await LLMPool.reload_db_members()
    return _llm_key_row(key)


@router.delete("/llm-keys/{key_id}", status_code=204)
async def delete_llm_key(
    key_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    key = (await db.execute(select(LlmPoolKey).where(LlmPoolKey.id == key_id))).scalars().first()
    if key is not None:
        await db.delete(key)
        await db.commit()
        await LLMPool.reload_db_members()
    return None


# ── Bulk CSV calling access requests ────────────────────────────────────────
# Defined BEFORE "/{organization_id}" so "bulk-calling-requests" isn't parsed as
# an org id. Tenants request the add-on (leaving a contact number); granting it
# means provisioning a dedicated Plivo number/auth onto their TenantResources.


def _bulk_request_dict(
    req, org_name, user_email, enabled: bool, number: str | None,
    *, is_apex: bool = False, bulk_status: str | None = None, bulk_count: int = 0,
) -> dict:
    return {
        "id": str(req.id),
        "organization_id": str(req.organization_id),
        "organization_name": org_name or "—",
        "tenant_id": req.tenant_id,
        "requested_by_email": user_email or "—",
        "contact_number": req.contact_number,
        "note": req.note,
        "status": req.status,
        "enabled": enabled,
        "bulk_number": number,
        # APEX rows offer one-click auto-provisioning; bulk_status/bulk_count show
        # live progress of the 5-DID pool ("provisioning" → "active").
        "is_apex": is_apex,
        "bulk_status": bulk_status,
        "bulk_count": bulk_count,
        "created_at": req.created_at.isoformat() if req.created_at else None,
        "reviewed_at": req.reviewed_at.isoformat() if req.reviewed_at else None,
    }


@router.get("/bulk-calling-requests")
async def list_bulk_calling_requests(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
    limit: int = 500,
):
    """Every tenant request for Bulk CSV Calling, newest first, with the live
    enabled state so the console can show pending vs already-provisioned."""
    from app.models.bulk_calling_request import BulkCallingRequest

    limit = max(1, min(int(limit or 500), 2000))
    rows = await db.execute(
        select(BulkCallingRequest, Organization.name, OrganizationUser.email, Organization.product_tier)
        .join(Organization, Organization.id == BulkCallingRequest.organization_id, isouter=True)
        .join(OrganizationUser, OrganizationUser.id == BulkCallingRequest.requested_by_user_id, isouter=True)
        .order_by(BulkCallingRequest.created_at.desc())
        .limit(limit)
    )
    rows = rows.all()
    # Resolve the live bulk-calling state per tenant in one pass.
    tenant_ids = [r[0].tenant_id for r in rows if r[0].tenant_id]
    tr_by_tid: dict[str, TenantResources] = {}
    if tenant_ids:
        tr_rows = await db.execute(
            select(TenantResources).where(TenantResources.tenant_id.in_(tenant_ids))
        )
        tr_by_tid = {tr.tenant_id: tr for tr in tr_rows.scalars().all()}
    items = []
    for req, org_name, user_email, product_tier in rows:
        tr = tr_by_tid.get(req.tenant_id) if req.tenant_id else None
        enabled = PlivoService.bulk_calling_enabled(tr) if tr else False
        number = PlivoService.bulk_calling_caller_id(tr) if tr else None
        bulk_cfg = (tr.provider_status or {}).get("bulk_calling") or {} if tr else {}
        items.append(
            _bulk_request_dict(
                req, org_name, user_email, enabled, number,
                is_apex=(product_tier == "nokvo_apex"),
                bulk_status=bulk_cfg.get("status"),
                bulk_count=len(bulk_cfg.get("numbers") or []),
            )
        )
    return {"items": items}


class BulkCallingGrantPayload(BaseModel):
    # The dedicated Plivo telephony provisioned for this tenant's bulk calling.
    auth_id: str = Field(min_length=3, max_length=128)
    auth_token: str = Field(min_length=3, max_length=512)
    number: str = Field(min_length=4, max_length=32)  # the DID / outbound caller ID


@router.post("/bulk-calling-requests/{request_id}/grant")
async def grant_bulk_calling_request(
    request_id: uuid.UUID,
    payload: BulkCallingGrantPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Provision dedicated Plivo telephony for the tenant and unlock the feature.

    Writes ``provider_status["bulk_calling"]`` (auth token encrypted at rest) and
    flips the request to ``approved``. The auth token is never echoed back."""
    from sqlalchemy.orm.attributes import flag_modified

    from app.models.bulk_calling_request import BulkCallingRequest

    req = (
        await db.execute(select(BulkCallingRequest).where(BulkCallingRequest.id == request_id))
    ).scalars().first()
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")

    # Resolve the tenant to provision onto (denormalized tenant_id, else via org).
    tr = None
    if req.tenant_id:
        tr = (
            await db.execute(select(TenantResources).where(TenantResources.tenant_id == req.tenant_id))
        ).scalars().first()
    if tr is None:
        tr = (
            await db.execute(
                select(TenantResources).where(TenantResources.organization_id == req.organization_id)
            )
        ).scalars().first()
    if tr is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found for this organization")

    auth_id = payload.auth_id.strip()
    auth_token = payload.auth_token.strip()
    # Normalize to bare digits — Plivo's Call API rejects formatted numbers like
    # "+91 22 6423 2977", which would make every campaign silently fail to dial.
    number = PlivoService.normalize_number(payload.number)

    # Pre-flight the dedicated telephony so we never "enable" a config that can't
    # place calls (bad creds, or a caller ID not actually rented on that account).
    validation_error = await PlivoService.validate_bulk_telephony(auth_id, auth_token, number)
    if validation_error:
        raise HTTPException(status_code=400, detail=validation_error)

    provider_status = dict(tr.provider_status or {})
    provider_status["bulk_calling"] = {
        "auth_id": auth_id,
        "auth_token_enc": encrypt_secret(auth_token),
        "number": number,
        "enabled": True,
        "granted_at": datetime.now(timezone.utc).isoformat(),
        "granted_by_superadmin_id": str(current_user.id),
    }
    tr.provider_status = provider_status
    flag_modified(tr, "provider_status")
    db.add(tr)

    req.status = "approved"
    req.reviewed_at = datetime.now(timezone.utc)
    req.reviewed_by_superadmin_id = current_user.id
    db.add(req)

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="bulk_calling_granted",
            risk_level="high",  # provisions outbound calling capability
            target_type="organization",
            target_id=str(req.organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            metadata_={"tenant_id": tr.tenant_id, "number": number, "auth_id": auth_id},
        )
    )
    await db.commit()
    return {"id": str(req.id), "status": req.status, "enabled": True, "bulk_number": number}


@router.post("/bulk-calling-requests/{request_id}/auto-provision")
async def auto_provision_bulk_calling(
    request_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """APEX one-click: programmatically create a dedicated Plivo sub-account, reuse
    the tenant's onboarding compliance application, and buy 5 DIDs against it
    (deferred via the number poller until compliance approves). Replaces the manual
    auth/token/number paste for APEX; non-APEX orgs must use ``/grant``."""
    from app.models.bulk_calling_request import BulkCallingRequest
    from app.services.plivo_bulk_provisioning_service import (
        BULK_DID_COUNT,
        PlivoBulkProvisioningService,
    )

    req = (
        await db.execute(select(BulkCallingRequest).where(BulkCallingRequest.id == request_id))
    ).scalars().first()
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")

    # APEX-only — auto-provisioning is gated to the deterministic-outbound product.
    org = (
        await db.execute(select(Organization).where(Organization.id == req.organization_id))
    ).scalars().first()
    if org is None or (org.product_tier or "") != "nokvo_apex":
        raise HTTPException(
            status_code=409,
            detail="Auto-provisioning is for NOKVO APEX organizations only — use the manual grant for others.",
        )

    # Resolve the tenant to provision onto (denormalized tenant_id, else via org).
    tr = None
    if req.tenant_id:
        tr = (
            await db.execute(select(TenantResources).where(TenantResources.tenant_id == req.tenant_id))
        ).scalars().first()
    if tr is None:
        tr = (
            await db.execute(
                select(TenantResources).where(TenantResources.organization_id == req.organization_id)
            )
        ).scalars().first()
    if tr is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found for this organization")

    cfg = await PlivoBulkProvisioningService.start_auto_provision(
        tr, db, superadmin_id=str(current_user.id)
    )

    req.status = "approved"
    req.reviewed_at = datetime.now(timezone.utc)
    req.reviewed_by_superadmin_id = current_user.id
    db.add(req)
    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="bulk_auto_provisioned",
            risk_level="high",  # programmatically buys outbound DIDs (recurring cost)
            target_type="organization",
            target_id=str(req.organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            metadata_={"tenant_id": tr.tenant_id, "target_count": BULK_DID_COUNT, "auth_id": cfg.get("auth_id")},
        )
    )
    await db.commit()
    return {
        "id": str(req.id),
        "status": req.status,
        "bulk_status": cfg.get("status"),
        "enabled": bool(cfg.get("enabled")),
        "numbers": cfg.get("numbers") or [],
        "target_count": BULK_DID_COUNT,
        "error": cfg.get("error"),
    }


class GrantExistingSubaccountPayload(BaseModel):
    # The DIDs the operator rented (by hand) on the client's sub-account. Optional —
    # blank means "auto-detect every number on the sub-account".
    numbers: list[str] = Field(default_factory=list)


@router.post("/bulk-calling-requests/{request_id}/grant-existing")
async def grant_bulk_existing_subaccount(
    request_id: uuid.UUID,
    payload: GrantExistingSubaccountPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """APEX manual provisioning: the operator rents the DIDs in Plivo by hand, then
    enables bulk calling on the client's EXISTING account-creation sub-account here —
    no auth token to paste (the stored encrypted creds are reused). ``numbers`` is
    optional (blank = auto-detect what's on the sub-account). APEX-only; non-APEX
    orgs use the manual ``/grant`` with their own dedicated credentials."""
    from app.models.bulk_calling_request import BulkCallingRequest
    from app.services.plivo_bulk_provisioning_service import PlivoBulkProvisioningService

    req = (
        await db.execute(select(BulkCallingRequest).where(BulkCallingRequest.id == request_id))
    ).scalars().first()
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")

    org = (
        await db.execute(select(Organization).where(Organization.id == req.organization_id))
    ).scalars().first()
    if org is None or (org.product_tier or "") != "nokvo_apex":
        raise HTTPException(
            status_code=409,
            detail="Existing-sub-account grant is for NOKVO APEX organizations only — use the manual grant for others.",
        )

    tr = None
    if req.tenant_id:
        tr = (
            await db.execute(select(TenantResources).where(TenantResources.tenant_id == req.tenant_id))
        ).scalars().first()
    if tr is None:
        tr = (
            await db.execute(
                select(TenantResources).where(TenantResources.organization_id == req.organization_id)
            )
        ).scalars().first()
    if tr is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found for this organization")

    cfg = await PlivoBulkProvisioningService.grant_with_existing_subaccount(
        tr, db, numbers=payload.numbers, superadmin_id=str(current_user.id)
    )
    if cfg.get("error"):
        raise HTTPException(status_code=400, detail=cfg["error"])

    req.status = "approved"
    req.reviewed_at = datetime.now(timezone.utc)
    req.reviewed_by_superadmin_id = current_user.id
    db.add(req)
    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="bulk_granted_existing_subaccount",
            risk_level="high",  # enables outbound DIDs (recurring cost) on the client's sub-account
            target_type="organization",
            target_id=str(req.organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            metadata_={"tenant_id": tr.tenant_id, "pool": cfg.get("numbers"), "auth_id": cfg.get("auth_id")},
        )
    )
    await db.commit()
    return {
        "id": str(req.id),
        "status": req.status,
        "enabled": bool(cfg.get("enabled")),
        "numbers": cfg.get("numbers") or [],
        "bulk_status": cfg.get("status"),
    }


@router.post("/bulk-calling-requests/{request_id}/deny")
async def deny_bulk_calling_request(
    request_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Decline a request (leaves any existing telephony untouched)."""
    from app.models.bulk_calling_request import BulkCallingRequest

    req = (
        await db.execute(select(BulkCallingRequest).where(BulkCallingRequest.id == request_id))
    ).scalars().first()
    if req is None:
        raise HTTPException(status_code=404, detail="Request not found")
    req.status = "denied"
    req.reviewed_at = datetime.now(timezone.utc)
    req.reviewed_by_superadmin_id = current_user.id
    db.add(req)
    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="bulk_calling_denied",
            risk_level="low",
            target_type="organization",
            target_id=str(req.organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
        )
    )
    await db.commit()
    return {"id": str(req.id), "status": req.status}


# ── Affiliate program (operator console) ────────────────────────────────────
# List + KYC approval + the T+2 due-settlement queue + manual settle (operator
# does the bank transfer and records the UTR). Declared BEFORE the
# parameterized /{organization_id} routes below so the literal "affiliates"
# segment is never captured as an org id. T+2 due-ness is a pure query filter —
# there is deliberately no background ticker.


def _affiliate_summary(affiliate, totals: dict, referred_count: int) -> dict:
    from app.services.affiliate_service import bank_details_complete

    return {
        "id": str(affiliate.id),
        "affiliate_number": affiliate.affiliate_number,
        "full_name": affiliate.full_name,
        "email": affiliate.email,
        "status": affiliate.status,
        "created_at": affiliate.created_at.isoformat() if affiliate.created_at else None,
        "last_login_at": affiliate.last_login_at.isoformat() if affiliate.last_login_at else None,
        "kyc_verified_at": affiliate.kyc_verified_at.isoformat() if affiliate.kyc_verified_at else None,
        "kyc_verified_by": affiliate.kyc_verified_by,
        # FULL bank details — the operator needs them to make the transfer.
        "bank": (
            {
                "account_holder": affiliate.bank_account_holder,
                "account_number": affiliate.bank_account_number,
                "ifsc": affiliate.bank_ifsc,
            }
            if bank_details_complete(affiliate)
            else None
        ),
        "totals": totals,
        "referred_count": referred_count,
    }


async def _affiliate_totals(db: AsyncSession) -> dict:
    """Per-affiliate accrued/pending/due/settled rupees in one grouped pass."""
    from datetime import timedelta

    from app.models.affiliate_commission import AffiliateCommission

    due_cutoff = datetime.now(timezone.utc) - timedelta(
        hours=settings.AFFILIATE_SETTLEMENT_DUE_HOURS
    )
    rows = (
        await db.execute(
            select(
                AffiliateCommission.affiliate_id,
                AffiliateCommission.amount_rupees,
                AffiliateCommission.settlement_id,
                AffiliateCommission.created_at,
            )
        )
    ).all()
    out: dict = {}
    for affiliate_id, amount, settlement_id, created_at in rows:
        t = out.setdefault(
            affiliate_id, {"accrued": 0.0, "pending": 0.0, "due": 0.0, "settled": 0.0}
        )
        value = float(amount or 0)
        t["accrued"] += value
        if settlement_id is not None:
            t["settled"] += value
        else:
            t["pending"] += value
            created = created_at
            if created is not None and created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if created is not None and created <= due_cutoff:
                t["due"] += value
    for t in out.values():
        for k in t:
            t[k] = round(t[k], 2)
    return out


@router.get("/affiliates")
async def list_affiliates(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    from app.models.affiliate import Affiliate

    affiliates = (
        await db.execute(select(Affiliate).order_by(Affiliate.created_at.desc()))
    ).scalars().all()
    totals = await _affiliate_totals(db)
    referred = dict(
        (
            await db.execute(
                select(Organization.affiliate_id, func.count(Organization.id))
                .where(Organization.affiliate_id.is_not(None))
                .group_by(Organization.affiliate_id)
            )
        ).all()
    )
    empty = {"accrued": 0.0, "pending": 0.0, "due": 0.0, "settled": 0.0}
    return {
        "items": [
            _affiliate_summary(a, totals.get(a.id, dict(empty)), int(referred.get(a.id, 0)))
            for a in affiliates
        ]
    }


@router.get("/affiliates/settlements/due")
async def affiliate_settlements_due(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_READ_ROLES)),
):
    """The T+2 payout queue: unsettled commissions older than the due window,
    grouped per affiliate — only affiliates the operator may actually pay
    (active + KYC verified + bank details on file)."""
    from datetime import timedelta

    from app.models.affiliate import Affiliate
    from app.models.affiliate_commission import AffiliateCommission
    from app.services.affiliate_service import settlement_eligible

    due_cutoff = datetime.now(timezone.utc) - timedelta(
        hours=settings.AFFILIATE_SETTLEMENT_DUE_HOURS
    )
    rows = (
        await db.execute(
            select(AffiliateCommission)
            .where(
                AffiliateCommission.settlement_id.is_(None),
                AffiliateCommission.created_at <= due_cutoff,
            )
            .order_by(AffiliateCommission.created_at.asc())
        )
    ).scalars().all()
    by_affiliate: dict = {}
    for c in rows:
        by_affiliate.setdefault(c.affiliate_id, []).append(c)
    items = []
    for affiliate_id, commissions in by_affiliate.items():
        affiliate = await db.get(Affiliate, affiliate_id)
        if affiliate is None or not settlement_eligible(affiliate):
            continue
        items.append(
            {
                "affiliate_id": str(affiliate.id),
                "affiliate_number": affiliate.affiliate_number,
                "full_name": affiliate.full_name,
                "bank": {
                    "account_holder": affiliate.bank_account_holder,
                    "account_number": affiliate.bank_account_number,
                    "ifsc": affiliate.bank_ifsc,
                },
                "due_amount_rupees": round(sum(float(c.amount_rupees or 0) for c in commissions), 2),
                "due_count": len(commissions),
                "oldest_created_at": (
                    commissions[0].created_at.isoformat() if commissions[0].created_at else None
                ),
            }
        )
    items.sort(key=lambda x: x["due_amount_rupees"], reverse=True)
    return {"items": items}


@router.post("/affiliates/{affiliate_id}/kyc-verify")
async def verify_affiliate_kyc(
    affiliate_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Operator approved the affiliate's identity (verified out-of-band, e.g.
    bank account-holder name match) — unblocks settlement (with bank details).
    Idempotent."""
    from app.models.affiliate import Affiliate

    affiliate = await db.get(Affiliate, affiliate_id)
    if affiliate is None:
        raise HTTPException(status_code=404, detail="Affiliate not found")
    if affiliate.kyc_verified_at is None:
        affiliate.kyc_verified_at = datetime.now(timezone.utc)
        affiliate.kyc_verified_by = current_user.email
        db.add(
            SuperAdminAuditLog(
                superadmin_id=current_user.id,
                action="affiliate_kyc_verified",
                risk_level="low",
                target_type="affiliate",
                target_id=str(affiliate.id),
                ip_address=request.client.host if request.client else None,
                user_agent=request.headers.get("user-agent"),
                request_id=request.headers.get("x-request-id"),
            )
        )
        await db.commit()
        await db.refresh(affiliate)
    return {
        "id": str(affiliate.id),
        "kyc_verified_at": affiliate.kyc_verified_at.isoformat() if affiliate.kyc_verified_at else None,
        "kyc_verified_by": affiliate.kyc_verified_by,
    }


@router.post("/affiliates/{affiliate_id}/reset-totp")
async def reset_affiliate_totp(
    affiliate_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Lost-authenticator recovery (support-ticket flow): rotate the TOTP
    secret and drop the affiliate back to ``pending_totp``. They then re-submit
    the public signup form with the same email — the reclaim path issues a
    fresh QR and re-activates the SAME id + affiliate number (and clears the
    KYC approval, since the identity fields may have changed)."""
    from app.core.secret_crypto import encrypt_secret as _encrypt
    from app.core.security import generate_totp_secret
    from app.models.affiliate import Affiliate

    affiliate = await db.get(Affiliate, affiliate_id)
    if affiliate is None:
        raise HTTPException(status_code=404, detail="Affiliate not found")
    affiliate.totp_secret_encrypted_v2 = _encrypt(generate_totp_secret())
    affiliate.status = "pending_totp"
    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="affiliate_totp_reset",
            risk_level="medium",
            target_type="affiliate",
            target_id=str(affiliate.id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
        )
    )
    await db.commit()
    return {"id": str(affiliate.id), "status": affiliate.status}


class AffiliateSettleRequest(BaseModel):
    utr: str = Field(min_length=1, max_length=64)


@router.post("/affiliates/{affiliate_id}/settle")
async def settle_affiliate_commissions(
    affiliate_id: uuid.UUID,
    payload: AffiliateSettleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Mark every DUE commission for this affiliate settled under one bank
    transfer (one UTR). The affiliate row lock serializes a double-click /
    two-operator race; rows are locked too so a concurrent settle can't stamp
    them twice."""
    from datetime import timedelta

    from app.models.affiliate import Affiliate
    from app.models.affiliate_commission import AffiliateCommission, AffiliateSettlement
    from app.services.affiliate_service import settlement_eligible

    utr = (payload.utr or "").strip()
    if not utr:
        raise HTTPException(status_code=400, detail="Enter the bank UTR reference.")

    affiliate = (
        await db.execute(
            select(Affiliate).where(Affiliate.id == affiliate_id).with_for_update()
        )
    ).scalars().first()
    if affiliate is None:
        raise HTTPException(status_code=404, detail="Affiliate not found")
    if not settlement_eligible(affiliate):
        raise HTTPException(
            status_code=409,
            detail="Affiliate is not payable yet — verify KYC and make sure bank details are on file.",
        )

    due_cutoff = datetime.now(timezone.utc) - timedelta(
        hours=settings.AFFILIATE_SETTLEMENT_DUE_HOURS
    )
    commissions = (
        await db.execute(
            select(AffiliateCommission)
            .where(
                AffiliateCommission.affiliate_id == affiliate.id,
                AffiliateCommission.settlement_id.is_(None),
                AffiliateCommission.created_at <= due_cutoff,
            )
            .with_for_update()
        )
    ).scalars().all()
    if not commissions:
        raise HTTPException(status_code=409, detail="Nothing is due for settlement yet.")

    total = sum(Decimal(str(c.amount_rupees or 0)) for c in commissions)
    settlement = AffiliateSettlement(
        id=uuid.uuid4(),
        affiliate_id=affiliate.id,
        amount_rupees=total,
        commission_count=len(commissions),
        utr_reference=utr[:64],
        settled_by=current_user.email,
    )
    db.add(settlement)
    await db.flush()
    for c in commissions:
        c.settlement_id = settlement.id
    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="affiliate_commissions_settled",
            risk_level="medium",
            target_type="affiliate",
            target_id=str(affiliate.id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            metadata_={
                "settlement_id": str(settlement.id),
                "amount_rupees": float(total),
                "commission_count": len(commissions),
                "utr": utr[:64],
            },
        )
    )
    await db.commit()
    return {
        "settlement_id": str(settlement.id),
        "amount_rupees": float(total),
        "commission_count": len(commissions),
        "utr_reference": utr[:64],
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

    tenant_res = (
        await db.execute(select(TenantResources).where(TenantResources.organization_id == org.id))
    ).scalars().first()
    plivo_cfg = (tenant_res.provider_status or {}).get("plivo", {}) if tenant_res else {}
    _comp = (plivo_cfg.get("compliance") or {}) if isinstance(plivo_cfg, dict) else {}
    # APEX orgs dial from a POOL of DIDs on their account-creation sub-account
    # (provider_status["bulk_calling"]), not the inbound number — surface it so the
    # console can show/manage the outbound pool instead of the inbound DID.
    _bulk = (tenant_res.provider_status or {}).get("bulk_calling") or {} if tenant_res else {}
    telephony = {
        "tenant_id": tenant_res.tenant_id if tenant_res else None,
        "number": (plivo_cfg.get("number") if isinstance(plivo_cfg, dict) else None)
        or (tenant_res.twilio_phone_number if tenant_res else None),
        "has_application": bool(isinstance(plivo_cfg, dict) and plivo_cfg.get("application_id")),
        "provisioning_status": tenant_res.provisioning_status if tenant_res else None,
        "number_status": plivo_cfg.get("number_status") if isinstance(plivo_cfg, dict) else None,
        "compliance_application_id": _comp.get("application_id"),
        "compliance_error": _comp.get("error"),
        "bulk_enabled": PlivoService.bulk_calling_enabled(tenant_res) if tenant_res else False,
        "bulk_numbers": _bulk.get("numbers") or [],
        "bulk_status": _bulk.get("status"),
    }

    subscription_inr = sub.get("monthly_inr", 0.0)
    usage_inr = at.get("usage_revenue_inr", 0.0)
    revenue_inr = round(subscription_inr + usage_inr, 2)
    cogs_inr = at.get("cogs_inr", 0.0)

    # The APEX org's STAMPED plan config (what it is actually billed and capped at) plus
    # the catalog label — this is what the console's plan panel shows and changes.
    apex_plan = None
    if (org.product_tier or "") == "nokvo_apex":
        from app.services.apex_account_service import plan_snapshot
        from app.services.apex_plans import resolve_plan

        apex_plan = plan_snapshot(org)
        apex_plan["plan_label"] = resolve_plan(org.apex_plan_code).label if org.apex_plan_code else None
        apex_plan["activated_at"] = org.apex_activated_at.isoformat() if org.apex_activated_at else None
        # The live monthly charge (the subscription row), so an upgrade shows what changes.
        apex_plan["monthly_inr"] = subscription_inr

    return {
        "organization_id": oid,
        "organization_name": org.name,
        "admin_email": org.admin_email,
        "admin_name": org.admin_name,
        "status": org.status,
        "product_tier": org.product_tier,
        "is_apex": (org.product_tier or "") == "nokvo_apex",
        "apex_plan": apex_plan,
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
        "telephony": telephony,
        "recent_calls": recent,
    }


class PlivoNumberPayload(BaseModel):
    # A single DID, or up to 5 at once via ``numbers`` (each bound to the tenant's
    # inbound Application). ``number`` stays for back-compat / single-DID callers.
    number: str | None = None
    numbers: list[str] | None = None
    reassign: bool = True


@router.post("/{organization_id}/plivo-number")
async def change_plivo_number(
    organization_id: str,
    payload: PlivoNumberPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Bind one or more Plivo DIDs (up to 5) to a tenant's inbound app (operator
    override)."""
    raw_numbers = list(payload.numbers) if payload.numbers else ([payload.number] if payload.number else [])
    numbers = [n.strip() for n in raw_numbers if (n or "").strip()]
    if not numbers:
        raise HTTPException(status_code=400, detail="A phone number is required.")
    tenant_res = (
        await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
    ).scalars().first()
    if tenant_res is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found — provision the tenant first.")

    try:
        from app.services.public_url import public_base_url

        base = public_base_url()
    except Exception:
        base = None

    try:
        result = await PlivoService.set_tenant_numbers(
            tenant_res, db, numbers=numbers, reassign=payload.reassign, base=base
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not set the number: {exc}")

    # APEX: adding DIDs should ALSO make the org dialable. The DIDs live on the
    # tenant's subaccount, so enable bulk calling from it (auto-detects the live
    # pool) — no separate grant needed. Best-effort, APEX-only.
    org = (
        await db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalars().first()
    if org is not None and (org.product_tier or "") == "nokvo_apex":
        try:
            from app.services.plivo_bulk_provisioning_service import PlivoBulkProvisioningService

            bulk = await PlivoBulkProvisioningService.grant_with_existing_subaccount(
                tenant_res, db, numbers=None, superadmin_id=str(current_user.id)
            )
            result["bulk_enabled"] = bool(bulk.get("enabled"))
            result["bulk_pool"] = bulk.get("numbers") or []
            if bulk.get("error"):
                result["bulk_error"] = bulk["error"]
        except Exception as exc:  # noqa: BLE001
            result["bulk_error"] = str(exc)

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="tenant_plivo_number_changed",
            risk_level="high",
            target_type="organization",
            target_id=str(organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            before_state={"number": result.get("previous")},
            after_state={"number": result.get("number"), "numbers": result.get("numbers")},
            metadata_={
                "reassign": payload.reassign,
                "assigned": result.get("assigned"),
                "assigned_count": result.get("assigned_count"),
                "numbers": result.get("numbers"),
            },
        )
    )
    await db.commit()
    return result


class ApexBulkNumbersPayload(BaseModel):
    # DIDs the operator rented (by hand) on this APEX org's account-creation Plivo
    # sub-account. Optional — blank means "auto-detect every DID on the sub-account".
    # Up to 5 (the fixed bulk caller-ID pool size).
    numbers: list[str] = Field(default_factory=list)


@router.post("/{organization_id}/apex-bulk-numbers")
async def assign_apex_bulk_numbers(
    organization_id: str,
    payload: ApexBulkNumbersPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """APEX: register the DIDs rented on this org's account-creation Plivo sub-account
    as its outbound bulk caller-ID pool — no inbound-app binding, no auth token to
    paste (the stored sub-account creds are reused). ``numbers`` is optional (blank =
    auto-detect what the sub-account owns). This is the org-keyed twin of
    ``/bulk-calling-requests/{id}/grant-existing`` for APEX orgs that auto-provisioned
    at signup (so have no request row). APEX-only; Nokvo One orgs use ``/plivo-number``
    to bind an inbound DID."""
    org = (
        await db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if (org.product_tier or "") != "nokvo_apex":
        raise HTTPException(
            status_code=409,
            detail="Bulk-number assignment is for NOKVO APEX organizations only — use Change number for others.",
        )
    tenant_res = (
        await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
    ).scalars().first()
    if tenant_res is None:
        raise HTTPException(status_code=404, detail="Tenant resources not found — provision the tenant first.")

    from app.services.plivo_bulk_provisioning_service import PlivoBulkProvisioningService

    cfg = await PlivoBulkProvisioningService.grant_with_existing_subaccount(
        tenant_res, db, numbers=payload.numbers, superadmin_id=str(current_user.id)
    )
    if cfg.get("error"):
        raise HTTPException(status_code=400, detail=cfg["error"])

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="apex_bulk_numbers_assigned",
            risk_level="high",  # enables outbound DIDs (recurring cost) on the client's sub-account
            target_type="organization",
            target_id=str(organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            metadata_={"tenant_id": tenant_res.tenant_id, "pool": cfg.get("numbers"), "auth_id": cfg.get("auth_id")},
        )
    )
    await db.commit()
    return {
        "enabled": bool(cfg.get("enabled")),
        "numbers": cfg.get("numbers") or [],
        "bulk_status": cfg.get("status"),
    }


class PlanChangePayload(BaseModel):
    plan: str  # "inbound_only" | "inbound_outbound"
    # Enabling outbound is a paid capability. This flips it on WITHOUT charging
    # (a manual comp/override), so granting it requires an explicit ack that no
    # payment will be collected — recorded in the audit log.
    acknowledge_comp: bool = False


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

    enabling_outbound = bool(PLAN_CATALOG[payload.plan]["outbound"])

    org = (
        await db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    # Granting outbound flips on a PAID capability without charging — a manual
    # comp/override. Require an explicit acknowledgement so payment is never
    # skipped silently (the SuperAdmin UI surfaces this as a confirmation).
    is_comp_grant = enabling_outbound and not bool(org.calling_enabled)
    if is_comp_grant and not payload.acknowledge_comp:
        raise HTTPException(
            status_code=409,
            detail=(
                "Enabling outbound grants a paid feature without collecting payment. "
                "Confirm the comp/manual grant to proceed (acknowledge_comp=true)."
            ),
        )

    before = {"plan_type": org.plan_type, "calling_enabled": bool(org.calling_enabled)}

    org.plan_type = payload.plan
    org.calling_enabled = enabling_outbound
    after = {"plan_type": org.plan_type, "calling_enabled": bool(org.calling_enabled)}
    db.add(org)

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="organization_plan_changed",
            # A free grant of a paid capability is higher-risk than a downgrade.
            risk_level="high" if is_comp_grant else "medium",
            target_type="organization",
            target_id=str(org.id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            before_state=before,
            after_state=after,
            metadata_={
                "plan": payload.plan,
                "label": PLAN_CATALOG[payload.plan]["label"],
                "comp_grant": is_comp_grant,
                "payment_skipped": is_comp_grant,
                "acknowledged": bool(payload.acknowledge_comp),
            },
        )
    )
    await db.commit()

    return {
        "organization_id": str(org.id),
        "plan_type": org.plan_type,
        "plan_label": _plan_label(org.plan_type),
        "calling_enabled": bool(org.calling_enabled),
    }


class BypassPaymentPayload(BaseModel):
    # Provisioning a tenant without collecting payment is a comp/manual grant;
    # require an explicit acknowledgement (recorded in the audit log).
    acknowledge_comp: bool = False
    # Optional plan to grant for the comp tenant (when no subscription drives it).
    plan: str | None = None  # "inbound_only" | "inbound_outbound"


@router.post("/{organization_id}/bypass-payment")
async def bypass_payment(
    organization_id: str,
    payload: BypassPaymentPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Comp-activate a tenant stuck in ``pending_payment`` WITHOUT a Razorpay
    payment — runs the same idempotent provisioning the payment webhook would,
    flipping the org into onboarding. A manual grant, so it requires an explicit
    acknowledgement and is logged as a high-risk payment bypass."""
    if not payload.acknowledge_comp:
        raise HTTPException(
            status_code=409,
            detail=(
                "Bypassing payment provisions a tenant without collecting payment. "
                "Confirm the comp/manual activation to proceed (acknowledge_comp=true)."
            ),
        )
    if payload.plan is not None and payload.plan not in PLAN_CATALOG:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown plan '{payload.plan}'. Valid: {', '.join(PLAN_CATALOG)}",
        )

    org = (
        await db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if org.status != "pending_payment":
        raise HTTPException(
            status_code=409,
            detail=f"Organization is '{org.status}', not pending_payment — nothing to bypass.",
        )

    before_status = org.status

    # Run the exact post-payment activation (provision + activate + onboarding).
    from app.api.nokvo_one_payments import activate_and_provision

    try:
        result = await activate_and_provision(db, org.id)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Provisioning failed: {exc}")

    # Optional explicit plan grant (when no subscription set one).
    if payload.plan is not None:
        org = (
            await db.execute(select(Organization).where(Organization.id == organization_id))
        ).scalars().first()
        org.plan_type = payload.plan
        org.calling_enabled = bool(PLAN_CATALOG[payload.plan]["outbound"])
        db.add(org)

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="organization_payment_bypassed",
            risk_level="high",
            target_type="organization",
            target_id=str(organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            before_state={"status": before_status},
            after_state={"status": result.get("org_status")},
            metadata_={
                "comp_grant": True,
                "payment_skipped": True,
                "acknowledged": True,
                "plan": payload.plan,
            },
        )
    )
    await db.commit()

    return {
        "organization_id": str(organization_id),
        "provisioned": bool(result.get("provisioned")),
        "org_status": result.get("org_status"),
        "onboarding_step": result.get("onboarding_step"),
        "idempotent": bool(result.get("idempotent", False)),
    }


class GrantMinutesPayload(BaseModel):
    # Crediting minutes without a payment is a comp/manual grant — require an
    # explicit acknowledgement (recorded in the audit log).
    minutes: int
    acknowledge_comp: bool = False


@router.post("/{organization_id}/grant-minutes")
async def grant_apex_minutes(
    organization_id: str,
    payload: GrantMinutesPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Credit a NOKVO APEX org's Call Credits wallet WITHOUT a payment — a manual
    comp grant of ``minutes`` minutes of talk time. The credit lands in the SAME
    rupee-valued wallet a paid bundle does (``minute_purchases.rupees``, which the
    live dialer gate + dashboard read), valued at ``minutes × slab rate`` so the
    org sees +``minutes`` estimated minutes. (The dead Phase-3 ``minutes``-only
    ledger is NOT what the wallet reads — crediting ₹0 there added nothing.)
    APEX-only. High-risk, acknowledged + audited."""
    import uuid as _uuid
    from app.models.minute_purchase import MinutePurchase
    from app.services.minute_balance_service import apex_credit_summary
    from app.services.minute_pricing import flat_rate_for_minutes

    if not payload.acknowledge_comp:
        raise HTTPException(
            status_code=409,
            detail="Crediting minutes without a payment is a comp grant. Confirm to proceed (acknowledge_comp=true).",
        )
    try:
        minutes = int(payload.minutes)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Enter a whole number of minutes.")
    if minutes <= 0:
        raise HTTPException(status_code=400, detail="Minutes must be a positive number.")
    if minutes > 10_000_000:
        raise HTTPException(status_code=400, detail="That's over the 10,000,000-minute per-grant limit.")

    org = (
        await db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    if (org.product_tier or "") != "nokvo_apex":
        raise HTTPException(status_code=409, detail="Minute grants apply only to NOKVO APEX organizations.")

    # Value the comp at the bundle's slab rate so it credits the LIVE wallet and the
    # bundle's FIFO slab matches (minutes × ₹/min). 1 Call Credit ≡ ₹1.
    rate = flat_rate_for_minutes(minutes)
    credits = rate * minutes
    before = await apex_credit_summary(db, org.id)
    db.add(
        MinutePurchase(
            id=_uuid.uuid4(),
            organization_id=org.id,
            minutes=minutes,
            rupees=credits,            # ← lands in the live Call Credits wallet
            rate_per_minute=rate,
            source="comp_grant",
            # Synthetic unique id so the row is durable + idempotent-safe (the
            # purchases table is unique on razorpay_payment_id).
            razorpay_payment_id=f"comp:{_uuid.uuid4()}",
            razorpay_ref=f"superadmin:{current_user.id}",
        )
    )
    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="apex_minutes_granted",
            risk_level="high",
            target_type="organization",
            target_id=str(org.id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            before_state={"credits_remaining": before["credits_remaining"]},
            after_state={"credits_remaining": before["credits_remaining"] + float(credits)},
            metadata_={
                "minutes_granted": minutes,
                "credits_granted": float(credits),
                "comp_grant": True,
                "acknowledged": True,
            },
        )
    )
    await db.commit()
    after = await apex_credit_summary(db, org.id)
    return {
        "organization_id": str(org.id),
        "minutes_granted": minutes,
        "credits_granted": float(credits),
        # ``remaining_minutes`` mirrors the console row's live estimate so the
        # optimistic update + reload agree.
        "remaining_minutes": after["estimated_minutes_remaining"],
        **after,
    }


@router.post("/{organization_id}/retry-compliance")
async def retry_compliance(
    organization_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(_WRITE_ROLES)),
):
    """Re-file Plivo compliance for a tenant stuck at ``pending_compliance``.

    Reuses the end-user's already-uploaded Plivo documents (no re-upload needed)
    and files the ``ComplianceApplication`` if it never got filed. Once approved,
    the number poller rents + assigns the DID automatically."""
    org = (
        await db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    tenant_res = (
        await db.execute(select(TenantResources).where(TenantResources.organization_id == org.id))
    ).scalars().first()
    if tenant_res is None:
        raise HTTPException(status_code=409, detail="Tenant is not provisioned yet — nothing to retry.")

    legal = (org.legal_name or org.name or "").strip()
    if not legal:
        raise HTTPException(status_code=400, detail="Organization has no legal name on file.")

    from app.services.plivo_compliance_service import PlivoComplianceService

    try:
        result = await PlivoComplianceService.submit_compliance_and_allot_number(
            tenant_res,
            db,
            legal_name=legal,
            alias_name=org.alias_name,
            business_pan=org.business_pan,
            cin=org.cin,
            documents=[],  # reuse existing Plivo documents
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Compliance retry failed: {exc}")

    comp = (result.get("compliance") or {})
    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="organization_compliance_retried",
            risk_level="low",
            target_type="organization",
            target_id=str(organization_id),
            ip_address=request.client.host if request.client else None,
            user_agent=request.headers.get("user-agent"),
            request_id=request.headers.get("x-request-id"),
            metadata_={
                "application_id": comp.get("application_id"),
                "number_status": result.get("number_status"),
            },
        )
    )
    await db.commit()

    return {
        "organization_id": str(organization_id),
        "application_id": comp.get("application_id"),
        "number": result.get("number"),
        "number_status": result.get("number_status"),
        "error": comp.get("error"),
    }
