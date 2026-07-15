"""Repair the monthly onboarding-bundle re-credit bug (one-time data fix).

THE BUG (fixed in code alongside this script): every monthly renewal webhook
stamped a fresh payment id on the Subscription row before ``_bg_activate`` ran,
so ``_record_minute_purchase``'s per-payment-id idempotency passed again each
cycle and the FULL onboarding minute bundle was re-credited every month. Any
org past month 1 accumulated duplicate ``minute_purchases`` rows:
``source='onboarding'`` with the SAME ``razorpay_ref`` (subscription id) but a
different payment id per cycle.

THE REPAIR: per (organization_id, razorpay_ref), keep the EARLIEST onboarding
row (the genuine first-invoice bundle) and delete the later duplicates — they
were never real purchases, so removing them is honest bookkeeping. Balances
recompute automatically (balance = SUM(purchases) − consumed); the gate cache
is invalidated per affected org.

⚠️ An org that already SPENT the phantom credits can go negative and will be
blocked from dialing until it tops up — review the dry-run's "balance after"
column before applying; that call is a business decision.

Run from repo root:
    source venv/bin/activate
    python3 scripts/fix_duplicate_onboarding_credits.py            # DRY RUN (default)
    python3 scripts/fix_duplicate_onboarding_credits.py --apply    # delete duplicates
"""
from __future__ import annotations

import asyncio
import logging
import sys
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

# Runnable as `python3 scripts/fix_duplicate_onboarding_credits.py` from the
# repo root without PYTHONPATH gymnastics.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.core.config import settings
from app.models.minute_purchase import MinutePurchase
from app.models.organization import Organization
from app.services.minute_balance_service import balance_rupees, invalidate_balance_cache

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("fix-onboarding-dupes")


async def main() -> int:
    apply = "--apply" in sys.argv[1:]

    eng = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    Session = async_sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)

    async with Session() as db:
        rows = (
            await db.execute(
                select(MinutePurchase)
                .where(MinutePurchase.source == "onboarding")
                .order_by(MinutePurchase.created_at.asc())
            )
        ).scalars().all()

        # Group by the bundle's true identity; the first row per group is the
        # genuine purchase, everything after it is the bug.
        groups: dict[tuple, list[MinutePurchase]] = defaultdict(list)
        for row in rows:
            groups[(row.organization_id, row.razorpay_ref or f"__none__{row.id}")].append(row)

        duplicates = {key: grp[1:] for key, grp in groups.items() if len(grp) > 1}
        if not duplicates:
            log.info("No duplicate onboarding credits found — nothing to do.")
            await eng.dispose()
            return 0

        affected_orgs = sorted({key[0] for key in duplicates}, key=str)
        log.info(
            "%s duplicate onboarding credit row(s) across %s org(s)%s",
            sum(len(v) for v in duplicates.values()),
            len(affected_orgs),
            "" if apply else "  [DRY RUN — pass --apply to delete]",
        )

        total_minutes = 0
        total_rupees = Decimal("0")
        for (org_id, ref), dupes in sorted(duplicates.items(), key=lambda kv: str(kv[0][0])):
            org = await db.get(Organization, org_id)
            dup_minutes = sum(int(d.minutes or 0) for d in dupes)
            dup_rupees = sum(Decimal(str(d.rupees or 0)) for d in dupes)
            total_minutes += dup_minutes
            total_rupees += dup_rupees
            before = await balance_rupees(db, org_id)
            log.info(
                "org=%s (%s) sub=%s: %d duplicate row(s) → remove %d min / ₹%s "
                "(balance ₹%s → ₹%s) payment_ids=%s",
                org_id,
                getattr(org, "name", "?"),
                ref,
                len(dupes),
                dup_minutes,
                dup_rupees,
                before,
                before - dup_rupees,
                [d.razorpay_payment_id for d in dupes],
            )
            if apply:
                for d in dupes:
                    await db.delete(d)

        if apply:
            await db.commit()
            for org_id in affected_orgs:
                await invalidate_balance_cache(org_id)
            log.info(
                "DELETED %d row(s): %d min / ₹%s removed across %d org(s); balance caches invalidated.",
                sum(len(v) for v in duplicates.values()), total_minutes, total_rupees, len(affected_orgs),
            )
        else:
            log.info(
                "DRY RUN complete: would delete %d row(s) — %d min / ₹%s across %d org(s).",
                sum(len(v) for v in duplicates.values()), total_minutes, total_rupees, len(affected_orgs),
            )

    await eng.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
