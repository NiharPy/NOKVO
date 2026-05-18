"""Delete every Nokvo One organization and all the cloud resources owned by it.

Run order per tenant:
    1. Qdrant       — drop the per-tenant collection.
    2. Redis        — scan + DEL every key under the tenant's namespace.
    3. Azure Blob   — delete every blob under the tenant's prefix.
    4. Azure Key Vault — best-effort delete of secrets named in
                       ``secret_refs``.
    5. Azure Resource Group — fire-and-forget delete (this nukes the
                       per-tenant OpenAI / Cognitive / etc. deployments).
    6. DB           — delete ``outbound_campaign``, ``tenant_usage_event``,
                       ``tenant_resources``, ``organization``. The
                       ``organizations`` row deletion cascades via
                       ON DELETE CASCADE foreign keys to:
                         - organization_users
                         - organization_sessions (via FK to users)
                         - email_verifications
                         - member_invitations
                         - member_* tables
                         - nokvo_one_agents
                         - nokvo_one_tool_records
                         - mcp_tool_registry

USAGE
-----
Dry run (lists what WILL be deleted, makes no changes):

    source venv/bin/activate
    python -m app.scripts.delete_nokvo_one_tenants

Live run — requires you to type the exact phrase, no flags will skip it:

    python -m app.scripts.delete_nokvo_one_tenants --i-understand

SAFETY
------
- Default mode is dry-run. ``--i-understand`` is required to actually mutate.
- Even with the flag, the script prints the plan and waits for you to type
  ``DELETE ALL NOKVO ONE TENANTS`` exactly. Anything else aborts.
- Failures on individual cloud resources are logged but don't abort the
  per-tenant deletion. The DB rows are removed last so a half-cleanup
  leaves the app in a consistent state.
- Only orgs with ``product_tier='nokvo_one'`` are touched. Non-Nokvo-One
  organizations are not affected.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import delete, select

from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.member_invitation import MemberInvitation
from app.models.organization import Organization
from app.models.outbound_campaign import OutboundCampaign
from app.models.tenant_resources import TenantResources
from app.models.tenant_usage_event import TenantUsageEvent
from app.services.azure_blob_service import AzureBlobService
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.azure_resource_group_service import AzureResourceGroupService
from app.services.qdrant_service import QdrantService

try:
    import redis.asyncio as redis_async
except Exception:  # pragma: no cover
    redis_async = None  # type: ignore


CONFIRMATION_PHRASE = "DELETE ALL NOKVO ONE TENANTS"


def _log(msg: str) -> None:
    print(msg, flush=True)


# ── per-resource cleanup helpers (each logs its own outcome) ─────────────────

def _delete_qdrant_collection(collection_name: str | None) -> None:
    if not collection_name:
        _log("    qdrant: (no collection_name)")
        return
    try:
        client = QdrantService._client()
        client.delete_collection(collection_name=collection_name)
        _log(f"    qdrant: deleted collection {collection_name}")
    except Exception as exc:
        _log(f"    qdrant: FAILED to delete {collection_name}: {exc!r}")


async def _delete_redis_namespace(namespace: str | None) -> None:
    if not namespace:
        _log("    redis: (no namespace)")
        return
    if redis_async is None:
        _log("    redis: redis library missing, skipping")
        return
    try:
        client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)
        pattern = f"{namespace}*"
        deleted = 0
        cursor = 0
        while True:
            cursor, keys = await client.scan(cursor=cursor, match=pattern, count=500)
            if keys:
                await client.delete(*keys)
                deleted += len(keys)
            if cursor == 0:
                break
        await client.aclose()
        _log(f"    redis: deleted {deleted} keys under {namespace}*")
    except Exception as exc:
        _log(f"    redis: FAILED for {namespace}: {exc!r}")


def _delete_blob_prefix(tenant_id: str, blob_prefix: str | None) -> None:
    """Walk every blob under ``blob_prefix`` and delete it. The shared
    storage account stays; we only own the per-tenant prefix."""
    if not blob_prefix:
        _log("    blob: (no prefix)")
        return
    account_name = settings.AZURE_SHARED_STORAGE_ACCOUNT
    container_name = settings.AZURE_SHARED_STORAGE_CONTAINER
    if not account_name:
        _log("    blob: no AZURE_SHARED_STORAGE_ACCOUNT configured, skipping")
        return
    try:
        from azure.storage.blob import BlobServiceClient
        from app.core.azure_auth import AzureAuth

        account_url = f"https://{account_name}.blob.core.windows.net"
        credential = AzureAuth.get_credential()
        service_client = BlobServiceClient(account_url=account_url, credential=credential)
        container_client = service_client.get_container_client(container_name)
        deleted = 0
        for blob in container_client.list_blobs(name_starts_with=blob_prefix):
            try:
                container_client.delete_blob(blob.name)
                deleted += 1
            except Exception as exc:
                _log(f"      blob: skip {blob.name}: {exc!r}")
        _log(f"    blob: deleted {deleted} blobs under {blob_prefix}")
    except Exception as exc:
        _log(f"    blob: FAILED for {blob_prefix}: {exc!r}")


async def _delete_keyvault_secrets(secret_refs: dict[str, Any] | None) -> None:
    if not secret_refs:
        _log("    keyvault: (no secret_refs)")
        return
    deleted = 0
    failed = 0
    for key, ref in (secret_refs or {}).items():
        # secret_refs format: {"key_name": {"secret_name": "...", "secret_uri": "..."}}
        secret_name = None
        if isinstance(ref, dict):
            secret_name = ref.get("secret_name") or ref.get("name")
        elif isinstance(ref, str):
            secret_name = ref
        if not secret_name:
            continue
        try:
            ok = await AzureKeyVaultService.delete_secret(secret_name)
            if ok:
                deleted += 1
            else:
                failed += 1
        except Exception as exc:
            _log(f"      keyvault: skip {secret_name}: {exc!r}")
            failed += 1
    _log(f"    keyvault: deleted {deleted}, failed {failed}")


async def _delete_resource_group(rg_name: str | None) -> None:
    if not rg_name:
        _log("    azure_rg: (no resource group)")
        return
    try:
        ok = await AzureResourceGroupService.delete_resource_group(rg_name)
        if ok:
            _log(f"    azure_rg: delete queued for {rg_name} (Azure runs async)")
        else:
            _log(f"    azure_rg: delete returned False for {rg_name}")
    except Exception as exc:
        _log(f"    azure_rg: FAILED for {rg_name}: {exc!r}")


# ── per-tenant orchestrator ──────────────────────────────────────────────────


async def _cleanup_tenant_cloud(tenant: TenantResources) -> None:
    _log(f"  cleaning cloud resources for tenant {tenant.tenant_id}")
    _delete_qdrant_collection(tenant.qdrant_collection_name)
    await _delete_redis_namespace(tenant.redis_namespace)
    _delete_blob_prefix(tenant.tenant_id, tenant.blob_prefix)
    await _delete_keyvault_secrets(tenant.secret_refs)
    await _delete_resource_group(tenant.azure_resource_group_name)


async def _delete_org_db_rows(db, org: Organization) -> None:
    """Delete in dependency order with raw SQL DELETEs so we don't depend
    on SQLAlchemy session-flush ordering (which doesn't always respect FK
    direction when there's no ``cascade="all,delete"`` on the relationship).
    """
    org_id = org.id
    # 1) Find tenant_ids referenced by outbound_campaign (FK on tenant_id, no cascade)
    res = await db.execute(
        select(TenantResources.tenant_id).where(TenantResources.organization_id == org_id)
    )
    tenant_ids = [row[0] for row in res.all()]

    if tenant_ids:
        await db.execute(
            delete(OutboundCampaign).where(OutboundCampaign.tenant_id.in_(tenant_ids))
        )
    # 2) Usage events (no cascade)
    await db.execute(
        delete(TenantUsageEvent).where(TenantUsageEvent.organization_id == org_id)
    )
    # 3) tenant_resources rows (must happen BEFORE org delete since the FK
    #    on this table doesn't cascade)
    await db.execute(
        delete(TenantResources).where(TenantResources.organization_id == org_id)
    )
    # 4) The organization delete now cascades cleanly to users / sessions /
    #    agents / invitations / member_* / mcp_tool_registry / etc.
    await db.execute(delete(Organization).where(Organization.id == org_id))
    await db.commit()


# ── main entry ───────────────────────────────────────────────────────────────


async def list_targets(db) -> list[Organization]:
    res = await db.execute(
        select(Organization).where(Organization.product_tier == "nokvo_one")
    )
    return list(res.scalars().all())


async def run(live: bool) -> None:
    async with AsyncSessionLocal() as db:
        targets = await list_targets(db)

    if not targets:
        _log("No Nokvo One organizations found. Nothing to do.")
        return

    _log("=" * 70)
    _log(f"FOUND {len(targets)} NOKVO ONE ORGANIZATION(S):")
    _log("=" * 70)
    for org in targets:
        _log(f"  - {org.id}  {org.name!r:30s}  tier={org.product_tier}  status={org.status}")

    if not live:
        _log("")
        _log("DRY RUN. Pass --i-understand to actually delete.")
        return

    # Final manual confirmation — typed phrase. No flag can skip this.
    _log("")
    _log("This will DELETE EVERY ORGANIZATION ABOVE plus their Azure resource")
    _log("groups, Qdrant collections, Redis keys, and Blob folders.")
    _log("This action is IRREVERSIBLE at the cloud level.")
    _log("")
    _log(f"Type exactly:  {CONFIRMATION_PHRASE}")
    typed = input("> ").strip()
    if typed != CONFIRMATION_PHRASE:
        _log("Aborted (phrase did not match).")
        return

    _log("")
    _log("Proceeding with deletion...")
    _log("=" * 70)

    succeeded: list[str] = []
    failed: list[tuple[str, str]] = []
    for org in targets:
        _log(f"\norganization {org.id} ({org.name!r}):")
        try:
            async with AsyncSessionLocal() as db:
                res = await db.execute(
                    select(TenantResources).where(TenantResources.organization_id == org.id)
                )
                tenants = list(res.scalars().all())
                # Cloud cleanup runs without a DB session — Azure / Qdrant /
                # Redis / Blob clients have their own connections.
                for t in tenants:
                    await _cleanup_tenant_cloud(t)
                # Re-fetch org inside this session for the cascade delete.
                res = await db.execute(select(Organization).where(Organization.id == org.id))
                org_fresh = res.scalars().first()
                if org_fresh is None:
                    _log("  (org already gone, skipping db delete)")
                    succeeded.append(str(org.id))
                    continue
                _log("  deleting db rows (cascades to users/sessions/agents)...")
                await _delete_org_db_rows(db, org_fresh)
            _log("  done")
            succeeded.append(str(org.id))
        except Exception as exc:
            _log(f"  FAILED: {exc!r}")
            failed.append((str(org.id), str(exc)))

    _log("")
    _log("=" * 70)
    _log(f"SUMMARY: {len(succeeded)} deleted, {len(failed)} failed")
    _log("=" * 70)
    if failed:
        for org_id, err in failed:
            _log(f"  FAIL  {org_id}  {err[:120]}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--i-understand",
        action="store_true",
        help="Required to switch from dry-run to live deletion.",
    )
    args = parser.parse_args()
    asyncio.run(run(live=args.i_understand))


if __name__ == "__main__":
    main()
