"""Payment-gated onboarding — Razorpay monthly subscription.

Flow: signup creates the org in ``pending_payment`` (NO resources). The frontend
opens this module's ``create-subscription`` → Razorpay Checkout → ``verify``.
``verify`` (client) and ``webhook`` (Razorpay server-to-server, FAILURE-SAFE)
both converge on the idempotent :func:`activate_and_provision`, which provisions
resources exactly once and advances the org to ``pending_email_verification``.

Auth before a session exists: the payment endpoints accept the short-lived
``payment_token`` (a ``stage="payment"`` setup token issued by ``/signup``),
mirroring the TOTP-setup token flow. The webhook has no token — it identifies
the org via the subscription id.
"""
from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.nokvo_one_auth import _decode_setup_token, _hash_token
from app.core.config import settings
from app.models.email_verification import EmailVerification
from app.models.minute_purchase import MinutePurchase
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.subscription import Subscription
from app.models.tenant_resources import TenantResources
from app.services.email_service import EmailService
from app.services.minute_pricing import (
    MINUTES_MIN,
    apex_wallet_credit,
    cost_for_minutes,
    cost_paise,
    credited_minutes,
    flat_rate_for_minutes,
)
from app.services.nokvo_one_provisioning_service import NokvoOneProvisioningService
from app.services.razorpay_service import PLAN_CATALOG, RazorpayError, RazorpayService
from app.services.tool_flow_questions import ensure_tool_flow_questions

logger = logging.getLogger(__name__)
router = APIRouter()

# Retain fire-and-forget activation tasks — the loop holds only weak refs, so an
# untracked create_task can be GC'd mid-flight (losing a webhook's activation).
_bg_tasks: set[asyncio.Task] = set()


# ─────────── request bodies ───────────
class CreateSubscriptionRequest(BaseModel):
    payment_token: str
    plan: str  # "inbound_only" | "inbound_outbound" | "apex"
    minutes: int  # prepaid voice-minute bundle (flat-by-bracket), charged as a one-time addon
    # Legal acceptance recorded server-side for a defensible consent audit. Required
    # for APEX (its only terms gate is the payment step); optional/ignored for other
    # plans, which stamp terms via their own onboarding flow.
    terms_accepted: bool = False
    terms_version: str | None = None
    privacy_version: str | None = None
    # Optional referral: an affiliate number entered on the payment screen. A
    # valid code stamps org.affiliate_id (commission attribution); an invalid
    # one is logged and ignored — a typo must never block a paying customer.
    affiliate_code: str | None = None


# Upper bound on a single purchase — a fat-fingered / malicious request can't
# create an absurd order. 100,000 minutes ≈ ₹5.5L at the top rate.
_MINUTES_MAX = 100_000


def _validate_minutes(minutes: int) -> int:
    """Validate a requested minute bundle: a positive integer in
    ``[MINUTES_MIN, _MINUTES_MAX]``. Raises 400 otherwise. This is the single
    server-side gate every purchase (onboarding + top-up) passes through, so the
    flat-bracket price is always computed from a sane, server-validated quantity."""
    try:
        n = int(minutes)
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="Enter the number of minutes you need.")
    if n < MINUTES_MIN:
        raise HTTPException(status_code=400, detail=f"Minimum is {MINUTES_MIN} minutes.")
    if n > _MINUTES_MAX:
        raise HTTPException(
            status_code=400,
            detail=f"That's over the {_MINUTES_MAX:,}-minute per-purchase limit.",
        )
    return n


async def _record_minute_purchase(
    db: AsyncSession,
    *,
    organization_id: uuid.UUID,
    minutes: int,
    source: str,
    razorpay_payment_id: str | None,
    razorpay_ref: str | None,
    rupees=None,
    rate_per_minute=None,
) -> None:
    """Credit a paid minute bundle to the org, EXACTLY ONCE. Idempotent on
    ``razorpay_payment_id`` (the payment can't double-credit on a verify+webhook
    race or a Razorpay retry). A purchase with no payment id is skipped — we only
    credit confirmed payments.

    ONBOARDING bundles get a second, stronger key: at most one credit per
    ``razorpay_ref`` (the subscription id). The bundle is charged only on the
    subscription's FIRST invoice, but every monthly renewal webhook stamps a
    fresh payment id onto the Subscription row before ``_bg_activate`` runs —
    so per-payment-id idempotency alone re-credited the full bundle every
    cycle. Keying on the subscription ref makes any later payment event for
    the same subscription a no-op. (Top-ups keep payment-id-only dedupe: each
    top-up is its own Order/ref.)

    ``minutes`` is the CREDITED amount. ``rupees``/``rate_per_minute`` default to
    the flat-bracket price of ``minutes`` (Nokvo One: credited == billed), but
    APEX overrides them — it BILLS on the selected minutes while crediting
    floor(1.5×) minutes, so it passes the selected-minutes price explicitly."""
    if not minutes or minutes <= 0 or not razorpay_payment_id:
        return
    if source == "onboarding" and razorpay_ref:
        already_credited = (
            await db.execute(
                select(MinutePurchase).where(
                    MinutePurchase.source == "onboarding",
                    MinutePurchase.razorpay_ref == razorpay_ref,
                )
            )
        ).scalars().first()
        if already_credited is not None:
            return
    existing = (
        await db.execute(
            select(MinutePurchase).where(MinutePurchase.razorpay_payment_id == razorpay_payment_id)
        )
    ).scalars().first()
    if existing is not None:
        return
    db.add(
        MinutePurchase(
            id=uuid.uuid4(),
            organization_id=organization_id,
            minutes=int(minutes),
            rupees=cost_for_minutes(minutes) if rupees is None else rupees,
            rate_per_minute=flat_rate_for_minutes(minutes) if rate_per_minute is None else rate_per_minute,
            source=source,
            razorpay_payment_id=razorpay_payment_id,
            razorpay_ref=razorpay_ref,
        )
    )
    try:
        await db.commit()
    except IntegrityError:
        # Concurrent verify+webhook race: the other path inserted this payment id
        # first. UNIQUE(razorpay_payment_id) guarantees no double-credit, so the
        # loser just rolls back and treats the bundle as already credited.
        await db.rollback()
        return
    # New rupees landed — drop the cached gate sums so a just-topped-up org can
    # dial immediately instead of waiting out the cache TTL.
    from app.services.minute_balance_service import invalidate_balance_cache

    await invalidate_balance_cache(organization_id)


class VerifyPaymentRequest(BaseModel):
    payment_token: str
    razorpay_payment_id: str
    razorpay_subscription_id: str
    razorpay_signature: str


class PaymentStatusRequest(BaseModel):
    payment_token: str


