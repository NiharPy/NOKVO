"""
All-or-nothing tenant provisioner for Nokvo One.

Steps (strict order — any failure raises and rolls back in reverse):

  1. Azure Resource Group (per-tenant, lightweight)
  2. Azure OpenAI realtime-mini account + deployment in that RG
  3. Shared blob prefix tenants/{tenant_id}/ in the shared storage account
  4. Qdrant collection in the shared cluster
  5. Redis namespace in shared Redis
  6. Exotel placeholder record (credentials added later by superadmin/admin)

On success, returns a dict ready to seed a TenantResources row. The caller (the
signup endpoint) persists Organization + OrganizationUser + TenantResources in a
single transaction; that way the SQL commit is the final, atomic point of no
return. If the provisioner raises, no DB rows are committed.
"""
from __future__ import annotations

import inspect
import logging
import re
import uuid
from typing import Any, Callable

import redis.asyncio as redis_async

from app.core.config import settings
from app.services.azure_blob_service import AzureBlobService
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.azure_realtime_ai_service import AzureRealtimeAIService
from app.services.azure_resource_group_service import AzureResourceGroupService
from app.services.qdrant_service import QdrantService
from app.services.redis_tenant_service import RedisTenantService


logger = logging.getLogger(__name__)


class NokvoOneProvisioningError(RuntimeError):
    """Raised when any provisioning step fails. .step names which one."""

    def __init__(self, step: str, message: str, original: Exception | None = None):
        super().__init__(f"{step}: {message}")
        self.step = step
        self.message = message
        self.original = original


def _slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9\s-]", "", (name or "tenant").lower())
    slug = re.sub(r"[\s-]+", "-", slug).strip("-")
    return slug[:24] or "tenant"


async def _delete_qdrant_collection(name: str) -> None:
    try:
        client = QdrantService._client()
        if client.collection_exists(name):
            client.delete_collection(name)
    except Exception:
        logger.exception("Failed to delete Qdrant collection %s during rollback", name)


async def _delete_redis_namespace(namespace: str) -> None:
    try:
        client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)
        try:
            async for key in client.scan_iter(match=f"{namespace}:*"):
                await client.delete(key)
        finally:
            await client.aclose()
    except Exception:
        logger.exception("Failed to clean Redis namespace %s during rollback", namespace)


async def _delete_blob_prefix(prefix: str) -> None:
    account_name = settings.AZURE_SHARED_STORAGE_ACCOUNT
    container = settings.AZURE_SHARED_STORAGE_CONTAINER
    if not account_name:
        return
    try:
        from azure.storage.blob import BlobServiceClient

        from app.core.azure_auth import AzureAuth

        account_url = f"https://{account_name}.blob.core.windows.net"
        credential = AzureAuth.get_credential()
        blob_service_client = BlobServiceClient(account_url=account_url, credential=credential)
        container_client = blob_service_client.get_container_client(container)
        for blob in container_client.list_blobs(name_starts_with=prefix):
            container_client.delete_blob(blob.name)
    except Exception:
        logger.exception("Failed to clean blob prefix %s during rollback", prefix)


StepCallback = Callable[[str, str, str | None], Any]


async def _emit(on_step: StepCallback | None, name: str, status: str, message: str | None = None) -> None:
    """Invoke the on_step callback safely. Supports both sync and async callbacks."""
    if on_step is None:
        return
    try:
        result = on_step(name, status, message)
        if inspect.isawaitable(result):
            await result
    except Exception:
        logger.exception("on_step callback raised for %s/%s", name, status)


