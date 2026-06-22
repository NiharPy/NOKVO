"""Read-only: dump a campaign's contacts + the tenant's follow-up rows.

Diagnoses the "it keeps calling me" report: shows how many contacts carry the
same phone, their dial state, and whether the follow-up scheduler has rows that
would re-dial.
"""
import asyncio
import sys
from collections import Counter

from sqlalchemy import select

from app.db.session import AsyncSessionLocal
from app.models.outbound_campaign import OutboundCampaign

CAMPAIGN_ID = sys.argv[1] if len(sys.argv) > 1 else "832d6938-dab7-4a1a-8aa3-6faa6bfb8a0d"


async def main() -> None:
    async with AsyncSessionLocal() as db:
        c = (
            await db.execute(select(OutboundCampaign).where(OutboundCampaign.id == CAMPAIGN_ID))
        ).scalar_one_or_none()
        if c is None:
            print(f"campaign {CAMPAIGN_ID} not found")
            return
        contacts = c.contacts or []
        status_val = c.status.value if hasattr(c.status, "value") else c.status
        print(f"campaign={c.id}")
        print(f"  name={c.name!r} status={status_val} bulk={(c.agent_config or {}).get('bulk_csv')}")
        print(f"  tenant={c.tenant_id} from_number={c.from_number}")
        print(f"  total={c.total_count} answered={c.answered_count} failed={c.failed_count}")
        print(f"  started_at={c.started_at} completed_at={c.completed_at}")
        print(f"  followup_rules={(c.agent_config or {}).get('followup_rules')}")
        print(f"  #contacts={len(contacts)}")
        by_phone = Counter(str(ct.get("phone")) for ct in contacts)
        print("  phone occurrences:")
        for phone, n in by_phone.most_common():
            print(f"    {phone}: {n}")
        print("  contacts:")
        for ct in contacts:
            print(
                f"    phone={ct.get('phone')} status={ct.get('status')} "
                f"ended={ct.get('ended')} answered_at={ct.get('answered_at')} "
                f"call_id={ct.get('call_id')} link={ct.get('call_link_id')} "
                f"err={ct.get('error')}"
            )

        # Follow-up rows for the tenant (would they re-dial?)
        try:
            from app.models.lead_followup_schedule import LeadFollowupSchedule

            rows = (
                await db.execute(
                    select(LeadFollowupSchedule).where(
                        LeadFollowupSchedule.tenant_id == c.tenant_id
                    )
                )
            ).scalars().all()
            print(f"\n  follow-up rows for tenant: {len(rows)}")
            sc = Counter(str(r.status.value if hasattr(r.status, "value") else r.status) for r in rows)
            print(f"    by status: {dict(sc)}")
            for r in rows[:20]:
                st = r.status.value if hasattr(r.status, "value") else r.status
                print(
                    f"    status={st} scheduled_at={r.scheduled_at} attempts={r.attempts} "
                    f"reason={getattr(r.reason,'value',r.reason)} placed_call_id={r.placed_call_id} "
                    f"lead_id={r.lead_id} customer_id={r.customer_id}"
                )
        except Exception as exc:
            print(f"  follow-up query failed: {exc}")


asyncio.run(main())
