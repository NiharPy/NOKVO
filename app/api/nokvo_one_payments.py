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
from sqlalchemy.ext.asyncio import AsyncSession

from app.api import deps
from app.api.nokvo_one_auth import _decode_setup_token, _hash_token
from app.core.config import settings
from app.models.email_verification import EmailVerification
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.subscription import Subscription
from app.models.tenant_resources import TenantResources
from app.services.email_service import EmailService
from app.services.nokvo_one_provisioning_service import NokvoOneProvisioningService
from app.services.razorpay_service import PLAN_CATALOG, RazorpayError, RazorpayService
from app.services.tool_flow_questions import ensure_tool_flow_questions

logger = logging.getLogger(__name__)
router = APIRouter()


# ─────────── request bodies ───────────
class CreateSubscriptionRequest(BaseModel):
    payment_token: str
    plan: str  # "inbound_only" | "inbound_outbound"


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


async def _bg_activate(organization_id: uuid.UUID) -> None:
    """Webhook's background activation on a fresh session — never blocks the 200."""
    from app.db.session import AsyncSessionLocal

    try:
        async with AsyncSessionLocal() as db:
            await activate_and_provision(db, organization_id)
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

    spec = PLAN_CATALOG[payload.plan]
    try:
        plan_id = await RazorpayService.ensure_plan(payload.plan)
        sub = await RazorpayService.create_subscription(
            plan_id, notes={"organization_id": str(org.id), "plan": payload.plan}
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
            razorpay_plan_id=plan_id,
            razorpay_subscription_id=razorpay_subscription_id,
            status="created",
            raw_event=sub,
        )
    )
    await db.commit()
    return {
        "subscription_id": razorpay_subscription_id,
        "key_id": settings.RAZORPAY_KEY_ID,
        "plan": payload.plan,
        "amount_paise": spec["amount_paise"],
        "name": "Nokvo One",
        "description": f"{spec['label']} — monthly",
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
    # When a webhook secret is configured, enforce the signature. (Without it the
    # endpoint still works for local/test, but is unauthenticated — set the
    # secret in prod.)
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
    razorpay_subscription_id = sub_entity.get("id") or pay_entity.get("subscription_id")

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
            # Provision in the background so we return 200 fast (Razorpay retries
            # on slow/failed deliveries; our activation is idempotent).
            asyncio.create_task(_bg_activate(sub.organization_id))
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
            if str(remote.get("status") or "") in {"active", "authenticated"}:
                await activate_and_provision(db, organization_id)
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
