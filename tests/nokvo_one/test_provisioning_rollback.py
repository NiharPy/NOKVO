"""All-or-nothing semantics for the Nokvo One provisioner.

The orchestrator runs 6 ordered steps. If any step fails, every previously
completed step must be rolled back before the error reaches the caller.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.nokvo_one_provisioning_service import (
    NokvoOneProvisioningError,
    NokvoOneProvisioningService,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _common_patches(
    rg=None,
    ai=None,
    blob=None,
    qdrant=None,
    redis=None,
    kv_refs=None,
    kv_set=None,
    settings_kv_name="shared-vault",
):
    rg = rg or AsyncMock(return_value="rg-test")
    ai = ai or AsyncMock(
        return_value={
            "status": "provisioned",
            "account_name": "acct",
            "deployment_name": "d",
            "model": "m",
            "model_version": "v",
            "region": "r",
            "endpoint": "https://e",
            "api_key_encrypted": "enc",
            "_api_key_raw": "RAW",
        }
    )
    blob = blob or AsyncMock(
        return_value={"storage_account_name": "sa", "container_name": "c", "blob_prefix": "tenants/x/"}
    )
    qdrant = qdrant or AsyncMock(return_value="qd")
    redis = redis or AsyncMock(return_value="ns")
    kv_refs = kv_refs or AsyncMock(
        return_value={
            "llm_api_key": {"secret_name": "tenant-x-llm-api-key"},
            "_compliance": {"soft_delete_enabled": True, "purge_protection_enabled": True},
        }
    )
    kv_set = kv_set or AsyncMock(return_value=None)
    return [
        patch(
            "app.services.nokvo_one_provisioning_service.AzureResourceGroupService.provision_resource_group",
            rg,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureRealtimeAIService.provision_realtime_account",
            ai,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureBlobService.provision_blob_storage",
            blob,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.QdrantService.provision_collection",
            qdrant,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.RedisTenantService.provision_redis",
            redis,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureKeyVaultService.provision_secret_refs",
            kv_refs,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureKeyVaultService.set_secret_value",
            kv_set,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.settings.AZURE_SHARED_KEY_VAULT_NAME",
            settings_kv_name,
        ),
    ]


def test_provisioner_returns_resources_when_all_steps_succeed():
    patches = _common_patches()
    for p in patches:
        p.start()
    try:
        result = _run(
            NokvoOneProvisioningService.provision_or_raise(
                organization_id=uuid.uuid4(),
                organization_name="Acme Inc",
            )
        )
        assert result["provisioning_status"] == "success"
        assert result["azure_resource_group_name"] == "rg-test"
        assert result["qdrant_collection_name"] == "qd"
        assert result["redis_namespace"] == "ns"
        ps = result["provider_status"]
        assert ps["llm_provider"] == "azure_openai"
        assert ps["exotel"]["status"] == "pending_credentials"
        assert ps["key_vault_status"] == "provisioned"
        assert ps["llm_api_key_secret_ref"] == "tenant-x-llm-api-key"
        # Raw key MUST NOT leak into provider_status.
        assert "_api_key_raw" not in ps
        step_names = {s["name"] for s in result["provisioning_steps"]}
        assert "shared_key_vault" in step_names
    finally:
        for p in patches:
            p.stop()


def test_provisioner_rolls_back_when_key_vault_fails():
    """Key Vault is step 3. RG and AI succeed; KV blows up. Both earlier steps must roll back."""
    rg_delete = AsyncMock(return_value=True)
    ai_delete = AsyncMock(return_value=True)
    kv_delete = AsyncMock(return_value=True)

    kv_refs = AsyncMock(side_effect=RuntimeError("vault offline"))

    patches = _common_patches(kv_refs=kv_refs) + [
        patch(
            "app.services.nokvo_one_provisioning_service.AzureResourceGroupService.delete_resource_group",
            rg_delete,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureRealtimeAIService.delete_realtime_account",
            ai_delete,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureKeyVaultService.delete_secret",
            kv_delete,
        ),
    ]
    for p in patches:
        p.start()
    try:
        with pytest.raises(NokvoOneProvisioningError) as excinfo:
            _run(
                NokvoOneProvisioningService.provision_or_raise(
                    organization_id=uuid.uuid4(),
                    organization_name="Acme",
                )
            )
        assert excinfo.value.step == "shared_key_vault"
        ai_delete.assert_awaited_once()
        rg_delete.assert_awaited_once()
    finally:
        for p in patches:
            p.stop()


def test_provisioner_rolls_back_when_qdrant_fails():
    """RG, AI, KV, and blob succeed; Qdrant blows up. All four earlier steps must roll back."""
    rg_delete = AsyncMock(return_value=True)
    ai_delete = AsyncMock(return_value=True)
    kv_delete = AsyncMock(return_value=True)
    blob_delete = AsyncMock()

    qdrant = AsyncMock(side_effect=RuntimeError("qdrant down"))

    patches = _common_patches(qdrant=qdrant) + [
        patch(
            "app.services.nokvo_one_provisioning_service.AzureResourceGroupService.delete_resource_group",
            rg_delete,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureRealtimeAIService.delete_realtime_account",
            ai_delete,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureKeyVaultService.delete_secret",
            kv_delete,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service._delete_blob_prefix",
            blob_delete,
        ),
    ]

    for p in patches:
        p.start()
    try:
        with pytest.raises(NokvoOneProvisioningError) as excinfo:
            _run(
                NokvoOneProvisioningService.provision_or_raise(
                    organization_id=uuid.uuid4(),
                    organization_name="Acme Inc",
                )
            )
        assert excinfo.value.step == "qdrant_collection"
        blob_delete.assert_awaited_once()
        kv_delete.assert_awaited()  # at least one secret deleted
        ai_delete.assert_awaited_once()
        rg_delete.assert_awaited_once_with("rg-nokvo1-acme-inc-staging")
    finally:
        for p in patches:
            p.stop()


def test_provisioner_rolls_back_when_redis_fails():
    rg_delete = AsyncMock(return_value=True)
    ai_delete = AsyncMock(return_value=True)
    kv_delete = AsyncMock(return_value=True)
    blob_delete = AsyncMock()
    qd_delete = AsyncMock()

    redis = AsyncMock(side_effect=RuntimeError("redis offline"))

    patches = _common_patches(redis=redis) + [
        patch(
            "app.services.nokvo_one_provisioning_service.AzureResourceGroupService.delete_resource_group",
            rg_delete,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureRealtimeAIService.delete_realtime_account",
            ai_delete,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service.AzureKeyVaultService.delete_secret",
            kv_delete,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service._delete_blob_prefix",
            blob_delete,
        ),
        patch(
            "app.services.nokvo_one_provisioning_service._delete_qdrant_collection",
            qd_delete,
        ),
    ]
    for p in patches:
        p.start()
    try:
        with pytest.raises(NokvoOneProvisioningError) as excinfo:
            _run(
                NokvoOneProvisioningService.provision_or_raise(
                    organization_id=uuid.uuid4(),
                    organization_name="Acme",
                )
            )
        assert excinfo.value.step == "redis_namespace"
        qd_delete.assert_awaited_once_with("qd")
        blob_delete.assert_awaited_once()
        kv_delete.assert_awaited()
        ai_delete.assert_awaited_once()
        rg_delete.assert_awaited_once()
    finally:
        for p in patches:
            p.stop()


def test_provisioner_raises_when_rg_fails_immediately():
    rg = AsyncMock(side_effect=RuntimeError("subscription not configured"))
    patches = _common_patches(rg=rg)
    for p in patches:
        p.start()
    try:
        with pytest.raises(NokvoOneProvisioningError) as excinfo:
            _run(
                NokvoOneProvisioningService.provision_or_raise(
                    organization_id=uuid.uuid4(),
                    organization_name="Acme",
                )
            )
        assert excinfo.value.step == "resource_group"
    finally:
        for p in patches:
            p.stop()
