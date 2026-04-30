#!/usr/bin/env python3
"""Purge all application data except superadmin user accounts.

The script does two things:
1. Best-effort cleanup of external tenant provisioning artifacts.
2. Deletion of all database rows except `superadmin_users`.

By default it runs in dry-run mode. Pass `--confirm-delete` to execute.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import httpx
import redis.asyncio as redis
from azure.core.exceptions import ResourceNotFoundError
from azure.keyvault.secrets import SecretClient
from azure.mgmt.resource import ResourceManagementClient
from azure.storage.blob import BlobServiceClient
from qdrant_client.http.exceptions import UnexpectedResponse
from sqlalchemy import delete, func, select

from app.core.azure_auth import AzureAuth
from app.core.config import settings
from app.db.session import AsyncSessionLocal
from app.models.approval import SuperAdminApprovalRequest
from app.models.audit import SuperAdminAuditLog
from app.models.organization import Organization
from app.models.organization_session import OrganizationSession
from app.models.organization_user import OrganizationUser
from app.models.session import SuperAdminSession
from app.models.tenant_resources import TenantResources
from app.models.tenant_usage_event import TenantUsageEvent
from app.models.user import SuperAdminUser
from app.services.qdrant_service import QdrantService


@dataclass
class TenantSnapshot:
    organization_id: str
    organization_name: str
    admin_email: str | None
    tenant_id: str | None
    resource_group_name: str | None
    qdrant_collection_name: str | None
    redis_namespace: str | None
    storage_account_name: str | None
    storage_container_name: str | None
    blob_prefix: str | None
    key_vault_name: str | None
    secret_refs: dict[str, Any]
    provider_status: dict[str, Any]

    @property
    def twilio_subaccount_id(self) -> str | None:
        return (self.provider_status or {}).get("twilio_subaccount_id")


class CleanupError(RuntimeError):
    pass


async def _run_blocking_with_timeout(func, timeout_seconds: float, *args, **kwargs):
    return await asyncio.wait_for(asyncio.to_thread(func, *args, **kwargs), timeout=timeout_seconds)


async def load_snapshots() -> tuple[list[TenantSnapshot], dict[str, int]]:
    async with AsyncSessionLocal() as db:
        orgs = (await db.execute(select(Organization).order_by(Organization.created_at.asc()))).scalars().all()
        resource_rows = (await db.execute(select(TenantResources))).scalars().all()
        resource_map = {str(row.organization_id): row for row in resource_rows}

        counts = {
            "superadmin_users": int(await db.scalar(select(func.count()).select_from(SuperAdminUser)) or 0),
            "superadmin_sessions": int(await db.scalar(select(func.count()).select_from(SuperAdminSession)) or 0),
            "superadmin_audit_logs": int(await db.scalar(select(func.count()).select_from(SuperAdminAuditLog)) or 0),
            "superadmin_approval_requests": int(await db.scalar(select(func.count()).select_from(SuperAdminApprovalRequest)) or 0),
            "organizations": int(await db.scalar(select(func.count()).select_from(Organization)) or 0),
            "organization_users": int(await db.scalar(select(func.count()).select_from(OrganizationUser)) or 0),
            "organization_sessions": int(await db.scalar(select(func.count()).select_from(OrganizationSession)) or 0),
            "tenant_resources": int(await db.scalar(select(func.count()).select_from(TenantResources)) or 0),
            "tenant_usage_events": int(await db.scalar(select(func.count()).select_from(TenantUsageEvent)) or 0),
        }

        snapshots: list[TenantSnapshot] = []
        for org in orgs:
            tenant = resource_map.get(str(org.id))
            snapshots.append(
                TenantSnapshot(
                    organization_id=str(org.id),
                    organization_name=org.name,
                    admin_email=org.admin_email,
                    tenant_id=tenant.tenant_id if tenant else None,
                    resource_group_name=tenant.azure_resource_group_name if tenant else None,
                    qdrant_collection_name=tenant.qdrant_collection_name if tenant else None,
                    redis_namespace=tenant.redis_namespace if tenant else None,
                    storage_account_name=tenant.storage_account_name if tenant else None,
                    storage_container_name=tenant.storage_container_name if tenant else None,
                    blob_prefix=tenant.blob_prefix if tenant else None,
                    key_vault_name=tenant.key_vault_name if tenant else None,
                    secret_refs=dict(tenant.secret_refs or {}) if tenant and tenant.secret_refs else {},
                    provider_status=dict(tenant.provider_status or {}) if tenant and tenant.provider_status else {},
                )
            )

        return snapshots, counts


def print_plan(snapshots: list[TenantSnapshot], counts: dict[str, int], database_only: bool) -> None:
    print("Purge plan", flush=True)
    print(f"- superadmin users preserved: {counts['superadmin_users']}", flush=True)
    print(f"- organizations to delete: {counts['organizations']}", flush=True)
    print(f"- organization users to delete: {counts['organization_users']}", flush=True)
    print(f"- organization sessions to delete: {counts['organization_sessions']}", flush=True)
    print(f"- tenant resources to delete: {counts['tenant_resources']}", flush=True)
    print(f"- tenant usage events to delete: {counts['tenant_usage_events']}", flush=True)
    print(f"- superadmin sessions to delete: {counts['superadmin_sessions']}", flush=True)
    print(f"- superadmin audit logs to delete: {counts['superadmin_audit_logs']}", flush=True)
    print(f"- superadmin approval requests to delete: {counts['superadmin_approval_requests']}", flush=True)
    if database_only:
        print("- external cleanup: skipped (`--database-only`)", flush=True)
    else:
        print("- external cleanup: enabled", flush=True)

    if not snapshots:
        return

    print("\nOrganizations targeted", flush=True)
    for snapshot in snapshots:
        print(
            f"- {snapshot.organization_name} | org_id={snapshot.organization_id} | "
            f"admin={snapshot.admin_email or '-'} | tenant_id={snapshot.tenant_id or '-'}"
        , flush=True)
        print(
            f"  rg={snapshot.resource_group_name or '-'} | "
            f"qdrant={snapshot.qdrant_collection_name or '-'} | "
            f"redis={snapshot.redis_namespace or '-'} | "
            f"blob_prefix={snapshot.blob_prefix or '-'} | "
            f"twilio_subaccount={snapshot.twilio_subaccount_id or '-'}"
        , flush=True)


async def cleanup_twilio_subaccount(subaccount_id: str | None) -> None:
    if not subaccount_id or not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
        return

    endpoint = f"{settings.TWILIO_BASE_URL.rstrip('/')}/Accounts/{subaccount_id}.json"
    async with httpx.AsyncClient(
        auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
        timeout=30.0,
    ) as client:
        response = await client.post(endpoint, data={"Status": "closed"})
        if response.status_code == 404:
            return
        response.raise_for_status()


async def cleanup_qdrant_collection(collection_name: str | None) -> None:
    if not collection_name:
        return

    def _delete():
        client = QdrantService._client()
        try:
            exists = client.collection_exists(collection_name)
        except Exception as exc:
            raise CleanupError(f"Qdrant collection lookup failed for {collection_name}: {exc}") from exc

        if not exists:
            return

        try:
            client.delete_collection(collection_name=collection_name)
        except UnexpectedResponse as exc:
            raise CleanupError(f"Qdrant collection delete failed for {collection_name}: {exc}") from exc

    await _run_blocking_with_timeout(_delete, 30)


async def cleanup_redis_namespace(namespace: str | None) -> None:
    if not namespace:
        return

    client = redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        keys = [key async for key in client.scan_iter(match=f"{namespace}*")]
        if keys:
            await client.delete(*keys)
    finally:
        await client.aclose()


async def cleanup_blob_prefix(account_name: str | None, container_name: str | None, prefix: str | None) -> None:
    if not account_name or not container_name or not prefix:
        return

    def _delete():
        account_url = f"https://{account_name}.blob.core.windows.net"
        credential = AzureAuth.get_credential()
        blob_service_client = BlobServiceClient(account_url=account_url, credential=credential)
        container_client = blob_service_client.get_container_client(container_name)
        if not container_client.exists():
            return

        blobs = [blob.name for blob in container_client.list_blobs(name_starts_with=prefix)]
        for blob_name in blobs:
            container_client.delete_blob(blob_name, delete_snapshots="include")

    await _run_blocking_with_timeout(_delete, 60)


async def cleanup_keyvault_secrets(vault_name: str | None, secret_refs: dict[str, Any]) -> None:
    effective_vault = vault_name or settings.AZURE_SHARED_KEY_VAULT_NAME
    if not effective_vault:
        return

    secret_names = sorted(
        {
            ref.get("secret_name")
            for ref in (secret_refs or {}).values()
            if isinstance(ref, dict) and ref.get("secret_name")
        }
    )
    if not secret_names:
        return

    def _delete():
        kv_url = f"https://{effective_vault}.vault.azure.net/"
        client = SecretClient(vault_url=kv_url, credential=AzureAuth.get_credential())
        for secret_name in secret_names:
            try:
                poller = client.begin_delete_secret(secret_name)
                poller.result()
            except ResourceNotFoundError:
                continue

    await _run_blocking_with_timeout(_delete, 60)


async def cleanup_resource_group(resource_group_name: str | None) -> None:
    if not resource_group_name or not settings.AZURE_SUBSCRIPTION_ID:
        return

    def _delete():
        client = ResourceManagementClient(AzureAuth.get_credential(), settings.AZURE_SUBSCRIPTION_ID)
        try:
            poller = client.resource_groups.begin_delete(resource_group_name)
            poller.result()
        except Exception as exc:
            message = str(exc)
            if "ResourceGroupNotFound" in message or "could not be found" in message:
                return
            raise

    await _run_blocking_with_timeout(_delete, 120)


async def cleanup_external_resources(snapshots: list[TenantSnapshot]) -> list[str]:
    failures: list[str] = []
    for snapshot in snapshots:
        print(f"Cleaning external resources for {snapshot.organization_name}...", flush=True)
        steps = [
            ("twilio", cleanup_twilio_subaccount(snapshot.twilio_subaccount_id)),
            ("qdrant", cleanup_qdrant_collection(snapshot.qdrant_collection_name)),
            ("redis", cleanup_redis_namespace(snapshot.redis_namespace)),
            (
                "blob",
                cleanup_blob_prefix(
                    snapshot.storage_account_name or settings.AZURE_SHARED_STORAGE_ACCOUNT,
                    snapshot.storage_container_name or settings.AZURE_SHARED_STORAGE_CONTAINER,
                    snapshot.blob_prefix,
                ),
            ),
            (
                "keyvault",
                cleanup_keyvault_secrets(snapshot.key_vault_name, snapshot.secret_refs),
            ),
            ("resource_group", cleanup_resource_group(snapshot.resource_group_name)),
        ]

        for name, coro in steps:
            try:
                await coro
                print(f"  - {name}: ok", flush=True)
            except Exception as exc:
                failure = f"{snapshot.organization_name} [{name}] -> {exc}"
                failures.append(failure)
                print(f"  - {name}: FAILED -> {exc}", flush=True)

    return failures


async def purge_database() -> dict[str, int]:
    async with AsyncSessionLocal() as db:
        deleted_counts: dict[str, int] = {}

        statements = [
            ("organization_sessions", delete(OrganizationSession)),
            ("tenant_usage_events", delete(TenantUsageEvent)),
            ("tenant_resources", delete(TenantResources)),
            ("organization_users", delete(OrganizationUser)),
            ("organizations", delete(Organization)),
            ("superadmin_sessions", delete(SuperAdminSession)),
            ("superadmin_audit_logs", delete(SuperAdminAuditLog)),
            ("superadmin_approval_requests", delete(SuperAdminApprovalRequest)),
        ]

        for label, statement in statements:
            result = await db.execute(statement)
            deleted_counts[label] = result.rowcount or 0

        await db.commit()
        return deleted_counts


async def main() -> int:
    parser = argparse.ArgumentParser(description="Purge all non-superadmin account data and tenant provisioning.")
    parser.add_argument(
        "--confirm-delete",
        action="store_true",
        help="Execute the purge. Without this flag the script only prints the plan.",
    )
    parser.add_argument(
        "--database-only",
        action="store_true",
        help="Skip external resource cleanup and only delete database rows.",
    )
    parser.add_argument(
        "--force-db-delete",
        action="store_true",
        help="Delete database rows even if external cleanup reports failures.",
    )
    args = parser.parse_args()

    snapshots, counts = await load_snapshots()
    print_plan(snapshots, counts, database_only=args.database_only)

    if not args.confirm_delete:
        print("\nDry run only. Re-run with `--confirm-delete` to execute.", flush=True)
        return 0

    external_failures: list[str] = []
    if not args.database_only:
        external_failures = await cleanup_external_resources(snapshots)
        if external_failures and not args.force_db_delete:
            print("\nExternal cleanup failed. Database deletion aborted.", flush=True)
            for failure in external_failures:
                print(f"- {failure}", flush=True)
            print("Re-run with `--force-db-delete` if you want to wipe the database anyway.", flush=True)
            return 2

    deleted_counts = await purge_database()
    print("\nDatabase purge complete", flush=True)
    for label, count in deleted_counts.items():
        print(f"- {label}: {count}", flush=True)

    if external_failures:
        print("\nExternal cleanup had failures", flush=True)
        for failure in external_failures:
            print(f"- {failure}", flush=True)
        return 1

    print("\nAll non-superadmin account data has been purged.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
