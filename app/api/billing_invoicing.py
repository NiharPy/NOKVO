"""Internal billing endpoints — the recurring usage-invoice mailer.

These are not tenant- or superadmin-authenticated; they're meant to be called
by a scheduler (Azure Container Apps cron job / external cron) and are guarded
by the ``X-Cron-Secret`` shared secret (``settings.BILLING_CRON_SECRET``).

Run daily, e.g.::

    curl -X POST https://<host>/api/internal/billing/usage-invoices/run \
         -H "X-Cron-Secret: $BILLING_CRON_SECRET"
"""
from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.db.session import get_db
from app.services.usage_invoice_service import UsageInvoiceService

router = APIRouter()


def require_cron_secret(x_cron_secret: str | None = Header(default=None)) -> None:
    """Reject any call without the configured shared secret (constant-time)."""
    expected = settings.BILLING_CRON_SECRET or ""
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Billing cron secret is not configured.",
        )
    if not x_cron_secret or not hmac.compare_digest(x_cron_secret, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing X-Cron-Secret.",
        )


@router.post("/usage-invoices/run", dependencies=[Depends(require_cron_secret)])
async def run_usage_invoices(
    dry_run: bool = Query(default=False, description="Preview without sending or recording."),
    db: AsyncSession = Depends(get_db),
):
    """Send usage invoices to every tenant whose monthly cycle has elapsed."""
    if not settings.USAGE_INVOICE_ENABLED:
        return {"enabled": False, "invoices_sent": 0, "items": []}
    return await UsageInvoiceService.run_due(db, dry_run=dry_run)


@router.get("/usage-invoices/due", dependencies=[Depends(require_cron_secret)])
async def preview_due_usage_invoices(db: AsyncSession = Depends(get_db)):
    """Dry-run preview: which tenants are due and for how much (no email sent)."""
    return await UsageInvoiceService.run_due(db, dry_run=True)