class NokvoOneProvisioningService:
    @staticmethod
    async def provision_or_raise(
        organization_id: uuid.UUID,
        organization_name: str,
        environment: str = "staging",
        region: str = "centralindia",
        on_step: StepCallback | None = None,
    ) -> dict[str, Any]:
        tenant_id = str(uuid.uuid4())
        slug = _slug(organization_name)
        rg_name = f"rg-nokvo1-{slug}-{environment}"[:90]

        rollback_actions: list[tuple[str, Any]] = []

        async def rollback():
            for kind, payload in reversed(rollback_actions):
                if kind == "rg":
                    await AzureResourceGroupService.delete_resource_group(payload)
                elif kind == "ai":
                    rg, account = payload
                    await AzureRealtimeAIService.delete_realtime_account(rg, account)
                elif kind == "key_vault_secrets":
                    for secret_name in payload:
                        await AzureKeyVaultService.delete_secret(secret_name)
                elif kind == "blob":
                    await _delete_blob_prefix(payload)
                elif kind == "qdrant":
                    await _delete_qdrant_collection(payload)
                elif kind == "redis":
                    await _delete_redis_namespace(payload)

        # ── Step 1: Resource Group ───────────────────────────────────────────
        await _emit(on_step, "resource_group", "running")
        try:
            rg_result = await AzureResourceGroupService.provision_resource_group(
                rg_name=rg_name,
                tenant_id=tenant_id,
                org_id=str(organization_id),
                environment=environment,
                region=region,
            )
            rollback_actions.append(("rg", rg_name))
            await _emit(on_step, "resource_group", "success")
        except Exception as exc:
            await _emit(on_step, "resource_group", "failed", str(exc))
            raise NokvoOneProvisioningError("resource_group", str(exc), exc) from exc

        # ── Step 2: Azure OpenAI realtime-mini ───────────────────────────────
        await _emit(on_step, "azure_openai_realtime_mini", "running")
        try:
            ai_result = await AzureRealtimeAIService.provision_realtime_account(
                rg_name=rg_name,
                tenant_id=tenant_id,
                slug=slug,
                organization_name=organization_name,
            )
            if ai_result.get("status") not in {"provisioned", "skipped_no_azure_subscription"}:
                raise RuntimeError(
                    f"Unexpected AI provisioning status: {ai_result.get('status')!r}"
                )
            if ai_result.get("status") == "provisioned":
                rollback_actions.append(("ai", (rg_name, ai_result["account_name"])))
            await _emit(on_step, "azure_openai_realtime_mini", ai_result.get("status", "success"))
        except Exception as exc:
            await _emit(on_step, "azure_openai_realtime_mini", "failed", str(exc))
            await rollback()
            if isinstance(exc, NokvoOneProvisioningError):
                raise
            raise NokvoOneProvisioningError("azure_openai_realtime_mini", str(exc), exc) from exc

        # ── Step 3: Shared Key Vault placeholders + live LLM key ─────────────
        kv_status = "skipped_no_shared_vault"
        kv_secret_refs: dict = {}
        llm_api_key_secret_name: str | None = None
        if settings.AZURE_SHARED_KEY_VAULT_NAME:
            await _emit(on_step, "shared_key_vault", "running")
            try:
                kv_secret_refs = await AzureKeyVaultService.provision_secret_refs(tenant_id)
                created_secret_names = [
                    ref["secret_name"]
                    for key, ref in kv_secret_refs.items()
                    if key != "_compliance" and isinstance(ref, dict) and ref.get("secret_name")
                ]
                rollback_actions.append(("key_vault_secrets", created_secret_names))
                raw_key = ai_result.get("_api_key_raw")
                llm_ref = (kv_secret_refs.get("llm_api_key") or {}).get("secret_name")
                if raw_key and llm_ref:
                    await AzureKeyVaultService.set_secret_value(
                        secret_name=llm_ref,
                        secret_value=raw_key,
                        tenant_id=tenant_id,
                        secret_role="llm_api_key",
                    )
                    llm_api_key_secret_name = llm_ref
                kv_status = "provisioned"
                await _emit(on_step, "shared_key_vault", "success")
            except Exception as exc:
                await _emit(on_step, "shared_key_vault", "failed", str(exc))
                await rollback()
                raise NokvoOneProvisioningError("shared_key_vault", str(exc), exc) from exc
        else:
            await _emit(on_step, "shared_key_vault", kv_status)
        ai_result.pop("_api_key_raw", None)

        # ── Step 4: Shared blob prefix ───────────────────────────────────────
        await _emit(on_step, "blob_prefix", "running")
        try:
            blob_result = await AzureBlobService.provision_blob_storage(tenant_id)
            rollback_actions.append(("blob", blob_result.get("blob_prefix")))
            await _emit(on_step, "blob_prefix", "success")
        except Exception as exc:
            await _emit(on_step, "blob_prefix", "failed", str(exc))
            await rollback()
            raise NokvoOneProvisioningError("blob_prefix", str(exc), exc) from exc

        # ── Step 5: Qdrant collection ────────────────────────────────────────
        await _emit(on_step, "qdrant_collection", "running")
        try:
            qdrant_collection = await QdrantService.provision_collection(tenant_id)
            rollback_actions.append(("qdrant", qdrant_collection))
            await _emit(on_step, "qdrant_collection", "success")
        except Exception as exc:
            await _emit(on_step, "qdrant_collection", "failed", str(exc))
            await rollback()
            raise NokvoOneProvisioningError("qdrant_collection", str(exc), exc) from exc

        # ── Step 6: Redis namespace ──────────────────────────────────────────
        await _emit(on_step, "redis_namespace", "running")
        try:
            redis_namespace = await RedisTenantService.provision_redis(tenant_id)
            rollback_actions.append(("redis", redis_namespace))
            await _emit(on_step, "redis_namespace", "success")
        except Exception as exc:
            await _emit(on_step, "redis_namespace", "failed", str(exc))
            await rollback()
            raise NokvoOneProvisioningError("redis_namespace", str(exc), exc) from exc

        # ── Step 7: Exotel placeholder ───────────────────────────────────────
        await _emit(on_step, "exotel_placeholder", "pending_credentials")
        # Exotel has no programmatic subaccount API — we reserve a slot and the org
        # admin (or superadmin during approval) fills in SID/token/from-number later.
        exotel_record = {
            "provider": "exotel",
            "status": "pending_credentials",
            "sid": None,
            "token_encrypted": None,
            "from_number": None,
            "stream_url": None,
        }

        # ── Assemble TenantResources seed ────────────────────────────────────
        provider_status = {
            "product_tier": "nokvo_one",
            "llm_provider": "azure_openai",
            "llm_model": ai_result.get("model"),
            "llm_model_version": ai_result.get("model_version"),
            "llm_deployment": ai_result.get("deployment_name"),
            "llm_account": ai_result.get("account_name"),
            "llm_endpoint": ai_result.get("endpoint"),
            "llm_region": ai_result.get("region"),
            "llm_api_key_encrypted": ai_result.get("api_key_encrypted"),
            "llm_api_key_secret_ref": llm_api_key_secret_name,
            "llm_status": ai_result.get("status"),
            "key_vault_name": settings.AZURE_SHARED_KEY_VAULT_NAME or None,
            "key_vault_status": kv_status,
            "key_vault_secret_refs": {
                key: ref.get("secret_name")
                for key, ref in (kv_secret_refs or {}).items()
                if key != "_compliance" and isinstance(ref, dict) and ref.get("secret_name")
            },
            "key_vault_compliance": (kv_secret_refs or {}).get("_compliance"),
            "qdrant_status": "provisioned",
            "qdrant_url_ref": QdrantService.cluster_ref(),
            "redis_status": "provisioned",
            "redis_mode": "shared",
            "exotel": exotel_record,
        }

        return {
            "tenant_id": tenant_id,
            "azure_resource_group_name": rg_result or rg_name,
            "azure_region": region,
            "qdrant_collection_name": qdrant_collection,
            "qdrant_url_ref": QdrantService.cluster_ref(),
            "redis_namespace": redis_namespace,
            "storage_account_name": blob_result.get("storage_account_name"),
            "storage_container_name": blob_result.get("container_name"),
            "blob_prefix": blob_result.get("blob_prefix"),
            "provider_status": provider_status,
            "provisioning_status": "success",
            "provisioning_steps": [
                {"name": "resource_group", "status": "success"},
                {"name": "azure_openai_realtime_mini", "status": ai_result.get("status", "provisioned")},
                {"name": "shared_key_vault", "status": kv_status},
                {"name": "blob_prefix", "status": "success"},
                {"name": "qdrant_collection", "status": "success"},
                {"name": "redis_namespace", "status": "success"},
                {"name": "exotel_placeholder", "status": "pending_credentials"},
            ],
        }
