"""Read-only: list the most recent campaigns and their per-contact dial state.

Diagnoses "keeps calling me / not concurrent": shows duplicate phones, how many
contacts are still pending/calling/ended, and each contact's call_id so we can
see whether a number was placed more than once.
"""
import asyncio
import sys
from collections import Counter

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.outbound_campaign import OutboundCampaign

LIMIT = int(sys.argv[1]) if len(sys.argv) > 1 else 6


async def main() -> None:
    async with AsyncSessionLocal() as db:
        camps = (
            await db.execute(
                select(OutboundCampaign).order_by(OutboundCampaign.created_at.desc()).limit(LIMIT)
            )
        ).scalars().all()
        for c in camps:
            contacts = c.contacts or []
            status_val = c.status.value if hasattr(c.status, "value") else c.status
            by_status = Counter(str(ct.get("status")) for ct in contacts)
            by_phone = Counter(str(ct.get("phone")) for ct in contacts)
            dups = {p: n for p, n in by_phone.items() if n > 1}
            print("=" * 70)
            print(f"campaign={c.id} name={c.name!r}")
            print(f"  status={status_val} bulk={(c.agent_config or {}).get('bulk_csv')} "
                  f"from={c.from_number}")
            print(f"  created={c.created_at} started={c.started_at} completed={c.completed_at}")
            print(f"  total={c.total_count} answered={c.answered_count} failed={c.failed_count} "
                  f"#contacts={len(contacts)}")
            print(f"  contact status histogram: {dict(by_status)}")
            if dups:
                print(f"  ⚠ DUPLICATE phones (same number as multiple contacts): {dups}")
            for ct in contacts:
                print(
                    f"    phone={ct.get('phone')!r} status={ct.get('status')} "
                    f"ended={ct.get('ended')} call_id={ct.get('call_id')} "
                    f"answered_at={ct.get('answered_at')} link={ct.get('call_link_id')} "
                    f"err={ct.get('error')}"
                )


asyncio.run(main())
