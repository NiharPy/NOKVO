"""One-off teardown for ALL Nokvo One tenants.

Steps per tenant_resources row:
  1. Delete Azure Resource Group (cascades Azure OpenAI account + deployments)
  2. Delete the per-tenant Key Vault secrets recorded in provider_status
  3. Delete the per-tenant Qdrant collection
  4. Wipe per-tenant Redis namespace keys
  5. Wipe the per-tenant blob prefix

Then Postgres cleanup:
  - DELETE tenant_usage_events (FK NO ACTION on organizations)
  - DELETE tenant_resources    (FK NO ACTION on organizations)
  - DELETE organizations       (cascades organization_users, sessions, agents,
                                invitations, tool records, audit logs, etc.)

Run from repo root:
    source venv/bin/activate
    python3 scripts/delete_nokvo_one_tenants.py
"""
from __future__ import annotations

import asyncio
import logging
import sys

import redis.asyncio as redis_async
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.azure_resource_group_service import AzureResourceGroupService
from app.services.qdrant_service import QdrantService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("teardown")


async def delete_redis_namespace(namespace: str) -> None:
    client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        deleted = 0
        async for key in client.scan_iter(match=f"{namespace}:*"):
            await client.delete(key)
            deleted += 1
        log.info("redis: deleted %s keys under %s:*", deleted, namespace)
    finally:
        await client.aclose()


async def delete_blob_prefix(prefix: str) -> None:
    account_name = settings.AZURE_SHARED_STORAGE_ACCOUNT
    container = settings.AZURE_SHARED_STORAGE_CONTAINER
    if not account_name or not prefix:
        log.info("blob: skipping (no shared storage account configured)")
        return
    from azure.storage.blob import BlobServiceClient

    from app.core.azure_auth import AzureAuth

    account_url = f"https://{account_name}.blob.core.windows.net"
    credential = AzureAuth.get_credential()
    blob_service_client = BlobServiceClient(account_url=account_url, credential=credential)
    container_client = blob_service_client.get_container_client(container)
    deleted = 0
    for blob in container_client.list_blobs(name_starts_with=prefix):
        container_client.delete_blob(blob.name)
        deleted += 1
    log.info("blob: deleted %s blobs under %s", deleted, prefix)


async def delete_qdrant_collection(name: str) -> None:
    if not name:
        return
    client = QdrantService._client()
    if client.collection_exists(name):
        client.delete_collection(name)
        log.info("qdrant: dropped collection %s", name)
    else:
        log.info("qdrant: collection %s not found", name)


