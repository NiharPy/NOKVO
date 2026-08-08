"""NOKVO APEX account lifecycle — SuperAdmin-created, request-gated.

APEX has no self-serve signup. SuperAdmin creates the account from an access request:
this stamps the plan config, provisions everything **except phone numbers**, and (for a
chargeable plan) opens a Razorpay monthly subscription whose payment link is emailed to
the registered address. The account is then paid (Phase 4 webhook → ``pending_activation``
+ first-cycle credit) and **manually activated** by SuperAdmin (→ ``active``, numbers
provisioned best-effort).

Status flow:
  chargeable : (create) pending_payment → (paid webhook) pending_activation → (activate) active
  free_trial : (create) pending_activation [+1000-min credit] → (activate) active
Local dev without Razorpay configured short-circuits a chargeable create to
pending_activation + first-cycle credit so the flow is testable end to end.
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import security
from app.core.config import settings
from app.models.apex_access_request import ApexAccessRequest
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.subscription import Subscription
from app.models.tenant_resources import TenantResources
from app.services.apex_billing_service import credit_apex_wallet
from app.services.apex_plans import (
    VALID_PLAN_CODES,
    monthly_subscription_paise,
    resolve_plan,
    stamp_org_from_plan,
)
from app.services.email_service import EmailService
from app.services.nokvo_one_provisioning_service import NokvoOneProvisioningService
from app.services.razorpay_service import RazorpayService
from app.services.tool_flow_questions import ensure_tool_flow_questions

logger = logging.getLogger(__name__)

APEX_TIER = "nokvo_apex"


class ApexAccountError(RuntimeError):
    """Create/activate precondition failure (mapped to a 4xx by the endpoint)."""


def _razorpay_configured() -> bool:
    return bool(settings.PAYMENTS_ENABLED and settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET)


async def _existing_apex_email(db: AsyncSession, email: str) -> bool:
    row = (
        await db.execute(
            select(OrganizationUser.id)
            .join(Organization, Organization.id == OrganizationUser.organization_id)
            .where(OrganizationUser.email == email, Organization.product_tier == APEX_TIER)
            .limit(1)
        )
    ).first()
    return row is not None


async def _provision_except_numbers(db: AsyncSession, org: Organization) -> TenantResources:
    """Provision the tenant's non-telephony resources (blob/redis/etc.) and seed the
    vertical's tool-flow questions. Does NOT buy DIDs — numbers are provisioned at
    activation. Idempotent: returns the existing TenantResources if already provisioned."""
    existing = (
        await db.execute(select(TenantResources).where(TenantResources.organization_id == org.id))
    ).scalars().first()
    if existing is not None:
        return existing

    provision = await NokvoOneProvisioningService.provision_or_raise(
        organization_id=org.id,
        organization_name=org.name,
        region=org.region or "southindia",
    )
    prov_status, _ = ensure_tool_flow_questions(provision["provider_status"], org.industry)
    tr = TenantResources(
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
    db.add(tr)
    await db.flush()
    return tr


async def create_apex_account(
    db: AsyncSession,
    *,
    plan_code: str,
    company_name: str,
    admin_email: str,
    admin_name: str | None,
    phone: str | None = None,
    admin_password: str | None = None,
    enterprise_rate=None,
    enterprise_concurrency=None,
    trial_concurrency=None,
    complimentary_minutes: int = 0,
    request_id: uuid.UUID | None = None,
    region: str = "southindia",
    language: str = "en-IN",
    country_code: str = "IN",
) -> dict:
    """Create an APEX account on ``plan_code``. Returns a summary incl. the payment link
    (``short_url``) for chargeable plans. Raises :class:`ApexAccountError` on a bad plan,
    a duplicate APEX email, or missing enterprise overrides.

    ``complimentary_minutes`` grants free minutes on top of whatever the plan includes —
    credited at the org's own rate with NO bonus, and never billed. Applies to any plan."""
    plan_code = (plan_code or "").strip().lower()
    if plan_code not in VALID_PLAN_CODES:
        raise ApexAccountError(f"Unknown plan '{plan_code}'")
    if await _existing_apex_email(db, admin_email):
        raise ApexAccountError("An APEX account with this email already exists")

    plan = resolve_plan(plan_code)
    org = Organization(
        id=uuid.uuid4(),
        name=(company_name or "").strip() or admin_email.split("@")[0].capitalize(),
        admin_email=admin_email,
        admin_name=admin_name,
        email_domain=(admin_email.split("@")[-1] if "@" in admin_email else None),
        region=region,
        environment="staging",
        call_type="outbound",
        language=language,
        plan_type=None,
        product_tier=APEX_TIER,
        status="pending_payment",
        calling_enabled=True,
        stores_pii=True,
        record_calls=False,
        create_resource_group=False,
        twilio_auto_provision=False,
        industry="real_estate",
        country_code=country_code,
    )
    # Stamp the plan config (validates + writes concrete rate/concurrency; enterprise
    # requires the per-deal overrides).
    try:
        resolved = stamp_org_from_plan(
            org, plan_code,
            enterprise_rate=enterprise_rate,
            enterprise_concurrency=enterprise_concurrency,
            trial_concurrency=trial_concurrency,
        )
    except ValueError as exc:
        raise ApexAccountError(str(exc)) from exc
    db.add(org)
    await db.flush()

    generated_password = None
    if not admin_password:
        generated_password = secrets.token_urlsafe(12)
    admin_user = OrganizationUser(
        id=uuid.uuid4(),
        organization_id=org.id,
        email=admin_email,
        full_name=admin_name,
        role="admin",
        status="active",
        auth_provider="password",
        password_hash=security.get_password_hash(admin_password or generated_password),
        mfa_required=False,
        email_verified=False,
    )
    db.add(admin_user)
    await db.flush()

    # Provision everything except numbers.
    await _provision_except_numbers(db, org)

    payment_url: str | None = None
    subscription_id: str | None = None
    monthly_paise = (
        monthly_subscription_paise(resolved.rate_per_minute, resolved.included_minutes, resolved.platform_fee_paise)
        if plan.chargeable else 0
    )

    if not plan.chargeable:
        # Free Trial — no payment; grant the trial minutes now and await manual activation.
        # Set the status BEFORE crediting so credit_apex_wallet's commit persists both the
        # credit and the pending_activation status atomically.
        org.status = "pending_activation"
        await credit_apex_wallet(
            db, organization_id=org.id, minutes=resolved.included_minutes,
            rate=resolved.rate_per_minute, bonus_pct=resolved.billed_bonus_pct, source="trial_grant",
        )
    elif _razorpay_configured():
        plan_id = await RazorpayService.ensure_monthly_plan(
            f"NOKVO APEX — {resolved.label}", monthly_paise
        )
        sub = await RazorpayService.create_subscription(
            plan_id,
            notes={"organization_id": str(org.id), "apex_plan": plan_code, "kind": "apex_monthly"},
        )
        subscription_id = str(sub.get("id") or "")
        if not subscription_id:
            raise ApexAccountError("Razorpay returned no subscription id")
        payment_url = sub.get("short_url")
        db.add(
            Subscription(
                id=uuid.uuid4(),
                organization_id=org.id,
                plan="apex",
                amount_paise=monthly_paise,
                minutes=resolved.included_minutes,
                razorpay_plan_id=plan_id,
                razorpay_subscription_id=subscription_id,
                status="created",
            )
        )
        org.status = "pending_payment"
    else:
        # Local dev without Razorpay — short-circuit so the flow is testable: grant the
        # first cycle and move to pending_activation (status set before crediting so both
        # persist in credit_apex_wallet's commit).
        logger.warning("APEX create: Razorpay not configured — dev short-circuit (no charge) org=%s", org.id)
        org.status = "pending_activation"
        await credit_apex_wallet(
            db, organization_id=org.id, minutes=resolved.included_minutes,
            rate=resolved.rate_per_minute, bonus_pct=resolved.billed_bonus_pct,
            source="monthly_grant", razorpay_invoice_id=f"dev_{org.id}_cycle1",
        )

    # Complimentary minutes — a goodwill grant on top of the plan's own credit. Priced at
    # the org's rate with NO bonus (the operator entered the minutes they mean to give) and
    # tagged with its own source, so the ledger separates gifted minutes from paid ones.
    # credit_apex_wallet dedupes non-payment grants per (org, source): one grant per account.
    complimentary_minutes = max(0, int(complimentary_minutes or 0))
    if complimentary_minutes:
        await credit_apex_wallet(
            db, organization_id=org.id, minutes=complimentary_minutes,
            rate=resolved.rate_per_minute, bonus_pct=Decimal("0"),
            source="complimentary_grant",
        )

    if request_id is not None:
        req = (
            await db.execute(select(ApexAccessRequest).where(ApexAccessRequest.id == request_id))
        ).scalars().first()
        if req is not None:
            req.status = "converted"
            req.converted_org_id = org.id
            req.status_updated_at = datetime.now(timezone.utc)
            db.add(req)

    await db.commit()

    # Email the payment link (best-effort; a failure must not roll back a created account).
    if payment_url:
        try:
            await EmailService.send_apex_payment_link_email(
                admin_email, admin_name, org.name, resolved.label, monthly_paise, payment_url
            )
        except Exception:
            logger.warning("APEX create: payment-link email failed for org=%s", org.id, exc_info=True)

    return {
        "organization_id": str(org.id),
        "plan_code": plan_code,
        "status": org.status,
        "monthly_inr": monthly_paise / 100 if monthly_paise else 0,
        "chargeable": plan.chargeable,
        "concurrency": resolved.concurrency,
        "included_minutes": resolved.included_minutes,
        "complimentary_minutes": complimentary_minutes,
        "payment_url": payment_url,
        "razorpay_subscription_id": subscription_id,
        "generated_password": generated_password,  # non-null only when SuperAdmin didn't set one
    }


