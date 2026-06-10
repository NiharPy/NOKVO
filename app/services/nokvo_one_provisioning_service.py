"""
All-or-nothing tenant provisioner for Nokvo One.

Steps (strict order — any failure raises and rolls back in reverse):

  1. Shared blob prefix tenants/{tenant_id}/ in the shared storage account
  2. Qdrant collection in the shared cluster
  3. Redis namespace in shared Redis
  4. Exotel placeholder record (credentials added later by superadmin/admin)

The chat LLM is served by the shared global pool (``app.services.llm_pool``),
embeddings fall back to the global OpenAI key, and STT/TTS use the global Sarvam
key — so signup no longer provisions a per-tenant Azure OpenAI account, Resource
Group, or Key Vault (the slow Azure control-plane steps). Onboarding is now fast.

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
from app.services.azure_openai_chat_service import AzureOpenAIChatService
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
        region: str = "southindia",
        on_step: StepCallback | None = None,
    ) -> dict[str, Any]:
        tenant_id = str(uuid.uuid4())
        slug = _slug(organization_name)

        rollback_actions: list[tuple[str, Any]] = []

        async def rollback():
            for kind, payload in reversed(rollback_actions):
                if kind == "blob":
                    await _delete_blob_prefix(payload)
                elif kind == "qdrant":
                    await _delete_qdrant_collection(payload)
                elif kind == "redis":
                    await _delete_redis_namespace(payload)
                elif kind in ("plivo_number", "plivo_application", "plivo_subaccount"):
                    try:
                        from app.services.plivo_service import PlivoService
                        if kind == "plivo_number":
                            await PlivoService.release_number(payload)
                        elif kind == "plivo_application":
                            await PlivoService.delete_application(payload)
                        else:
                            await PlivoService.delete_subaccount(payload)
                    except Exception:
                        logger.debug("plivo rollback failed for %s/%s", kind, payload)

        # The chat LLM is served by the shared global pool (app.services.llm_pool),
        # embeddings fall back to the global OpenAI key, and STT/TTS use the global
        # Sarvam key — so signup no longer provisions a per-tenant Azure OpenAI
        # account / Resource Group / Key Vault (the slow control-plane steps that
        # were the onboarding bottleneck).

        # ── Step 1: Shared blob prefix ───────────────────────────────────────
        await _emit(on_step, "blob_prefix", "running")
        try:
            blob_result = await AzureBlobService.provision_blob_storage(tenant_id)
            rollback_actions.append(("blob", blob_result.get("blob_prefix")))
            await _emit(on_step, "blob_prefix", "success")
        except Exception as exc:
            await _emit(on_step, "blob_prefix", "failed", str(exc))
            await rollback()
            raise NokvoOneProvisioningError("blob_prefix", str(exc), exc) from exc

        # ── Step 2: Qdrant collection ────────────────────────────────────────
        await _emit(on_step, "qdrant_collection", "running")
        try:
            qdrant_collection = await QdrantService.provision_collection(tenant_id)
            rollback_actions.append(("qdrant", qdrant_collection))
            await _emit(on_step, "qdrant_collection", "success")
        except Exception as exc:
            await _emit(on_step, "qdrant_collection", "failed", str(exc))
            await rollback()
            raise NokvoOneProvisioningError("qdrant_collection", str(exc), exc) from exc

        # ── Step 3: Redis namespace ──────────────────────────────────────────
        await _emit(on_step, "redis_namespace", "running")
        try:
            redis_namespace = await RedisTenantService.provision_redis(tenant_id)
            rollback_actions.append(("redis", redis_namespace))
            await _emit(on_step, "redis_namespace", "success")
        except Exception as exc:
            await _emit(on_step, "redis_namespace", "failed", str(exc))
            await rollback()
            raise NokvoOneProvisioningError("redis_namespace", str(exc), exc) from exc

        # ── Step 4: Plivo telephony (subaccount + Application + DID) ──────────
        # Each tenant gets its own Plivo subaccount and an Application whose
        # answer_url points at our inbound webhook (automatic webhook config). A DID
        # is rented + assigned; India DIDs may be KYC/compliance-pending, in which
        # case the number stays `pending_verification` (signup never blocks). If
        # Plivo isn't configured, we reserve a pending slot like the old placeholder.
        await _emit(on_step, "plivo_telephony", "running")
        link_id = str(uuid.uuid4())
        plivo_record = {
            "provider": "plivo",
            "status": "pending_provisioning",
            "link_id": link_id,
            "subaccount_auth_id": None,
            "subaccount_auth_token_enc": None,
            "application_id": None,
            "answer_url": None,
            "number": None,
            "number_status": "pending_verification",
            "forward_from_number": None,
        }
        try:
            from app.services.plivo_service import PlivoService, PlivoError
            base = (settings.PLIVO_WEBHOOK_BASE_URL or "").rstrip("/")
            if not (settings.PLIVO_AUTH_ID and settings.PLIVO_AUTH_TOKEN and base):
                raise PlivoError("Plivo master creds / PLIVO_WEBHOOK_BASE_URL not configured")
            answer_url = f"{base}/api/nokvo-one/agents/plivo/voice/{link_id}"
            sub = await PlivoService.create_subaccount(f"{slug}-{tenant_id[:8]}")
            rollback_actions.append(("plivo_subaccount", sub["auth_id"]))
            app_id = await PlivoService.create_application(app_name=f"nokvo-{slug}", answer_url=answer_url)
            rollback_actions.append(("plivo_application", app_id))
            plivo_record.update({
                "status": "linked",
                "subaccount_auth_id": sub["auth_id"],
                "subaccount_auth_token_enc": PlivoService.encrypt_token(sub["auth_token"]),
                "application_id": app_id,
                "answer_url": answer_url,
            })
            try:
                rented = await PlivoService.rent_number(
                    country=settings.PLIVO_NUMBER_COUNTRY, app_id=app_id, sub_auth_id=sub["auth_id"]
                )
                rollback_actions.append(("plivo_number", rented["number"]))
                plivo_record["number"] = rented["number"]
                plivo_record["number_status"] = "active"
                await _emit(on_step, "plivo_telephony", "success")
            except PlivoError as num_exc:
                # DID not instantly rentable (India KYC/regulatory) — leave pending.
                await _emit(on_step, "plivo_telephony", "pending_number", str(num_exc))
        except Exception as exc:  # noqa: BLE001 — never block signup on telephony
            await _emit(on_step, "plivo_telephony", "pending_credentials", str(exc))

        # ── Assemble TenantResources seed ────────────────────────────────────
        # No per-tenant LLM/embedding/Key-Vault resources: chat is served by the
        # shared pool, embeddings by the global OpenAI key, STT/TTS by the global
        # Sarvam key. provider_status records the shared modes (no secrets/endpoints).
        provider_status = {
            "product_tier": "nokvo_one",
            "llm_provider": "azure_openai_pool",
            "llm_status": "pooled",
            "embedding_provider": "openai_global",
            "embedding_status": "global",
            "stt_provider": "sarvam",
            "stt_status": "global",
            "tts_provider": "sarvam",
            "tts_status": "global",
            "qdrant_status": "provisioned",
            "qdrant_url_ref": QdrantService.cluster_ref(),
            "redis_status": "provisioned",
            "redis_mode": "shared",
            "plivo": plivo_record,
            "agent_phone_link": {
                "link_id": link_id,
                "provider": "plivo",
                "status": plivo_record["status"],
            },
        }

        return {
            "tenant_id": tenant_id,
            "azure_resource_group_name": None,
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
                {"name": "blob_prefix", "status": "success"},
                {"name": "qdrant_collection", "status": "success"},
                {"name": "redis_namespace", "status": "success"},
                {"name": "plivo_telephony", "status": plivo_record["status"]},
            ],
        }