def _org_id_from_token(payment_token: str) -> uuid.UUID:
    payload = _decode_setup_token(payment_token, expected_stage="payment")
    return uuid.UUID(payload["organization_id"])


# ─────────── the idempotent convergence point ───────────
async def activate_and_provision(db: AsyncSession, organization_id: uuid.UUID) -> dict:
    """Provision resources + activate the subscription EXACTLY ONCE.

    Called by ``verify`` (client), the ``webhook`` (server), and ``status``
    (resume). A ``FOR UPDATE`` lock on the org row serializes concurrent callers;
    the ``TenantResources`` existence check makes provisioning itself idempotent.
    """
    org = (
        await db.execute(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
    ).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    sub = (
        await db.execute(
            select(Subscription)
            .where(Subscription.organization_id == organization_id)
            .order_by(Subscription.created_at.desc())
            .with_for_update()
        )
    ).scalars().first()

    existing_tenant = (
        await db.execute(
            select(TenantResources).where(TenantResources.organization_id == organization_id)
        )
    ).scalars().first()

    # Idempotency: already fully provisioned + advanced → no-op.
    if existing_tenant is not None and org.status != "pending_payment":
        return {"provisioned": True, "org_status": org.status, "idempotent": True}

    plan = sub.plan if sub else None
    if sub is not None:
        sub.status = "active"
    if plan in PLAN_CATALOG:
        org.plan_type = plan
        org.calling_enabled = bool(PLAN_CATALOG[plan]["outbound"])

    # Real estate is the only vertical — assign it as the org enters onboarding
    # so the real-estate-gated onboarding steps (projects / brochure upload) work
    # DURING the wizard, not only after it. (The post-onboarding auto-assign in
    # the frontend runs too late for the projects step.)
    if not (org.industry or "").strip():
        org.industry = "real_estate"

    # Provision external resources once (guarded by the tenant existence check
    # under the row lock — a racing webhook waits here, then sees the tenant).
    if existing_tenant is None:
        provision = await NokvoOneProvisioningService.provision_or_raise(
            organization_id=org.id,
            organization_name=org.name,
            region=org.region or "southindia",
        )
        # Seed the vertical's tool-flow questions on the new tenant (mirrors the
        # /business-template step) so booking/lead flows are ready at runtime.
        prov_status, _ = ensure_tool_flow_questions(provision["provider_status"], org.industry)
        db.add(
            TenantResources(
                id=uuid.uuid4(),
                organization_id=org.id,
                tenant_id=provision["tenant_id"],
                azure_resource_group_name=provision["azure_resource_group_name"],
                azure_region=provision["azure_region"],
                qdrant_collection_name=provision["qdrant_collection_name"],
                qdrant_url_ref=provision["qdrant_url_ref"],
                redis_namespace=provision["redis_namespace"],
                storage_account_name=provision["storage_account_name"],
                storage_container_name=provision["storage_container_name"],
                blob_prefix=provision["blob_prefix"],
                provider_status=prov_status,
                provisioning_status=provision["provisioning_status"],
                provisioning_steps=provision["provisioning_steps"],
                cleanup_required=False,
            )
        )
        if sub is not None:
            sub.tenant_id = provision["tenant_id"]

    if sub is not None:
        sub.provisioned_at = datetime.now(timezone.utc)

    admin = (
        await db.execute(
            select(OrganizationUser)
            .where(OrganizationUser.organization_id == org.id, OrganizationUser.role == "admin")
            .limit(1)
        )
    ).scalars().first()

    # Past the payment gate → straight into the onboarding wizard (business KYC →
    # Plivo compliance + number → working hours → projects → agent → ToS). No
    # email-verification / MFA gate: the account is secured by password + a
    # completed payment. The admin is activated so they hold a session and can
    # drive the (status-gated) onboarding endpoints.
    if org.status == "pending_payment":
        org.status = "onboarding"
        org.onboarding_step = "business_details"
    if admin is not None and admin.status == "pending_payment":
        admin.status = "active"

    await db.commit()
    return {
        "provisioned": True,
        "org_status": org.status,
        "onboarding_step": org.onboarding_step,
    }


def _purchase_args_for_sub(sub) -> dict:
    """Credit args for a paid subscription's minute bundle. NOKVO APEX (plan=
    "apex") BILLS on the selected minutes and credits a CALL CREDITS wallet of
    ``cost × 1.5`` (the 50% bonus as spendable credits), tagged to the SELECTED-
    minute bundle so the FIFO slab the wallet deducts at is the bracket bought.
    Nokvo One credits exactly what was billed (rupees == cost)."""
    selected = int(getattr(sub, "minutes", 0) or 0)
    if getattr(sub, "plan", None) == "apex":
        return {
            "minutes": selected,
            "rupees": apex_wallet_credit(selected),
            "rate_per_minute": flat_rate_for_minutes(selected),
        }
    return {"minutes": selected}


def _subscription_is_paid(remote: dict) -> bool:
    """True when Razorpay's subscription object says it's genuinely paid/live.
    The single source of truth for granting service — used by the webhook's
    background activation AND the /payments/status resume so a forged/replayed
    webhook can never grant anything Razorpay hasn't confirmed."""
    return str((remote or {}).get("status") or "") in {"active", "authenticated"}


async def _apex_credit_cycle(db: AsyncSession, org: Organization, sub: Subscription, cycle_key: str | None) -> None:
    """NOKVO APEX plan subscription — credit THIS billing cycle's included minutes to the
    Call Credits wallet (plan rate + billed bonus, per-cycle idempotent), and on the FIRST
    paid invoice move the account to ``pending_activation`` (SuperAdmin then activates). We
    do NOT provision/onboard here — number provisioning + go-live are the manual activate."""
    from app.services.apex_billing_service import credit_apex_wallet
    from app.services.apex_plans import get_apex_rate, resolve_plan

    included = int(getattr(org, "apex_included_minutes", 0) or 0)
    rate = get_apex_rate(org)
    bonus = getattr(org, "apex_billed_bonus_pct", None)
    if bonus is None:
        bonus = resolve_plan(getattr(org, "apex_plan_code", None)).billed_bonus_pct
    # Only credit against a concrete per-cycle INVOICE key (money-safety: never create an
    # unkeyed monthly credit that a later invoice-keyed event would duplicate). An event with
    # no invoice id still advances status below; the charged event credits the cycle.
    credited = False
    if cycle_key:
        credited = await credit_apex_wallet(
            db,
            organization_id=org.id,
            minutes=included,
            rate=rate,
            bonus_pct=bonus,
            source="monthly_grant",
            razorpay_payment_id=getattr(sub, "razorpay_payment_id", None),
            razorpay_ref=sub.razorpay_subscription_id,
            razorpay_invoice_id=cycle_key,
        )
    if sub.status != "active":
        sub.status = "active"
    if org.status == "pending_payment":
        org.status = "pending_activation"
    db.add(org)
    db.add(sub)
    await db.commit()
    logger.info("APEX-PLAN cycle processed org=%s credited=%s status=%s", org.id, credited, org.status)


async def _bg_activate(
    organization_id: uuid.UUID, razorpay_subscription_id: str | None = None, cycle_key: str | None = None
) -> None:
    """Webhook's background activation on a fresh session — never blocks the 200.

    Failure-safe twin of ``verify``: provisions AND credits the prepaid-minute
    bundle (idempotent) in case the client-side verify was lost.

    ``razorpay_subscription_id`` pins the subscription the webhook was actually
    about — selecting latest-by-created_at silently no-op'd when the user opened
    checkout twice and paid the OLDER subscription.

    DEFENSE IN DEPTH: the subscription is re-verified against the Razorpay API
    (the source of truth) BEFORE anything is granted — so a forged or replayed
    webhook, even one that somehow slipped past signature validation, can never
    activate an org or credit minutes. Mirrors the gate in ``/payments/status``."""
    from app.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            _sub_q = select(Subscription).where(
                Subscription.organization_id == organization_id
            )
            if razorpay_subscription_id:
                _sub_q = _sub_q.where(
                    Subscription.razorpay_subscription_id == razorpay_subscription_id
                )
            sub = (
                await db.execute(_sub_q.order_by(Subscription.created_at.desc()))
            ).scalars().first()
            if sub is None or not sub.razorpay_subscription_id:
                logger.info("RAZORPAY: bg-activate skipped — no subscription for org %s", organization_id)
                return
            # Razorpay must confirm the subscription is genuinely paid/live.
            try:
                remote = await RazorpayService.fetch_subscription(sub.razorpay_subscription_id)
            except RazorpayError:
                logger.warning(
                    "RAZORPAY: bg-activate — could not verify subscription %s; NOT granting",
                    sub.razorpay_subscription_id,
                )
                return
            if not _subscription_is_paid(remote):
                logger.warning(
                    "RAZORPAY: bg-activate — subscription %s status=%r not paid; NOT granting",
                    sub.razorpay_subscription_id, remote.get("status"),
                )
                return
            # NOKVO APEX plan accounts take the plan-driven path: credit the cycle's minutes
            # and move to pending_activation (SuperAdmin activates manually). They are NEVER
            # auto-provisioned/onboarded here.
            org = (
                await db.execute(select(Organization).where(Organization.id == organization_id))
            ).scalars().first()
            if (
                settings.ENABLE_APEX_PLANS
                and org is not None
                and (org.product_tier or "") == "nokvo_apex"
                and org.apex_plan_code
            ):
                await _apex_credit_cycle(db, org, sub, cycle_key)
                return
            await activate_and_provision(db, organization_id)
            # Credit the onboarding minutes once the webhook has stamped the
            # subscription's payment id. Idempotent on the payment id.
            if sub.minutes and sub.razorpay_payment_id:
                await _record_minute_purchase(
                    db,
                    organization_id=organization_id,
                    source="onboarding",
                    razorpay_payment_id=sub.razorpay_payment_id,
                    razorpay_ref=sub.razorpay_subscription_id,
                    **_purchase_args_for_sub(sub),
                )
    except Exception:
        logger.exception("RAZORPAY: background activation failed for org %s", organization_id)


# ─────────── endpoints ───────────
@router.post("/payments/create-subscription")
async def create_subscription(payload: CreateSubscriptionRequest, db: AsyncSession = Depends(deps.get_db)):
    organization_id = _org_id_from_token(payload.payment_token)
    if payload.plan not in PLAN_CATALOG:
        raise HTTPException(status_code=400, detail="Unknown plan")
    org = (await db.execute(select(Organization).where(Organization.id == organization_id))).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")

    # APEX: legal acceptance is MANDATORY and recorded server-side, so a
    # subscription can never be created for APEX without a stamped, versioned
    # consent — even if the client-side gate is bypassed. Committed together with
    # the Subscription row below.
    if payload.plan == "apex":
        if not payload.terms_accepted:
            raise HTTPException(
                status_code=400,
                detail="You must accept the Terms & Conditions and Privacy Policy to continue.",
            )
        org.terms_accepted_at = datetime.now(timezone.utc)
        org.terms_version = (
            f"terms={(payload.terms_version or '?')};privacy={(payload.privacy_version or '?')}"
        )[:64]

    # Affiliate attribution: stamp the org (the accrual source of truth) while
    # it's still unpaid — the last valid code entered before the successful
    # payment wins. The code string in the Razorpay notes below is audit only.
    affiliate = None
    if (payload.affiliate_code or "").strip():
        from app.services.affiliate_service import resolve_active_affiliate_by_code

        affiliate = await resolve_active_affiliate_by_code(db, payload.affiliate_code)
        if affiliate is not None and org.status == "pending_payment":
            org.affiliate_id = affiliate.id
            org.affiliate_code_used = affiliate.affiliate_number
        elif affiliate is None:
            logger.warning(
                "AFFILIATE: unknown/inactive code %r entered at payment for org %s — ignored",
                payload.affiliate_code, org.id,
            )

    spec = PLAN_CATALOG[payload.plan]
    minutes = _validate_minutes(payload.minutes)
    minutes_paise = cost_paise(minutes)
    try:
        plan_id = await RazorpayService.ensure_plan(payload.plan)
        sub = await RazorpayService.create_subscription(
            plan_id,
            notes={
                "organization_id": str(org.id),
                "plan": payload.plan,
                "minutes": str(minutes),
                "affiliate_code": affiliate.affiliate_number if affiliate else "",
            },
            # One-time addon on the FIRST invoice = the prepaid-minute bundle, so
            # onboarding charges platform fee + minutes together; later cycles bill
            # only the plan amount.
            addons=[
                {
                    "item": {
                        "name": f"{minutes} prepaid voice minutes",
                        "amount": minutes_paise,
                        "currency": "INR",
                    }
                }
            ],
        )
    except RazorpayError as exc:
        logger.error("RAZORPAY: create-subscription failed: %s", exc)
        raise HTTPException(status_code=500, detail="Could not start the subscription. Please try again.")

    razorpay_subscription_id = str(sub.get("id") or "")
    if not razorpay_subscription_id:
        raise HTTPException(status_code=500, detail="Razorpay returned no subscription id")

    db.add(
        Subscription(
            id=uuid.uuid4(),
            organization_id=org.id,
            plan=payload.plan,
            amount_paise=spec["amount_paise"],
            currency="INR",
            minutes=minutes,
            razorpay_plan_id=plan_id,
            razorpay_subscription_id=razorpay_subscription_id,
            status="created",
            raw_event=sub,
        )
    )
    await db.commit()
    _is_apex = payload.plan == "apex"
    _credited = credited_minutes(minutes) if _is_apex else minutes
    return {
        "subscription_id": razorpay_subscription_id,
        "key_id": settings.RAZORPAY_KEY_ID,
        "plan": payload.plan,
        "affiliate_applied": affiliate is not None,
        "amount_paise": spec["amount_paise"],
        # First-invoice total the customer pays now (platform fee + minutes bundle).
        "minutes": minutes,
        "credited_minutes": _credited,  # ≈ minutes display (APEX: credits ÷ slab)
        # APEX wallet grant in Call Credits (cost × 1.5); null for Nokvo One (rupee
        # balance == amount paid, no bonus).
        "credits": float(apex_wallet_credit(minutes)) if _is_apex else None,
        "minutes_amount_paise": minutes_paise,
        "first_invoice_paise": spec["amount_paise"] + minutes_paise,
        "name": "NOKVO APEX" if _is_apex else "Nokvo One",
        "description": f"{spec['label']} — ₹{spec['amount_paise'] // 100}/mo + {_credited} min",
    }


@router.post("/payments/verify")
async def verify_payment(
    request: Request, payload: VerifyPaymentRequest, db: AsyncSession = Depends(deps.get_db)
):
    organization_id = _org_id_from_token(payload.payment_token)
    if not RazorpayService.verify_checkout_signature(
        payload.razorpay_payment_id, payload.razorpay_subscription_id, payload.razorpay_signature
    ):
        raise HTTPException(status_code=400, detail="Payment signature verification failed")

    sub = (
        await db.execute(
            select(Subscription).where(
                Subscription.razorpay_subscription_id == payload.razorpay_subscription_id,
                Subscription.organization_id == organization_id,
            )
        )
    ).scalars().first()
    if sub is None:
        raise HTTPException(status_code=404, detail="Subscription not found for this organization")
    sub.razorpay_payment_id = payload.razorpay_payment_id
    db.add(sub)
    await db.commit()

    result = await activate_and_provision(db, organization_id)

    # Credit the prepaid-minute bundle bought with this subscription (idempotent
    # on the payment id, so a verify+webhook race can't double-credit). APEX
    # credits floor(1.5×) the billed minutes via _purchase_args_for_sub.
    await _record_minute_purchase(
        db,
        organization_id=organization_id,
        source="onboarding",
        razorpay_payment_id=payload.razorpay_payment_id,
        razorpay_ref=payload.razorpay_subscription_id,
        **_purchase_args_for_sub(sub),
    )

    # Affiliate commission for the FIRST invoice (verify only ever runs during
    # onboarding). Amount is reconstructed server-side from our own Subscription
    # row — plan price + the same addon expression that priced the bundle —
    # never from the client payload. Races with the webhook resolve via the
    # unique payment id.
    from app.services.affiliate_service import accrue_affiliate_commission

    await accrue_affiliate_commission(
        db,
        organization_id=organization_id,
        razorpay_payment_id=payload.razorpay_payment_id,
        razorpay_subscription_id=payload.razorpay_subscription_id,
        amount_paise=int(sub.amount_paise or 0) + cost_paise(sub.minutes or 0),
    )

    # Issue a session so the now-active admin can drive the onboarding wizard
    # without an email-verification round-trip. (Google admins already hold one;
    # this simply refreshes it.)
    session = None
    org = (
        await db.execute(select(Organization).where(Organization.id == organization_id))
    ).scalars().first()
    admin = (
        await db.execute(
            select(OrganizationUser)
            .where(OrganizationUser.organization_id == organization_id, OrganizationUser.role == "admin")
            .limit(1)
        )
    ).scalars().first()
    if org is not None and admin is not None:
        from app.api.nokvo_one_auth import _issue_full_session

        session = await _issue_full_session(db, request, admin, org, mfa_completed=True)
    return {"ok": True, **result, "session": session}


@router.post("/payments/webhook")
async def razorpay_webhook(request: Request, db: AsyncSession = Depends(deps.get_db)):
    raw_body = await request.body()
    signature = request.headers.get("x-razorpay-signature")
    # Fail CLOSED in production: never process an unsigned payment webhook in prod
    # (an unauthenticated webhook + the activation path = free service). Dev/test
    # without a secret still works.
    if settings.is_production and not settings.RAZORPAY_WEBHOOK_SECRET:
        logger.error("RAZORPAY: webhook hit in production with no RAZORPAY_WEBHOOK_SECRET set — rejecting.")
        raise HTTPException(status_code=503, detail="Webhook not configured")
    # When a webhook secret is configured, enforce the signature.
    if settings.RAZORPAY_WEBHOOK_SECRET and not RazorpayService.verify_webhook_signature(raw_body, signature):
        raise HTTPException(status_code=403, detail="Invalid webhook signature")

    import json as _json

    try:
        event = _json.loads(raw_body.decode("utf-8"))
    except Exception:
        return {"ok": True}  # ignore unparseable bodies; always 200 so Razorpay doesn't hammer-retry

    event_type = str(event.get("event") or "")
    # Pull the subscription id from whichever entity the event carries.
    entity = (event.get("payload") or {})
    sub_entity = ((entity.get("subscription") or {}).get("entity")) or {}
    pay_entity = ((entity.get("payment") or {}).get("entity")) or {}
    inv_entity = ((entity.get("invoice") or {}).get("entity")) or {}
    razorpay_subscription_id = sub_entity.get("id") or pay_entity.get("subscription_id")
    # Per-cycle idempotency key for the APEX monthly grant: the INVOICE id (one per billing
    # cycle). Keyed strictly on the invoice — NOT the payment id — so that different events
    # for the SAME cycle (subscription.activated without a payment, then subscription.charged
    # with one) never resolve to different keys and double-credit. An event that carries no
    # invoice id simply doesn't credit here; the charged event (which always does) credits
    # once, and a missed cycle is recoverable via the SuperAdmin resync.
    cycle_key = inv_entity.get("id") or pay_entity.get("invoice_id")

    # Durable audit of every verified webhook — a failed/dropped handler can be replayed
    # from the SuperAdmin console (best-effort; never breaks the 200).
    try:
        from app.models.razorpay_webhook_event import RazorpayWebhookEvent

        db.add(
            RazorpayWebhookEvent(
                id=uuid.uuid4(),
                event_id=request.headers.get("x-razorpay-event-id"),
                event_type=event_type,
                subscription_id=razorpay_subscription_id,
                payment_id=pay_entity.get("id"),
                invoice_id=cycle_key,
                status="received",
                payload=event,
            )
        )
        await db.commit()
    except Exception:
        await db.rollback()
        logger.debug("RAZORPAY: webhook audit persist failed (best-effort)", exc_info=True)

    if event_type in {"subscription.activated", "subscription.charged", "subscription.authenticated", "payment.captured"} and razorpay_subscription_id:
        sub = (
            await db.execute(
                select(Subscription).where(Subscription.razorpay_subscription_id == razorpay_subscription_id)
            )
        ).scalars().first()
        if sub is not None:
            sub.raw_event = event
            if pay_entity.get("id"):
                sub.razorpay_payment_id = pay_entity["id"]
            await db.commit()
            # Affiliate commission for this confirmed charge. This branch is the
            # ONLY path that sees recurring monthly charges, and the payment
            # entity's `amount` is the actual charged paise — first invoice =
            # plan + minutes addon automatically, renewals = plan only. Events
            # without a payment entity (e.g. subscription.authenticated) are
            # skipped by the guard; top-ups are Order payments with no
            # subscription id and never enter this branch. Idempotent on the
            # payment id; best-effort (never breaks the webhook 200).
            if pay_entity.get("id") and pay_entity.get("amount"):
                from app.services.affiliate_service import accrue_affiliate_commission

                await accrue_affiliate_commission(
                    db,
                    organization_id=sub.organization_id,
                    razorpay_payment_id=str(pay_entity["id"]),
                    razorpay_subscription_id=str(razorpay_subscription_id),
                    amount_paise=int(pay_entity["amount"]),
                    raw_event=event,
                )
            # Provision in the background so we return 200 fast (Razorpay retries
            # on slow/failed deliveries; our activation is idempotent). Retained
            # in a module set — an untracked task can be GC'd mid-flight.
            _task = asyncio.create_task(
                _bg_activate(sub.organization_id, razorpay_subscription_id, cycle_key)
            )
            _bg_tasks.add(_task)
            _task.add_done_callback(_bg_tasks.discard)
    elif event_type in {"subscription.cancelled", "subscription.completed", "subscription.halted"} and razorpay_subscription_id:
        # The subscription has truly ENDED: a cancel-at-cycle-end reached its cycle
        # end (the operative event for our perpetual plans), a fixed-term ran its
        # course (`completed` — a harmless safety net that likely never fires here),
        # or repeated payment failure `halted` it. Mark it cancelled and SUSPEND
        # service so a cancelled account can't keep using paid voice features.
        # Idempotent. NOTE: the activation branch above never clears
        # cancel_at_period_end / un-cancels, so a renewal webhook racing a manual
        # cancel converges safely rather than resurrecting a cancelling sub.
        sub = (
            await db.execute(
                select(Subscription).where(Subscription.razorpay_subscription_id == razorpay_subscription_id)
            )
        ).scalars().first()
        if sub is not None:
            sub.status = "cancelled"
            sub.cancel_at_period_end = False  # no longer merely "scheduled" — it's over
            sub.raw_event = event
            org = await db.get(Organization, sub.organization_id)
            if org is not None:
                org.calling_enabled = False  # shared kill for the voice path
            await db.commit()
            logger.info(
                "RAZORPAY: subscription %s ended (%s) — service suspended for org %s",
                razorpay_subscription_id, event_type, sub.organization_id,
            )
    return {"ok": True}


@router.get("/payments/status")
async def payment_status(payment_token: str, db: AsyncSession = Depends(deps.get_db)):
    organization_id = _org_id_from_token(payment_token)
    org = (await db.execute(select(Organization).where(Organization.id == organization_id))).scalars().first()
    if org is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    sub = (
        await db.execute(
            select(Subscription)
            .where(Subscription.organization_id == organization_id)
            .order_by(Subscription.created_at.desc())
        )
    ).scalars().first()

    # Resume an interrupted flow: if Razorpay says the subscription is live but we
    # never provisioned (lost verify call AND webhook), finish it now.
    if (
        sub is not None
        and org.status == "pending_payment"
        and sub.razorpay_subscription_id
    ):
        try:
            remote = await RazorpayService.fetch_subscription(sub.razorpay_subscription_id)
            if _subscription_is_paid(remote):
                await activate_and_provision(db, organization_id)
                # Credit the onboarding bundle too (idempotent on the payment id), so a
                # resume that lost BOTH the client verify AND the webhook's _bg_activate
                # doesn't leave an ACTIVE org with an empty wallet. Needs the payment id
                # the webhook stamps; absent that there's nothing confirmed to credit
                # and the webhook will do it when it lands.
                if sub.minutes and sub.razorpay_payment_id:
                    await _record_minute_purchase(
                        db,
                        organization_id=organization_id,
                        source="onboarding",
                        razorpay_payment_id=sub.razorpay_payment_id,
                        razorpay_ref=sub.razorpay_subscription_id,
                        **_purchase_args_for_sub(sub),
                    )
                    # First-invoice affiliate commission on the resume path too
                    # (client verify AND webhook both lost). Same server-side
                    # reconstruction as verify; idempotent on the payment id.
                    from app.services.affiliate_service import accrue_affiliate_commission

                    await accrue_affiliate_commission(
                        db,
                        organization_id=organization_id,
                        razorpay_payment_id=sub.razorpay_payment_id,
                        razorpay_subscription_id=sub.razorpay_subscription_id,
                        amount_paise=int(sub.amount_paise or 0) + cost_paise(sub.minutes or 0),
                    )
                org = (await db.execute(select(Organization).where(Organization.id == organization_id))).scalars().first()
        except RazorpayError:
            pass

    return {
        "org_status": org.status,
        "plan": org.plan_type,
        "calling_enabled": org.calling_enabled,
        "subscription_status": (sub.status if sub else None),
        "provisioned": bool(sub and sub.provisioned_at),
        "needs_payment": org.status == "pending_payment",
    }


# ─────────── prepaid-minute balance + top-ups (authenticated) ───────────
def _topup_admin_dep():
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["active", "onboarding"], allowed_roles=["admin"]
    )