def plan_snapshot(org: Organization) -> dict:
    """The org's stamped plan config, for the before/after of a plan change (and the
    SuperAdmin console's plan panel)."""
    return {
        "plan_code": org.apex_plan_code,
        "rate_per_minute": float(org.apex_rate_per_minute) if org.apex_rate_per_minute is not None else None,
        "concurrency": int(org.apex_concurrency) if org.apex_concurrency is not None else None,
        "included_minutes": int(org.apex_included_minutes) if org.apex_included_minutes is not None else None,
        "platform_fee_inr": (int(org.apex_platform_fee_paise) / 100) if org.apex_platform_fee_paise is not None else None,
        "billed_bonus_pct": float(org.apex_billed_bonus_pct) if org.apex_billed_bonus_pct is not None else None,
        "topup_bonus_pct": float(org.apex_topup_bonus_pct) if org.apex_topup_bonus_pct is not None else None,
        "support_tier": org.apex_support_tier,
    }


# A subscription in one of these states is still billable at Razorpay — it's the one a
# plan change has to replace.
_LIVE_SUB_STATUSES = {"created", "authenticated", "active", "pending", "halted"}


async def _live_apex_subscription(db: AsyncSession, org_id: uuid.UUID) -> Subscription | None:
    """The org's current billable APEX subscription (newest live one), if any."""
    rows = (
        await db.execute(
            select(Subscription)
            .where(Subscription.organization_id == org_id, Subscription.plan == "apex")
            .order_by(Subscription.created_at.desc())
        )
    ).scalars().all()
    for sub in rows:
        if (sub.status or "") in _LIVE_SUB_STATUSES:
            return sub
    return None


