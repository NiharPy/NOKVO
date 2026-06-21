"""Recurring usage-invoice mailer.

Bills each onboarded tenant for the voice minutes they used, one calendar month
from the day they last paid (or from the day they first paid, for the first
cycle). Designed to be hit by a daily scheduler: it finds every tenant whose
next cycle has elapsed, emails them an invoice for that window, and records a
``UsageInvoice`` row so the same cycle is never billed twice. A tenant several
cycles behind is caught up (bounded by ``USAGE_INVOICE_MAX_CYCLES_PER_RUN``).
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.call_cost import CallCost
from app.models.organization import Organization
from app.models.subscription import Subscription
from app.models.usage_invoice import UsageInvoice
from app.services.call_cost_calculator import rupees_display
from app.services.email_service import EmailService

logger = logging.getLogger(__name__)


def _add_one_month(dt: datetime) -> datetime:
    """``dt`` + one calendar month, clamping the day for short months."""
    month = dt.month + 1
    year = dt.year + (month - 1) // 12
    month = (month - 1) % 12 + 1
    # Clamp e.g. Jan 31 → Feb 28/29.
    day = dt.day
    while True:
        try:
            return dt.replace(year=year, month=month, day=day)
        except ValueError:
            day -= 1
            if day < 28:  # pragma: no cover — defensive, never reached
                raise


def _period_label(start: datetime, end: datetime) -> str:
    return f"{start.strftime('%d %b %Y')} – {end.strftime('%d %b %Y')}"


def _invoice_number(org_id: str, start: datetime) -> str:
    return f"INV-{start.strftime('%Y%m')}-{org_id[:8].upper()}"


class UsageInvoiceService:
    @staticmethod
    async def _window_usage(
        db: AsyncSession, org_id, start: datetime, end: datetime
    ) -> tuple[Decimal, int, int]:
        """(amount_inr, billed_minutes, call_count) over [start, end)."""
        row = (
            await db.execute(
                select(
                    func.coalesce(func.sum(CallCost.rupees), 0),
                    func.coalesce(func.sum(CallCost.billed_minutes), 0),
                    func.count(CallCost.id),
                ).where(
                    CallCost.organization_id == org_id,
                    CallCost.started_at >= start,
                    CallCost.started_at < end,
                )
            )
        ).one()
        return Decimal(str(row[0] or 0)), int(row[1] or 0), int(row[2] or 0)

    @classmethod
    async def run_due(
        cls,
        db: AsyncSession,
        *,
        now: datetime | None = None,
        dry_run: bool = False,
    ) -> dict:
        """Process every tenant whose billing cycle has elapsed."""
        now = now or datetime.now(timezone.utc)
        cap = max(1, int(settings.USAGE_INVOICE_MAX_CYCLES_PER_RUN or 6))
        dashboard_url = settings.NOKVO_ONE_PUBLIC_BASE_URL.rstrip("/")

        # First-payment anchor per org (earliest provisioned subscription).
        first_paid_sq = (
            select(
                Subscription.organization_id.label("oid"),
                func.min(Subscription.provisioned_at).label("first_paid"),
            )
            .where(Subscription.provisioned_at.isnot(None))
            .group_by(Subscription.organization_id)
            .subquery()
        )
        org_rows = (
            await db.execute(
                select(Organization, first_paid_sq.c.first_paid).join(
                    first_paid_sq,
                    first_paid_sq.c.oid == Organization.id,
                    isouter=True,
                ).where(Organization.status == "active")
            )
        ).all()

        # Existing invoice anchors per org: the latest period_end is the next
        # cycle's start; the set of period_starts guards against duplicates.
        existing_rows = (
            await db.execute(
                select(
                    UsageInvoice.organization_id,
                    func.max(UsageInvoice.period_end),
                ).group_by(UsageInvoice.organization_id)
            )
        ).all()
        last_period_end = {str(oid): pe for oid, pe in existing_rows}

        summary = {
            "ran_at": now.isoformat(),
            "dry_run": dry_run,
            "orgs_considered": len(org_rows),
            "invoices_sent": 0,
            "skipped_zero": 0,
            "failed": 0,
            "items": [],
        }

        for org, first_paid in org_rows:
            oid = str(org.id)
            email = (org.admin_email or "").strip()
            anchor = last_period_end.get(oid) or first_paid or org.created_at
            if anchor is None:
                continue
            if anchor.tzinfo is None:
                anchor = anchor.replace(tzinfo=timezone.utc)

            cycles = 0
            while cycles < cap:
                period_end = _add_one_month(anchor)
                if period_end > now:
                    break  # cycle not yet complete
                period_start = anchor

                amount, minutes, calls = await cls._window_usage(
                    db, org.id, period_start, period_end
                )
                amount_display = f"₹{rupees_display(amount):.2f}"
                item = {
                    "organization_id": oid,
                    "organization_name": org.name,
                    "email": email,
                    "period_start": period_start.isoformat(),
                    "period_end": period_end.isoformat(),
                    "minutes": minutes,
                    "calls": calls,
                    "amount_inr": str(amount),
                    "amount_display": amount_display,
                }

                if minutes <= 0:
                    item["status"] = "skipped_zero"
                    summary["skipped_zero"] += 1
                    if not dry_run:
                        db.add(
                            UsageInvoice(
                                organization_id=org.id,
                                tenant_id=org.tenant_id if hasattr(org, "tenant_id") else None,
                                period_start=period_start,
                                period_end=period_end,
                                minutes=0,
                                call_count=calls,
                                amount_inr=Decimal("0"),
                                status="skipped_zero",
                                email_to=email or None,
                            )
                        )
                elif not email:
                    item["status"] = "failed"
                    item["error"] = "no admin email"
                    summary["failed"] += 1
                    if not dry_run:
                        db.add(
                            UsageInvoice(
                                organization_id=org.id,
                                period_start=period_start,
                                period_end=period_end,
                                minutes=minutes,
                                call_count=calls,
                                amount_inr=amount,
                                status="failed",
                                error="no admin email",
                            )
                        )
                else:
                    invoice_no = _invoice_number(oid, period_start)
                    status = "sent"
                    error = None
                    if not dry_run:
                        try:
                            await EmailService.send_usage_invoice_email(
                                email,
                                org_name=org.name,
                                invoice_number=invoice_no,
                                period_label=_period_label(period_start, period_end),
                                minutes=minutes,
                                call_count=calls,
                                amount_display=amount_display,
                                dashboard_url=dashboard_url,
                            )
                        except Exception as exc:  # noqa: BLE001
                            status = "failed"
                            error = str(exc)
                            logger.exception("usage-invoice email failed for %s", email)
                        db.add(
                            UsageInvoice(
                                organization_id=org.id,
                                period_start=period_start,
                                period_end=period_end,
                                minutes=minutes,
                                call_count=calls,
                                amount_inr=amount,
                                status=status,
                                email_to=email,
                                error=error,
                                sent_at=now if status == "sent" else None,
                            )
                        )
                    item["status"] = status
                    item["invoice_number"] = invoice_no
                    if error:
                        item["error"] = error
                    if status == "sent":
                        summary["invoices_sent"] += 1
                    else:
                        summary["failed"] += 1

                summary["items"].append(item)
                anchor = period_end
                cycles += 1

        if not dry_run:
            await db.commit()
        return summary