def _subscription_admin_dep():
    # Subscription management is shared by BOTH products (Nokvo One + NOKVO APEX),
    # org-scoped so an admin only ever touches their own org's subscription.
    return deps.RequireNokvoOneOrganization(
        allowed_statuses=["active", "onboarding"],
        allowed_roles=["admin"],
        allowed_product_tiers=["nokvo_one", "nokvo_apex"],
    )


class TopupOrderRequest(BaseModel):
    minutes: int


class TopupVerifyRequest(BaseModel):
    minutes: int
    razorpay_order_id: str
    razorpay_payment_id: str
    razorpay_signature: str


@router.get("/payments/minutes/balance")
async def minutes_balance_endpoint(
    user: OrganizationUser = Depends(_topup_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Remaining / purchased / consumed prepaid minutes + the slab ladder for the
    top-up calculator."""
    from app.services.minute_balance_service import balance_summary
    from app.services.minute_pricing import MINUTES_MIN, slab_ladder

    summary = await balance_summary(db, user.organization_id)
    return {
        **summary,
        "minutes_min": MINUTES_MIN,
        "minutes_max": _MINUTES_MAX,
        "slabs": [
            {
                "from_minute": s["from_minute"],
                "to_minute": s["to_minute"],
                "rupees_per_minute": str(s["rupees_per_minute"]),
            }
            for s in slab_ladder()
        ],
    }


@router.post("/payments/topup/create-order")
async def topup_create_order(
    payload: TopupOrderRequest,
    user: OrganizationUser = Depends(_topup_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Create a one-time Razorpay Order for a prepaid-minute top-up. The frontend
    opens checkout.js with the returned ``order_id``."""
    minutes = _validate_minutes(payload.minutes)
    amount_paise = cost_paise(minutes)
    try:
        order = await RazorpayService.create_order(
            amount_paise,
            notes={"organization_id": str(user.organization_id), "minutes": str(minutes), "kind": "minute_topup"},
            receipt=f"topup-{user.organization_id.hex[:18]}",
        )
    except RazorpayError as exc:
        logger.error("RAZORPAY: top-up create-order failed: %s", exc)
        raise HTTPException(status_code=500, detail="Could not start the top-up. Please try again.")
    order_id = str(order.get("id") or "")
    if not order_id:
        raise HTTPException(status_code=500, detail="Razorpay returned no order id")
    return {
        "order_id": order_id,
        "key_id": settings.RAZORPAY_KEY_ID,
        "minutes": minutes,
        "amount_paise": amount_paise,
        "name": "Nokvo One",
        "description": f"{minutes} prepaid voice minutes",
    }


@router.post("/payments/topup/verify")
async def topup_verify(
    payload: TopupVerifyRequest,
    user: OrganizationUser = Depends(_topup_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Verify a top-up order payment and credit the minutes (idempotent).

    STRICT enforcement: the credited minutes come from the SERVER-SIDE order (its
    Razorpay ``notes.minutes``, re-validated against the order ``amount``), NOT the
    client's payload — so a client can't pay for a small order and claim a large
    credit. The signature only proves the payment is genuine; binding to the
    fetched order binds it to what was actually ordered + paid."""
    if not RazorpayService.verify_order_signature(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
    ):
        raise HTTPException(status_code=400, detail="Payment signature verification failed")

    # Authoritative: re-fetch the order and read what was actually ordered.
    try:
        order = await RazorpayService.fetch_order(payload.razorpay_order_id)
    except RazorpayError as exc:
        logger.error("RAZORPAY: top-up order fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not confirm the order. Please try again.")

    notes = order.get("notes") or {}
    # The order must belong to THIS org (the notes we stamped at create time) — a
    # paid order can't be replayed to credit a different organization.
    if str(notes.get("organization_id") or "") != str(user.organization_id):
        raise HTTPException(status_code=403, detail="This order does not belong to your organization.")
    if str(order.get("status") or "") != "paid":
        raise HTTPException(status_code=400, detail="Order is not paid.")

    minutes = _validate_minutes(int(notes.get("minutes") or 0))
    # Defence in depth: the order amount MUST equal our server price for those
    # minutes — rejects any order whose amount was tampered with.
    if int(order.get("amount") or -1) != cost_paise(minutes):
        logger.warning(
            "RAZORPAY: top-up amount mismatch order=%s amount=%s expected=%s",
            payload.razorpay_order_id, order.get("amount"), cost_paise(minutes),
        )
        raise HTTPException(status_code=400, detail="Order amount mismatch.")

    await _record_minute_purchase(
        db,
        organization_id=user.organization_id,
        minutes=minutes,
        source="topup",
        razorpay_payment_id=payload.razorpay_payment_id,
        razorpay_ref=payload.razorpay_order_id,
    )
    from app.services.minute_balance_service import balance_rupees

    return {
        "ok": True,
        "minutes": minutes,
        "remaining_rupees": float(await balance_rupees(db, user.organization_id)),
    }


# ─────────── NOKVO APEX top-ups + balance (MINUTES model) ───────────
# APEX is a separate product: balance is in MINUTES, and every purchase BILLS on
# the selected minutes (flat slabs) but CREDITS floor(1.5×) minutes. These mirror
# the Nokvo One top-up endpoints but credit the bonus + report a minutes balance.
def _apex_topup_dep():
    return deps.RequireApexOrganization(allowed_statuses=["active", "onboarding"], allowed_roles=["admin"])


@router.get("/apex/payments/minutes/balance")
async def apex_minutes_balance_endpoint(
    user: OrganizationUser = Depends(_apex_topup_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """APEX Call Credits wallet (purchased / used / remaining credits +
    estimated minutes remaining) + the slab ladder for the top-up calculator.
    Credits are rupee-valued (1 ≡ ₹1) but the UI labels them 'Call Credits'."""
    from app.services.minute_balance_service import apex_credit_summary
    from app.services.minute_pricing import slab_ladder

    summary = await apex_credit_summary(db, user.organization_id)
    return {
        **summary,
        "minutes_min": MINUTES_MIN,
        "minutes_max": _MINUTES_MAX,
        "bonus_pct": 50,
        "slabs": [
            {"from_minute": s["from_minute"], "to_minute": s["to_minute"], "rupees_per_minute": str(s["rupees_per_minute"])}
            for s in slab_ladder()
        ],
    }


@router.post("/apex/payments/topup/create-order")
async def apex_topup_create_order(
    payload: TopupOrderRequest,
    user: OrganizationUser = Depends(_apex_topup_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    selected = _validate_minutes(payload.minutes)
    amount_paise = cost_paise(selected)  # BILL on selected (slabs)
    try:
        order = await RazorpayService.create_order(
            amount_paise,
            notes={"organization_id": str(user.organization_id), "minutes": str(selected), "kind": "apex_minute_topup"},
            receipt=f"apextop-{user.organization_id.hex[:16]}",
        )
    except RazorpayError as exc:
        logger.error("RAZORPAY: APEX top-up create-order failed: %s", exc)
        raise HTTPException(status_code=500, detail="Could not start the top-up. Please try again.")
    order_id = str(order.get("id") or "")
    if not order_id:
        raise HTTPException(status_code=500, detail="Razorpay returned no order id")
    return {
        "order_id": order_id,
        "key_id": settings.RAZORPAY_KEY_ID,
        "minutes": selected,
        "credits": float(apex_wallet_credit(selected)),  # Call Credits that land in the wallet
        "credited_minutes": credited_minutes(selected),  # ≈ minutes display (credits ÷ slab)
        "amount_paise": amount_paise,
        "name": "NOKVO APEX",
        "description": f"{float(apex_wallet_credit(selected)):,.0f} Call Credits (incl. 50% bonus)",
    }


@router.post("/apex/payments/topup/verify")
async def apex_topup_verify(
    payload: TopupVerifyRequest,
    user: OrganizationUser = Depends(_apex_topup_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Verify an APEX top-up order and credit the Call Credits wallet (cost×1.5).
    The selected quantity + amount come from the SERVER-SIDE order (strict — the
    client can't inflate the credit)."""
    if not RazorpayService.verify_order_signature(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
    ):
        raise HTTPException(status_code=400, detail="Payment signature verification failed")
    try:
        order = await RazorpayService.fetch_order(payload.razorpay_order_id)
    except RazorpayError as exc:
        logger.error("RAZORPAY: APEX top-up order fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not confirm the order. Please try again.")

    notes = order.get("notes") or {}
    if str(notes.get("organization_id") or "") != str(user.organization_id):
        raise HTTPException(status_code=403, detail="This order does not belong to your organization.")
    if str(order.get("status") or "") != "paid":
        raise HTTPException(status_code=400, detail="Order is not paid.")
    selected = _validate_minutes(int(notes.get("minutes") or 0))
    if int(order.get("amount") or -1) != cost_paise(selected):
        raise HTTPException(status_code=400, detail="Order amount mismatch.")

    await _record_minute_purchase(
        db,
        organization_id=user.organization_id,
        minutes=selected,
        source="topup",
        razorpay_payment_id=payload.razorpay_payment_id,
        razorpay_ref=payload.razorpay_order_id,
        rupees=apex_wallet_credit(selected),
        rate_per_minute=flat_rate_for_minutes(selected),
    )
    from app.services.minute_balance_service import apex_credit_summary

    summary = await apex_credit_summary(db, user.organization_id)
    return {
        "ok": True,
        "credits_added": float(apex_wallet_credit(selected)),
        "credited_minutes": credited_minutes(selected),
        **summary,
    }


# ─────────── NOKVO APEX plan billing (plan-driven; ENABLE_APEX_PLANS) ───────────
# Rate/bonus come from the org's stamped plan (not the quantity slab). Cancel reuses
# /payments/cancel-subscription (shared). GET /apex/plans is public for the request form.

def _apex_topup_paise(selected: int, rate) -> int:
    from decimal import ROUND_HALF_UP, Decimal

    return int((Decimal(int(selected)) * Decimal(str(rate)) * Decimal("100")).to_integral_value(rounding=ROUND_HALF_UP))


@router.get("/apex/plans")
async def apex_plans_catalog():
    """Public NOKVO APEX plan catalog (pricing/limits) — for the request-access form and
    the dashboard billing panel. Pricing is public info; no auth required."""
    from app.services.apex_plans import APEX_PLANS, plan_public_view

    return {"plans": [plan_public_view(p) for p in APEX_PLANS.values()]}


@router.get("/apex/billing")
async def apex_billing_summary(
    user: OrganizationUser = Depends(_apex_topup_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """The APEX dashboard billing panel: plan config, monthly amount, subscription status,
    and the Call Credits wallet (with plan-rate estimated minutes)."""
    if not settings.ENABLE_APEX_PLANS:
        raise HTTPException(status_code=404, detail="APEX plans are not enabled")
    from app.services.apex_plans import get_apex_rate, get_topup_bonus_pct, resolve_plan
    from app.services.minute_balance_service import consumed_rupees, purchased_rupees

    org = await db.get(Organization, user.organization_id)
    plan = resolve_plan(getattr(org, "apex_plan_code", None))
    rate = get_apex_rate(org)
    purchased = await purchased_rupees(db, org.id)
    consumed = await consumed_rupees(db, org.id)
    remaining = purchased - consumed
    est_minutes = int(remaining / rate) if rate and remaining > 0 else 0

    sub = (
        await db.execute(
            select(Subscription)
            .where(Subscription.organization_id == org.id)
            .order_by((Subscription.status == "active").desc(), Subscription.created_at.desc())
            .limit(1)
        )
    ).scalars().first()

    return {
        "plan_code": org.apex_plan_code,
        "plan_label": plan.label,
        "rate_per_minute": float(rate),
        "concurrency": int(getattr(org, "apex_concurrency", None) or plan.concurrency or 1),
        "included_minutes": int(getattr(org, "apex_included_minutes", 0) or 0),
        "platform_fee_inr": int(getattr(org, "apex_platform_fee_paise", 0) or 0) / 100,
        "monthly_inr": int(sub.amount_paise) / 100 if sub else None,
        "support_tier": org.apex_support_tier,
        "topup_bonus_pct": float(get_topup_bonus_pct(org)),
        "billed_bonus_pct": float(getattr(org, "apex_billed_bonus_pct", 0) or 0),
        "subscription_status": sub.status if sub else None,
        "cancel_at_period_end": bool(sub.cancel_at_period_end) if sub else False,
        "current_period_end": sub.current_period_end.isoformat() if sub and sub.current_period_end else None,
        "credits_purchased": float(purchased),
        "credits_used": float(consumed),
        "credits_remaining": float(remaining),
        "estimated_minutes_remaining": est_minutes,
        "org_status": org.status,
    }


@router.post("/apex/billing/topup/create-order")
async def apex_plan_topup_create_order(
    payload: TopupOrderRequest,
    user: OrganizationUser = Depends(_apex_topup_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Buy a top-up at the org's PLAN rate (not the quantity slab). The wallet is credited
    with the plan's top-up bonus; the customer is billed selected × plan rate."""
    if not settings.ENABLE_APEX_PLANS:
        raise HTTPException(status_code=404, detail="APEX plans are not enabled")
    from app.services.apex_plans import get_apex_rate, get_topup_bonus_pct, wallet_credit_for

    org = await db.get(Organization, user.organization_id)
    rate = get_apex_rate(org)
    selected = _validate_minutes(payload.minutes)
    amount_paise = _apex_topup_paise(selected, rate)
    bonus = get_topup_bonus_pct(org)
    try:
        order = await RazorpayService.create_order(
            amount_paise,
            notes={"organization_id": str(org.id), "minutes": str(selected), "kind": "apex_plan_topup"},
            receipt=f"apexptop-{org.id.hex[:15]}",
        )
    except RazorpayError as exc:
        logger.error("RAZORPAY: APEX plan top-up create-order failed: %s", exc)
        raise HTTPException(status_code=500, detail="Could not start the top-up. Please try again.")
    order_id = str(order.get("id") or "")
    if not order_id:
        raise HTTPException(status_code=500, detail="Razorpay returned no order id")
    credit = wallet_credit_for(selected, rate, bonus)
    return {
        "order_id": order_id,
        "key_id": settings.RAZORPAY_KEY_ID,
        "minutes": selected,
        "credits": float(credit),
        "amount_paise": amount_paise,
        "name": "NOKVO APEX",
        "description": (
            f"{float(credit):,.0f} Call Credits"
            + (f" (incl. {float(bonus):.0f}% bonus)" if bonus and bonus > 0 else "")
        ),
    }


@router.post("/apex/billing/topup/verify")
async def apex_plan_topup_verify(
    payload: TopupVerifyRequest,
    user: OrganizationUser = Depends(_apex_topup_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Verify a plan top-up order and credit the wallet at the plan rate + top-up bonus.
    Quantity/amount are taken from the SERVER-SIDE order (the client can't inflate it)."""
    if not settings.ENABLE_APEX_PLANS:
        raise HTTPException(status_code=404, detail="APEX plans are not enabled")
    from app.services.apex_billing_service import credit_apex_wallet
    from app.services.apex_plans import get_apex_rate, get_topup_bonus_pct

    if not RazorpayService.verify_order_signature(
        payload.razorpay_order_id, payload.razorpay_payment_id, payload.razorpay_signature
    ):
        raise HTTPException(status_code=400, detail="Payment signature verification failed")
    try:
        order = await RazorpayService.fetch_order(payload.razorpay_order_id)
    except RazorpayError as exc:
        logger.error("RAZORPAY: APEX plan top-up order fetch failed: %s", exc)
        raise HTTPException(status_code=502, detail="Could not confirm the order. Please try again.")

    notes = order.get("notes") or {}
    if str(notes.get("organization_id") or "") != str(user.organization_id):
        raise HTTPException(status_code=403, detail="This order does not belong to your organization.")
    if str(order.get("status") or "") != "paid":
        raise HTTPException(status_code=400, detail="Order is not paid.")

    org = await db.get(Organization, user.organization_id)
    rate = get_apex_rate(org)
    bonus = get_topup_bonus_pct(org)
    selected = _validate_minutes(int(notes.get("minutes") or 0))
    if int(order.get("amount") or -1) != _apex_topup_paise(selected, rate):
        raise HTTPException(status_code=400, detail="Order amount mismatch.")

    await credit_apex_wallet(
        db,
        organization_id=org.id,
        minutes=selected,
        rate=rate,
        bonus_pct=bonus,
        source="topup",
        razorpay_payment_id=payload.razorpay_payment_id,
        razorpay_ref=payload.razorpay_order_id,
    )
    from app.services.apex_plans import wallet_credit_for

    return {"ok": True, "credits_added": float(wallet_credit_for(selected, rate, bonus))}


# ─────────── cancel subscription ───────────
class CancelSubscriptionResponse(BaseModel):
    status: str
    cancel_at_period_end: bool
    current_period_end: str | None
    message: str


def _unix_to_dt(ts) -> datetime | None:
    try:
        ts = int(ts)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(ts, tz=timezone.utc) if ts > 0 else None


@router.post("/payments/cancel-subscription", response_model=CancelSubscriptionResponse)
async def cancel_subscription_endpoint(
    user: OrganizationUser = Depends(_subscription_admin_dep()),
    db: AsyncSession = Depends(deps.get_db),
):
    """Cancel the org's Razorpay subscription AT CYCLE END: service continues
    until ``current_period_end`` (the month already paid), then billing stops —
    they're never charged again. Idempotent: a second call returns the already-
    scheduled state without re-hitting Razorpay. Reactivation = re-subscribe."""
    # Latest subscription for this org, preferring an active one. Row-locked so a
    # renewal webhook can't interleave with the cancel (see the webhook race note).
    row = (
        await db.execute(
            select(Subscription)
            .where(Subscription.organization_id == user.organization_id)
            .order_by(
                (Subscription.status == "active").desc(),
                Subscription.created_at.desc(),
            )
            .with_for_update()
            .limit(1)
        )
    ).scalars().first()
    if row is None or row.status not in ("active", "created"):
        raise HTTPException(status_code=409, detail="No active subscription to cancel.")

    def _resp(msg: str) -> CancelSubscriptionResponse:
        return CancelSubscriptionResponse(
            status=row.status,
            cancel_at_period_end=row.cancel_at_period_end,
            current_period_end=row.current_period_end.isoformat() if row.current_period_end else None,
            message=msg,
        )

    # Already scheduled / ended → idempotent no-op (don't re-hit Razorpay).
    if row.cancel_at_period_end or row.status == "cancelled":
        return _resp("Cancellation is already scheduled — you won't be charged again.")

    # An active sub cancels at cycle end (keep the paid month); an unpaid `created`
    # sub was never charged, so cancel it immediately.
    at_cycle_end = row.status == "active"
    try:
        remote = await RazorpayService.cancel_subscription(
            row.razorpay_subscription_id, cancel_at_cycle_end=at_cycle_end
        )
    except RazorpayError as exc:
        msg = str(exc).lower()
        if "(400)" in msg and ("cancel" in msg or "already" in msg):
            remote = {}  # already cancelled on Razorpay's side → converge local state
        else:
            logger.warning("RAZORPAY: cancel failed for sub %s: %r", row.razorpay_subscription_id, exc)
            raise HTTPException(
                status_code=502,
                detail="Could not cancel the subscription with the payment provider. Please try again.",
            ) from exc

    row.cancel_at_period_end = True
    period_end = _unix_to_dt(remote.get("current_end") or remote.get("end_at"))
    if period_end is not None:
        row.current_period_end = period_end
    if str(remote.get("status") or "").lower() == "cancelled":
        # Immediate cancellation (unpaid `created` sub) — it's over now; suspend.
        row.status = "cancelled"
        row.cancel_at_period_end = False
        org = await db.get(Organization, row.organization_id)
        if org is not None:
            org.calling_enabled = False
    if remote:
        row.raw_event = remote
    await db.commit()

    if row.status == "cancelled":
        return _resp("Your subscription has been cancelled.")
    end_txt = row.current_period_end.date().isoformat() if row.current_period_end else "the end of your current billing cycle"
    return _resp(f"Subscription will end on {end_txt}; you won't be charged again.")
