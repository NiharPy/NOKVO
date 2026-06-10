"""One-time: wire an EXISTING tenant's telephony to the Plivo MASTER account
(NO subaccount), reusing a DID already owned by the master account.

New tenants get their own subaccount automatically at provisioning
(`nokvo_one_provisioning_service`). This script is only for the pre-existing
tenant(s) that should run directly on the main account.

It creates a Plivo Application (answer_url → our inbound webhook), assigns the
given DID to that Application, and writes ``provider_status.plivo`` with
``subaccount_auth_id = None`` (so outbound uses the master creds).

Dry-run by default. ``--execute`` performs the Plivo + DB mutations.

USAGE
    venv/bin/python -m app.scripts.setup_plivo_tenant --list
    venv/bin/python -m app.scripts.setup_plivo_tenant --tenant-id <id> --number 918031321315
    venv/bin/python -m app.scripts.setup_plivo_tenant --tenant-id <id> --number 918031321315 --execute
"""
from __future__ import annotations

import argparse
import asyncio
import uuid

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.tenant_resources import TenantResources
from app.services.plivo_service import PlivoService


async def _list() -> None:
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(select(TenantResources))).scalars().all()
        if not rows:
            print("No tenants found.")
            return
        for r in rows:
            ps = r.provider_status or {}
            plivo = ps.get("plivo") or {}
            link = ps.get("agent_phone_link") or {}
            print(
                f"tenant_id={r.tenant_id}  org={r.organization_id}  tier={ps.get('product_tier')}  "
                f"plivo_number={plivo.get('number')}  number_status={plivo.get('number_status')}  "
                f"subaccount={plivo.get('subaccount_auth_id')}  link_id={link.get('link_id')}"
            )


async def _setup(tenant_id: str | None, number: str, execute: bool) -> None:
    if not (settings.PLIVO_AUTH_ID and settings.PLIVO_AUTH_TOKEN and settings.PLIVO_WEBHOOK_BASE_URL):
        print("ERROR: PLIVO_AUTH_ID / PLIVO_AUTH_TOKEN / PLIVO_WEBHOOK_BASE_URL must be set.")
        return
    async with AsyncSessionLocal() as db:
        q = select(TenantResources)
        if tenant_id:
            q = q.where(TenantResources.tenant_id == tenant_id)
        rows = (await db.execute(q)).scalars().all()
        if not rows:
            print("No matching tenant.")
            return
        if len(rows) > 1:
            print(f"{len(rows)} tenants matched — pass --tenant-id to pick one (use --list).")
            return
        tr = rows[0]
        ps = dict(tr.provider_status or {})
        link = dict(ps.get("agent_phone_link") or {})
        link_id = link.get("link_id") or str(uuid.uuid4())
        base = settings.PLIVO_WEBHOOK_BASE_URL.rstrip("/")
        answer_url = f"{base}/api/nokvo-one/agents/plivo/voice/{link_id}"

        print(f"tenant_id   : {tr.tenant_id}")
        print(f"link_id     : {link_id}")
        print(f"answer_url  : {answer_url}")
        print(f"DID         : {number}  (MASTER account — no subaccount)")
        if not execute:
            print("\nDRY RUN. Re-run with --execute to create the Application, assign the DID, and persist.")
            return

        app_id = await PlivoService.create_application(app_name=f"nokvo-{str(tr.tenant_id)[:8]}", answer_url=answer_url)
        await PlivoService.assign_number(number, app_id=app_id)  # master, no subaccount move
        ps["plivo"] = {
            "provider": "plivo",
            "status": "linked",
            "link_id": link_id,
            "subaccount_auth_id": None,
            "subaccount_auth_token_enc": None,
            "application_id": app_id,
            "answer_url": answer_url,
            "number": number,
            "number_status": "active",
            "forward_from_number": (ps.get("plivo") or {}).get("forward_from_number"),
        }
        ps["agent_phone_link"] = {"link_id": link_id, "provider": "plivo", "status": "linked"}
        tr.provider_status = ps
        tr.twilio_phone_number = number
        flag_modified(tr, "provider_status")
        db.add(tr)
        await db.commit()
        print(f"\nDONE — Application {app_id} created, DID {number} assigned, tenant wired to the MASTER account.")
        print(f"Forward your business line to {number}. Inbound calls now hit {answer_url}.")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--list", action="store_true", help="List tenants + their Plivo status.")
    p.add_argument("--tenant-id", help="Target tenant id (required when >1 tenant).")
    p.add_argument("--number", default="918031321315", help="Owned master DID to assign (default: 918031321315).")
    p.add_argument("--execute", action="store_true", help="Apply (default: dry-run).")
    args = p.parse_args()
    if args.list:
        asyncio.run(_list())
    else:
        asyncio.run(_setup(args.tenant_id, args.number, args.execute))


if __name__ == "__main__":
    main()
