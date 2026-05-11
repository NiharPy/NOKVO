from azure.keyvault.secrets import SecretClient
from azure.keyvault.secrets.aio import SecretClient as AsyncSecretClient
from azure.mgmt.resource import ResourceManagementClient
from azure.mgmt.resource.resources.models import GenericResource
from app.core.config import settings
from app.core.azure_auth import AzureAuth
from datetime import datetime, timedelta, timezone
import re

KEY_VAULT_API_VERSION = "2024-11-01"
DIAGNOSTIC_SETTINGS_API_VERSION = "2021-05-01-preview"
DIAGNOSTIC_SETTINGS_NAME = "nokvo-keyvault-audit"


class AzureKeyVaultService:
    @staticmethod
    def _normalize_token(value: str) -> str:
        return re.sub(r"[^a-z0-9-]", "-", value.lower())

    @staticmethod
    def _secret_name(tenant_id: str, suffix: str) -> str:
        normalized_tenant = AzureKeyVaultService._normalize_token(tenant_id)
        if normalized_tenant.startswith("tenant-"):
            normalized_tenant = normalized_tenant[len("tenant-"):]
        name = f"tenant-{normalized_tenant}-{suffix}"
        return name[:127].strip("-")

    @staticmethod
    def _rotation_metadata() -> dict:
        now = datetime.now(timezone.utc)
        expires_on = now + timedelta(days=settings.KEY_VAULT_SECRET_ROTATION_DAYS)
        return {
            "rotation_policy": "placeholder-rotation",
            "rotation_interval_days": settings.KEY_VAULT_SECRET_ROTATION_DAYS,
            "created_at": now.isoformat(),
            "next_rotation_due_at": expires_on.isoformat(),
            "last_rotated_at": None,
        }

    @staticmethod
    def _find_shared_vault(resource_client: ResourceManagementClient, vault_name: str):
        for resource in resource_client.resources.list(filter="resourceType eq 'Microsoft.KeyVault/vaults'"):
            if getattr(resource, "name", None) == vault_name:
                return resource
        raise RuntimeError(f"Shared Key Vault '{vault_name}' was not found in this subscription.")

    @staticmethod
    def _ensure_vault_hardening(resource_client: ResourceManagementClient, vault_name: str) -> str:
        compliance = {
            "soft_delete_enabled": False,
            "purge_protection_enabled": False,
            "audit_logs_enabled": False,
        }
        vault = AzureKeyVaultService._find_shared_vault(resource_client, vault_name)
        vault_resource = resource_client.resources.get_by_id(vault.id, KEY_VAULT_API_VERSION)
        props = dict(getattr(vault_resource, "properties", {}) or {})
        needs_update = (
            props.get("enableSoftDelete") is not True
            or props.get("enablePurgeProtection") is not True
        )

        if needs_update:
            props["enableSoftDelete"] = True
            props["enablePurgeProtection"] = True
            parameters = GenericResource(
                location=getattr(vault_resource, "location", None),
                tags=getattr(vault_resource, "tags", None),
                properties=props,
                sku=getattr(vault_resource, "sku", None),
            )
            resource_client.resources.begin_create_or_update_by_id(
                vault.id,
                KEY_VAULT_API_VERSION,
                parameters,
            ).result()
            compliance["soft_delete_enabled"] = True
            compliance["purge_protection_enabled"] = True
        else:
            compliance["soft_delete_enabled"] = props.get("enableSoftDelete") is True
            compliance["purge_protection_enabled"] = props.get("enablePurgeProtection") is True

        workspace_id = settings.AZURE_LOG_ANALYTICS_WORKSPACE_ID
        if not workspace_id:
            if settings.ENFORCE_KEY_VAULT_AUDIT_LOGS:
                raise RuntimeError("AZURE_LOG_ANALYTICS_WORKSPACE_ID is required to enable Key Vault audit logs.")
            return compliance

        diagnostic_resource_id = (
            f"{vault.id}/providers/Microsoft.Insights/diagnosticSettings/{DIAGNOSTIC_SETTINGS_NAME}"
        )
        diagnostic_payload = GenericResource(
            properties={
                "workspaceId": workspace_id,
                "logAnalyticsDestinationType": "Dedicated",
                "logs": [
                    {
                        "categoryGroup": "Audit",
                        "enabled": True,
                        "retentionPolicy": {"days": 0, "enabled": False},
                    }
                ],
                "metrics": [
                    {
                        "category": "AllMetrics",
                        "enabled": True,
                        "retentionPolicy": {"days": 0, "enabled": False},
                    }
                ],
            }
        )
        resource_client.resources.begin_create_or_update_by_id(
            diagnostic_resource_id,
            DIAGNOSTIC_SETTINGS_API_VERSION,
            diagnostic_payload,
        ).result()
        compliance["audit_logs_enabled"] = True

        return compliance

    @staticmethod
    async def provision_secret_refs(tenant_id: str) -> dict:
        """
        Creates placeholder secret references in Azure Key Vault.
        Does not store real values unless provided.
        Returns the dictionary of references.
        """
        kv_name = settings.AZURE_SHARED_KEY_VAULT_NAME
        rotation = AzureKeyVaultService._rotation_metadata()

        refs = {
            "llm_api_key": {
                "secret_name": AzureKeyVaultService._secret_name(tenant_id, "llm-api-key"),
                **rotation,
            },
            "stt_api_key": {
                "secret_name": AzureKeyVaultService._secret_name(tenant_id, "stt-api-key"),
                **rotation,
            },
            "tts_api_key": {
                "secret_name": AzureKeyVaultService._secret_name(tenant_id, "tts-api-key"),
                **rotation,
            },
            "twilio_account_sid": {
                "secret_name": AzureKeyVaultService._secret_name(tenant_id, "twilio-account-sid"),
                **rotation,
            },
            "twilio_auth_token": {
                "secret_name": AzureKeyVaultService._secret_name(tenant_id, "twilio-auth-token"),
                **rotation,
            },
            "db_connection_string": {
                "secret_name": AzureKeyVaultService._secret_name(tenant_id, "db-connection-string"),
                **rotation,
            },
            "crm_access_token": {
                "secret_name": AzureKeyVaultService._secret_name(tenant_id, "crm-access-token"),
                **rotation,
            },
            "crm_refresh_token": {
                "secret_name": AzureKeyVaultService._secret_name(tenant_id, "crm-refresh-token"),
                **rotation,
            },
        }
        compliance = {
            "soft_delete_enabled": False,
            "purge_protection_enabled": False,
            "audit_logs_enabled": False,
        }

        if not kv_name:
            # Skip actual KV provisioning if no vault is configured
            refs["_compliance"] = compliance
            return refs

        kv_url = f"https://{kv_name}.vault.azure.net/"
        credential = AzureAuth.get_credential()

        try:
            if not settings.AZURE_SUBSCRIPTION_ID:
                raise RuntimeError("AZURE_SUBSCRIPTION_ID is required for Key Vault compliance checks.")

            resource_client = ResourceManagementClient(credential, settings.AZURE_SUBSCRIPTION_ID)
            compliance = AzureKeyVaultService._ensure_vault_hardening(resource_client, kv_name)
            client = SecretClient(vault_url=kv_url, credential=credential)

            # Create empty placeholders in KV (disabled by default so they aren't actively used until populated)
            for key, ref in refs.items():
                client.set_secret(
                    ref["secret_name"],
                    "PLACEHOLDER",
                    enabled=False,
                    content_type="reference-only",
                    tags={
                        "tenant_id": tenant_id,
                        "secret_role": key,
                        "rotation_interval_days": str(ref["rotation_interval_days"]),
                        "next_rotation_due_at": ref["next_rotation_due_at"],
                    },
                    expires_on=datetime.fromisoformat(ref["next_rotation_due_at"]),
                )

            refs["_compliance"] = compliance
            return refs
        except Exception as e:
            raise RuntimeError(f"Failed to provision Key Vault secrets: {str(e)}")

    @staticmethod
    async def set_secret_value(secret_name: str, secret_value: str, tenant_id: str, secret_role: str) -> None:
        kv_name = settings.AZURE_SHARED_KEY_VAULT_NAME
        if not kv_name:
            return

        kv_url = f"https://{kv_name}.vault.azure.net/"
        credential = AzureAuth.get_async_credential()
        rotation = AzureKeyVaultService._rotation_metadata()
        async with AsyncSecretClient(vault_url=kv_url, credential=credential) as client:
            await client.set_secret(
                secret_name,
                secret_value,
                enabled=True,
                content_type="runtime-credential",
                tags={
                    "tenant_id": tenant_id,
                    "secret_role": secret_role,
                    "rotation_interval_days": str(rotation["rotation_interval_days"]),
                    "next_rotation_due_at": rotation["next_rotation_due_at"],
                },
                expires_on=datetime.fromisoformat(rotation["next_rotation_due_at"]),
            )

    @staticmethod
    async def delete_secret(secret_name: str) -> bool:
        """Best-effort delete of a single secret. Used by Nokvo One provisioner rollback. Never raises."""
        kv_name = settings.AZURE_SHARED_KEY_VAULT_NAME
        if not kv_name:
            return True
        kv_url = f"https://{kv_name}.vault.azure.net/"
        credential = AzureAuth.get_async_credential()
        async with AsyncSecretClient(vault_url=kv_url, credential=credential) as client:
            try:
                poller = await client.begin_delete_secret(secret_name)
                await poller.wait()
                return True
            except Exception:
                import logging
                logging.getLogger(__name__).exception(
                    "Failed to delete Key Vault secret %s in vault %s during rollback",
                    secret_name,
                    kv_name,
                )
                return False

    @staticmethod
    async def get_secret_value(secret_name: str) -> str | None:
        kv_name = settings.AZURE_SHARED_KEY_VAULT_NAME
        if not kv_name:
            return None

        kv_url = f"https://{kv_name}.vault.azure.net/"
        credential = AzureAuth.get_async_credential()
        async with AsyncSecretClient(vault_url=kv_url, credential=credential) as client:
            try:
                secret = await client.get_secret(secret_name)
            except Exception as exc:
                raise RuntimeError(
                    f"Key Vault lookup failed for secret '{secret_name}' "
                    f"in vault '{kv_name}' ({kv_url}): {exc}"
                ) from exc
        return secret.value