async def change_apex_plan(
    db: AsyncSession,
    organization_id: uuid.UUID,
    *,
    plan_code: str,
    enterprise_rate=None,
    enterprise_concurrency=None,
    trial_concurrency=None,
    rebill: bool = True,
) -> dict:
    """Move an existing APEX account onto ``plan_code`` (upgrade, downgrade, or a re-stamp
    of the same plan to repair drift).

    Re-stamps EVERY plan column from the catalog — the same write path as creation — so the
    new rate, concurrency, included minutes, platform fee, bonuses and support tier all take
    effect together. Rate and concurrency apply to the very next call; the included minutes
    are granted by the paid invoice of the new subscription, exactly as they are at creation.

    THE WALLET IS NEVER TOUCHED. Existing Call Credits were bought at the old rate and stay
    exactly as they are (they simply buy more or fewer minutes at the new rate); a plan
    change neither grants nor claws back credit. Use the SuperAdmin grant action for goodwill.

    ``rebill`` (default True) handles the money side when the monthly amount changes: the
    live Razorpay subscription is replaced — a new one is opened at the new amount and its
    link emailed, then the old one is cancelled (at cycle end if it was paid, so the customer
    keeps the month they already bought). Pass ``rebill=False`` to re-stamp the plan and
    leave Razorpay untouched (comped accounts, or when billing is settled out of band).

    The new subscription is created BEFORE anything is written, so a Razorpay failure leaves
    the account exactly as it was. If cancelling the OLD subscription then fails, the change
    still lands and ``old_subscription_cancel_error`` is returned — the operator must cancel
    it in the Razorpay dashboard or the customer is billed twice.
    """
    plan_code = (plan_code or "").strip().lower()
    if plan_code not in VALID_PLAN_CODES:
        raise ApexAccountError(f"Unknown plan '{plan_code}'")

    org = (
        await db.execute(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
    ).scalars().first()
    if org is None:
        raise ApexAccountError("Organization not found")
    if (org.product_tier or "") != APEX_TIER:
        raise ApexAccountError("Not an APEX organization")

    before = plan_snapshot(org)

    # Validate + compute the target config WITHOUT mutating the org yet: stamp onto a
    # detached probe so a bad enterprise/trial override raises before anything changes.
    class _Probe:
        pass

    try:
        resolved = stamp_org_from_plan(
            _Probe(), plan_code,
            enterprise_rate=enterprise_rate,
            enterprise_concurrency=enterprise_concurrency,
            trial_concurrency=trial_concurrency,
        )
    except ValueError as exc:
        raise ApexAccountError(str(exc)) from exc

    after = {
        "plan_code": resolved.code,
        "rate_per_minute": float(resolved.rate_per_minute),
        "concurrency": int(resolved.concurrency),
        "included_minutes": int(resolved.included_minutes),
        "platform_fee_inr": resolved.platform_fee_paise / 100,
        "billed_bonus_pct": float(resolved.billed_bonus_pct),
        "topup_bonus_pct": float(resolved.topup_bonus_pct),
        "support_tier": resolved.support_tier,
    }
    changed = before != after

    monthly_paise = (
        monthly_subscription_paise(
            resolved.rate_per_minute, resolved.included_minutes, resolved.platform_fee_paise
        )
        if resolved.chargeable
        else 0
    )

    old_sub = await _live_apex_subscription(db, org.id)
    old_monthly_paise = int(old_sub.amount_paise or 0) if old_sub is not None else 0
    # Replace the mandate when the recurring amount actually moves, or when the account
    # leaves/enters a chargeable plan. Re-stamping an identical plan bills nothing.
    needs_rebill = rebill and (monthly_paise != old_monthly_paise)

    payment_url: str | None = None
    new_subscription_id: str | None = None
    new_plan_id: str | None = None
    cancel_error: str | None = None
    billing_note: str | None = None

    if needs_rebill and monthly_paise > 0:
        if not _razorpay_configured():
            billing_note = "Razorpay is not configured — the plan was re-stamped but no new subscription was opened."
        else:
            # Create FIRST: a failure here must leave the account untouched.
            try:
                new_plan_id = await RazorpayService.ensure_monthly_plan(
                    f"NOKVO APEX — {resolved.label}", monthly_paise
                )
                sub = await RazorpayService.create_subscription(
                    new_plan_id,
                    notes={
                        "organization_id": str(org.id),
                        "apex_plan": plan_code,
                        "kind": "apex_monthly",
                        "plan_change_from": before.get("plan_code") or "",
                    },
                )
            except Exception as exc:
                raise ApexAccountError(f"Could not open the new subscription: {exc}") from exc
            new_subscription_id = str(sub.get("id") or "")
            if not new_subscription_id:
                raise ApexAccountError("Razorpay returned no subscription id")
            payment_url = sub.get("short_url")
    elif needs_rebill and monthly_paise == 0:
        billing_note = "Free Trial is not charged — the existing subscription was cancelled."

    # ── Everything below is the committed change ──
    stamp_org_from_plan(
        org, plan_code,
        enterprise_rate=enterprise_rate,
        enterprise_concurrency=enterprise_concurrency,
        trial_concurrency=trial_concurrency,
    )
    db.add(org)

    if needs_rebill and old_sub is not None:
        # Cancel at cycle end for a PAID subscription (the customer keeps the month they
        # bought); immediately for one that was never charged.
        at_cycle_end = (old_sub.status or "") not in {"created", "pending"}
        if _razorpay_configured() and old_sub.razorpay_subscription_id:
            try:
                await RazorpayService.cancel_subscription(
                    old_sub.razorpay_subscription_id, cancel_at_cycle_end=at_cycle_end
                )
            except Exception as exc:
                cancel_error = str(exc)
                logger.error(
                    "APEX plan change: could not cancel old subscription %s for org=%s — "
                    "CANCEL IT MANUALLY or the customer is double-billed: %s",
                    old_sub.razorpay_subscription_id, org.id, exc,
                )
            # Mirror the remote state locally ONLY when the remote cancel actually ran —
            # a row marked cancelled while the mandate is still live at Razorpay would hide
            # a double-billing account from the next operator.
            if cancel_error is None:
                old_sub.cancel_at_period_end = at_cycle_end
                if not at_cycle_end:
                    old_sub.status = "cancelled"
                db.add(old_sub)

    if new_subscription_id:
        db.add(
            Subscription(
                id=uuid.uuid4(),
                organization_id=org.id,
                plan="apex",
                amount_paise=monthly_paise,
                minutes=resolved.included_minutes,
                razorpay_plan_id=new_plan_id,
                razorpay_subscription_id=new_subscription_id,
                status="created",
            )
        )

    await db.commit()

    # Notify the customer (best-effort — a mail failure must not undo a committed change).
    # Also mail when only the BILLING moved (``changed`` is False but a new subscription was
    # opened, e.g. putting a comped account on a real mandate) — that link is the whole point.
    if changed or payment_url:
        try:
            await EmailService.send_apex_plan_change_email(
                org.admin_email, org.admin_name, org.name,
                resolve_plan(before.get("plan_code")).label if before.get("plan_code") else "—",
                resolved.label, monthly_paise, payment_url,
            )
        except Exception:
            logger.warning("APEX plan change: notification email failed for org=%s", org.id, exc_info=True)

    logger.info(
        "APEX plan change org=%s %s → %s rebill=%s new_sub=%s",
        org.id, before.get("plan_code"), plan_code, needs_rebill, new_subscription_id,
    )
    return {
        "organization_id": str(org.id),
        "changed": changed,
        "before": before,
        "after": after,
        "status": org.status,
        "monthly_inr": monthly_paise / 100,
        "previous_monthly_inr": old_monthly_paise / 100,
        "rebilled": bool(new_subscription_id),
        "payment_url": payment_url,
        "razorpay_subscription_id": new_subscription_id,
        "old_subscription_cancel_error": cancel_error,
        "billing_note": billing_note,
        "wallet_unchanged": True,
    }


async def activate_apex_account(db: AsyncSession, organization_id: uuid.UUID) -> dict:
    """Manually activate a paid APEX account. Locks the org, asserts it's an APEX org in
    ``pending_activation`` with a funded wallet, then flips it to ``active`` and provisions
    numbers (best-effort — requires compliance; falls back to the console auto-provision if
    not yet ready). Idempotent: a second call on an already-active org is a no-op."""
    from app.services.minute_balance_service import purchased_rupees

    org = (
        await db.execute(
            select(Organization).where(Organization.id == organization_id).with_for_update()
        )
    ).scalars().first()
    if org is None:
        raise ApexAccountError("Organization not found")
    if (org.product_tier or "") != APEX_TIER:
        raise ApexAccountError("Not an APEX organization")
    if org.status == "active":
        return {"organization_id": str(org.id), "status": "active", "idempotent": True}
    if org.status != "pending_activation":
        raise ApexAccountError(f"Account is '{org.status}', not 'pending_activation' — cannot activate yet")
    if (await purchased_rupees(db, org.id)) <= 0:
        raise ApexAccountError("No wallet credit yet — payment must land before activation")

    org.status = "active"
    org.apex_activated_at = datetime.now(timezone.utc)
    db.add(org)
    await db.commit()

    numbers_provisioned = False
    number_error: str | None = None
    tr = (
        await db.execute(select(TenantResources).where(TenantResources.organization_id == org.id))
    ).scalars().first()
    if tr is not None:
        try:
            from app.services.plivo_bulk_provisioning_service import PlivoBulkProvisioningService

            cfg = await PlivoBulkProvisioningService.start_auto_provision(tr, db, superadmin_id=None)
            numbers_provisioned = bool(cfg.get("enabled"))
            number_error = cfg.get("error")
        except Exception as exc:  # compliance not ready / transient — activate anyway
            number_error = str(exc)
            logger.warning("APEX activate: number provisioning deferred for org=%s: %s", org.id, exc)

    return {
        "organization_id": str(org.id),
        "status": org.status,
        "activated_at": org.apex_activated_at.isoformat() if org.apex_activated_at else None,
        "numbers_provisioned": numbers_provisioned,
        "number_error": number_error,
    }