async def main() -> int:
    eng = create_async_engine(settings.SQLALCHEMY_DATABASE_URI)
    try:
        async with eng.connect() as conn:
            rows = await conn.execute(
                text(
                    """
                    SELECT tr.id, tr.tenant_id, tr.organization_id, o.name,
                           tr.azure_resource_group_name, tr.qdrant_collection_name,
                           tr.redis_namespace, tr.blob_prefix, tr.provider_status
                    FROM tenant_resources tr
                    JOIN organizations o ON o.id = tr.organization_id
                    WHERE o.product_tier = 'nokvo_one'
                    """
                )
            )
            tenants = rows.mappings().all()

        if not tenants:
            log.info("nothing to do — no nokvo_one tenants found")
            return 0

        log.info("tearing down %s nokvo_one tenant(s)", len(tenants))
        org_ids: list[str] = []
        # ``tenant_str_ids`` collects the per-tenant string keys used by
        # the lead / campaign chain (those tables FK on the string
        # ``tenant_resources.tenant_id`` column, not the org UUID).
        tenant_str_ids: list[str] = []

        for t in tenants:
            log.info("=== tenant %s (org %r) ===", t["tenant_id"], t["name"])
            org_ids.append(str(t["organization_id"]))
            if t["tenant_id"]:
                tenant_str_ids.append(str(t["tenant_id"]))

            rg = t["azure_resource_group_name"]
            if rg:
                ok = await AzureResourceGroupService.delete_resource_group(rg)
                log.info("rg: %s -> deleted=%s", rg, ok)
            else:
                log.info("rg: skipping (none recorded)")

            ps = t["provider_status"] or {}
            kv_refs = (ps.get("key_vault_secret_refs") or {}) if isinstance(ps, dict) else {}
            secret_names = [v for v in kv_refs.values() if v]
            for secret_name in secret_names:
                try:
                    await AzureKeyVaultService.delete_secret(secret_name)
                    log.info("kv: deleted secret %s", secret_name)
                except Exception as exc:
                    log.exception("kv: failed to delete %s: %s", secret_name, exc)

            try:
                await delete_qdrant_collection(t["qdrant_collection_name"])
            except Exception:
                log.exception("qdrant teardown failed")

            try:
                if t["redis_namespace"]:
                    await delete_redis_namespace(t["redis_namespace"])
            except Exception:
                log.exception("redis teardown failed")

            try:
                if t["blob_prefix"]:
                    await delete_blob_prefix(t["blob_prefix"])
            except Exception:
                log.exception("blob teardown failed")

        async with eng.begin() as conn:
            placeholders = ",".join(f":id{i}" for i in range(len(org_ids)))
            params = {f"id{i}": v for i, v in enumerate(org_ids)}
            tenant_id_placeholders = ",".join(f":tid{i}" for i in range(len(tenant_str_ids)))
            tenant_params = {f"tid{i}": v for i, v in enumerate(tenant_str_ids)}

            # Outbound campaign chain (added after the original script
            # shipped) — these FK to tenant_resources / organizations
            # with ON DELETE NO ACTION, so we have to remove them
            # before the tenant_resources / organizations cascade.
            # Order matters: contacts → leads → forms → connections,
            # then the campaigns themselves.
            if tenant_str_ids:
                pre_chain = [
                    (
                        "DELETE FROM outbound_campaign_contacts "
                        "WHERE campaign_id IN (SELECT id FROM outbound_campaigns "
                        f"WHERE tenant_id IN ({tenant_id_placeholders}))"
                    ),
                    f"DELETE FROM outgoing_leads WHERE tenant_id IN ({tenant_id_placeholders})",
                    f"DELETE FROM lead_capture_forms WHERE tenant_id IN ({tenant_id_placeholders})",
                    f"DELETE FROM lead_source_connections WHERE tenant_id IN ({tenant_id_placeholders})",
                    f"DELETE FROM outbound_campaigns WHERE tenant_id IN ({tenant_id_placeholders})",
                ]
                for stmt in pre_chain:
                    res = await conn.execute(text(stmt), tenant_params)
                    log.info(
                        "db: deleted %s rows via `%s ...`",
                        res.rowcount,
                        stmt.split(" WHERE", 1)[0],
                    )

            # Organization-level dependents (ON DELETE NO ACTION).
            org_chain_extra = [
                "DELETE FROM connect_sessions",
                "DELETE FROM organization_api_keys",
            ]
            for stmt in org_chain_extra:
                res = await conn.execute(
                    text(f"{stmt} WHERE organization_id IN ({placeholders})"),
                    params,
                )
                log.info("db: deleted %s rows via `%s ...`", res.rowcount, stmt)

            res = await conn.execute(
                text(
                    f"DELETE FROM tenant_usage_events WHERE organization_id IN ({placeholders})"
                ),
                params,
            )
            log.info("db: deleted %s tenant_usage_events", res.rowcount)
            res = await conn.execute(
                text(f"DELETE FROM tenant_resources WHERE organization_id IN ({placeholders})"),
                params,
            )
            log.info("db: deleted %s tenant_resources", res.rowcount)
            res = await conn.execute(
                text(f"DELETE FROM organizations WHERE id IN ({placeholders})"),
                params,
            )
            log.info("db: deleted %s organizations (cascades users/sessions/agents/...)", res.rowcount)

        log.info("done.")
        return 0
    finally:
        await eng.dispose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
