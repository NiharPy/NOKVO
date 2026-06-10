"""All-or-nothing semantics for the slimmed Nokvo One provisioner.

After the provisioning overhaul, signup provisions only shared/fast resources —
4 ordered steps: blob prefix → Qdrant collection → Redis namespace → Exotel
placeholder. The chat LLM (shared pool), embeddings (global OpenAI) and STT/TTS
(global Sarvam) are NOT provisioned per tenant. If a step fails, every earlier
step rolls back before the error reaches the caller.
"""
from __future__ import annotations

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from app.services.nokvo_one_provisioning_service import (
    NokvoOneProvisioningError,
    NokvoOneProvisioningService,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _common_patches(blob=None, qdrant=None, redis=None):
    blob = blob or AsyncMock(
        return_value={"storage_account_name": "sa", "container_name": "c", "blob_prefix": "tenants/x/"}
    )
    qdrant = qdrant or AsyncMock(return_value="qd")
    redis = redis or AsyncMock(return_value="ns")
    return [
        patch("app.services.nokvo_one_provisioning_service.AzureBlobService.provision_blob_storage", blob),
        patch("app.services.nokvo_one_provisioning_service.QdrantService.provision_collection", qdrant),
        patch("app.services.nokvo_one_provisioning_service.RedisTenantService.provision_redis", redis),
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
        # No per-tenant Azure resource group anymore.
        assert result["azure_resource_group_name"] is None
        assert result["qdrant_collection_name"] == "qd"
        assert result["redis_namespace"] == "ns"
        ps = result["provider_status"]
        assert ps["llm_provider"] == "azure_openai_pool"
        assert ps["llm_status"] == "pooled"
        assert ps["embedding_status"] == "global"
        assert ps["stt_status"] == "global" and ps["tts_status"] == "global"
        # Plivo not configured in tests → telephony reserves a pending slot, never blocks.
        assert ps["plivo"]["status"] == "pending_provisioning"
        assert ps["plivo"]["number_status"] == "pending_verification"
        assert ps["agent_phone_link"]["link_id"] and ps["agent_phone_link"]["provider"] == "plivo"
        # No per-tenant LLM key / Key Vault leaks into provider_status.
        assert "llm_api_key_secret_ref" not in ps and "key_vault_status" not in ps
        step_names = {s["name"] for s in result["provisioning_steps"]}
        assert step_names == {"blob_prefix", "qdrant_collection", "redis_namespace", "plivo_telephony"}
        assert "azure_openai_chat" not in step_names and "shared_key_vault" not in step_names
    finally:
        for p in patches:
            p.stop()


def test_provisioner_rolls_back_when_qdrant_fails():
    """Blob succeeds; Qdrant blows up → the blob prefix must roll back."""
    blob_delete = AsyncMock()
    qdrant = AsyncMock(side_effect=RuntimeError("qdrant down"))

    patches = _common_patches(qdrant=qdrant) + [
        patch("app.services.nokvo_one_provisioning_service._delete_blob_prefix", blob_delete),
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
    finally:
        for p in patches:
            p.stop()


def test_provisioner_rolls_back_when_redis_fails():
    blob_delete = AsyncMock()
    qd_delete = AsyncMock()
    redis = AsyncMock(side_effect=RuntimeError("redis offline"))

    patches = _common_patches(redis=redis) + [
        patch("app.services.nokvo_one_provisioning_service._delete_blob_prefix", blob_delete),
        patch("app.services.nokvo_one_provisioning_service._delete_qdrant_collection", qd_delete),
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
    finally:
        for p in patches:
            p.stop()


def test_on_step_emits_running_then_success_for_each_step():
    events: list[tuple[str, str]] = []

    async def on_step(name, status_, _msg=None):
        events.append((name, status_))

    patches = _common_patches()
    for p in patches:
        p.start()
    try:
        _run(
            NokvoOneProvisioningService.provision_or_raise(
                organization_id=uuid.uuid4(),
                organization_name="Acme Inc",
                on_step=on_step,
            )
        )
    finally:
        for p in patches:
            p.stop()

    step_names = [n for n, _ in events]
    for required in ["blob_prefix", "qdrant_collection", "redis_namespace", "plivo_telephony"]:
        assert required in step_names, f"missing step {required} in {step_names}"
    # The retired Azure steps must NOT be emitted.
    assert "resource_group" not in step_names and "azure_openai_chat" not in step_names
    blob_events = [s for n, s in events if n == "blob_prefix"]
    assert blob_events[:2] == ["running", "success"]


def test_on_step_emits_failed_then_provisioning_raises():
    events: list[tuple[str, str]] = []

    async def on_step(name, status_, _msg=None):
        events.append((name, status_))

    blob = AsyncMock(side_effect=RuntimeError("storage offline"))
    patches = _common_patches(blob=blob)
    for p in patches:
        p.start()
    try:
        with pytest.raises(NokvoOneProvisioningError):
            _run(
                NokvoOneProvisioningService.provision_or_raise(
                    organization_id=uuid.uuid4(),
                    organization_name="Acme",
                    on_step=on_step,
                )
            )
    finally:
        for p in patches:
            p.stop()
    assert ("blob_prefix", "running") in events
    assert ("blob_prefix", "failed") in events
