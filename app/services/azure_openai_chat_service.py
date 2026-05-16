"""
Azure OpenAI chat (gpt-4.1-mini) provisioner for Nokvo One.

Replaces the prior gpt-realtime-mini provisioner. Voice is now handled by the
Sarvam STT -> gpt-4.1-mini -> Sarvam TTS pipeline, so the per-tenant Azure OpenAI
account only needs a standard chat deployment.

  - Creates an Azure OpenAI account in a tenant-specific resource group.
  - Deploys gpt-4.1-mini in South India (configurable via env).
  - Returns the endpoint + Fernet-encrypted API key.
  - Stores the raw key on the result transiently so the orchestrator can push it
    into shared Key Vault, then strips it before persisting provider_status.
  - Exposes a delete helper used by rollback.

If AZURE_SUBSCRIPTION_ID is unset, returns a 'skipped' status so dev/test
environments can still run the orchestrator without Azure credentials.
"""
from __future__ import annotations

import logging
from typing import Any

from azure.core.exceptions import HttpResponseError
from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
from azure.mgmt.cognitiveservices.models import (
    Account,
    AccountProperties,
    Deployment,
    DeploymentModel,
    DeploymentProperties,
    Sku,
)

from app.core.azure_auth import AzureAuth
from app.core.config import settings
from app.core.secret_crypto import encrypt_secret


logger = logging.getLogger(__name__)


def _format_http_error(error: HttpResponseError) -> str:
    status_code = getattr(error, "status_code", "unknown")
    pieces = [f"Azure OpenAI API error (status={status_code})"]
    body = getattr(error, "error", None)
    if body is not None:
        code = getattr(body, "code", None)
        message = getattr(body, "message", None)
        if code:
            pieces.append(f"code={code}")
        if message:
            pieces.append(message)
    return " | ".join(pieces)


class AzureOpenAIChatService:
    @staticmethod
    def _account_name(slug: str, tenant_id: str) -> str:
        """Per-signup-attempt name: 'n1-' + slug[:10] + '-' + tenant_id[:6] (stripped of dashes).

        Cognitive Services account names must be <=24 chars and ^[a-zA-Z0-9-]+$.
        The tenant_id suffix means two signup attempts (different tenant ids) never collide,
        so Azure's 30-day soft-delete window can't block the next signup.
        """
        suffix = tenant_id.replace("-", "")[:6]
        prefix_body = (slug or "tenant")[:10].strip("-") or "tenant"
        return f"n1-{prefix_body}-{suffix}"[:24]

    @staticmethod
    def _build_account_payload(region: str, tenant_id: str, account_name: str) -> Account:
        return Account(
            location=region,
            kind="OpenAI",
            sku=Sku(name="S0"),
            properties=AccountProperties(
                custom_sub_domain_name=account_name,
                public_network_access="Enabled",
                disable_local_auth=False,
                dynamic_throttling_enabled=False,
                restrict_outbound_network_access=False,
            ),
            tags={
                "app": "nokvo",
                "product_tier": "nokvo_one",
                "tenant_id": tenant_id,
                "managed_by": "nokvo-platform",
            },
        )

    @staticmethod
    def _purge_soft_deleted_account(client: CognitiveServicesManagementClient, region: str, account_name: str) -> bool:
        """Purge a soft-deleted Cognitive Services account so the name is free for reuse.

        Returns True if a purge was issued. Swallows errors — the caller's create will
        re-raise if the name is still blocked.
        """
        try:
            client.deleted_accounts.begin_purge(
                location=region,
                resource_group_name="dummy",  # purge ignores RG; soft-deleted accounts are tracked by (location, name)
                account_name=account_name,
            ).wait()
            return True
        except TypeError:
            try:
                client.deleted_accounts.begin_purge(location=region, account_name=account_name).wait()
                return True
            except Exception:
                logger.exception("Failed to purge soft-deleted Cognitive Services account %s", account_name)
                return False
        except Exception:
            logger.exception("Failed to purge soft-deleted Cognitive Services account %s", account_name)
            return False

    @staticmethod
    async def provision_chat_account(
        rg_name: str,
        tenant_id: str,
        slug: str,
        organization_name: str,
    ) -> dict:
        region = settings.AZURE_OPENAI_CHAT_REGION or "southindia"
        account_name = AzureOpenAIChatService._account_name(slug, tenant_id)

        if not settings.AZURE_SUBSCRIPTION_ID:
            return {
                "account_name": account_name,
                "deployment_name": settings.AZURE_OPENAI_CHAT_DEPLOYMENT,
                "model": settings.AZURE_OPENAI_CHAT_MODEL,
                "model_version": settings.AZURE_OPENAI_CHAT_MODEL_VERSION,
                "region": region,
                "endpoint": None,
                "api_key_encrypted": None,
                "status": "skipped_no_azure_subscription",
            }

        credential = AzureAuth.get_credential()
        client = CognitiveServicesManagementClient(credential, settings.AZURE_SUBSCRIPTION_ID)

        def _create_account() -> Any:
            return client.accounts.begin_create(
                resource_group_name=rg_name,
                account_name=account_name,
                account=AzureOpenAIChatService._build_account_payload(region, tenant_id, account_name),
            ).result()

        try:
            account = _create_account()
        except HttpResponseError as exc:
            error_code = getattr(getattr(exc, "error", None), "code", "") or ""
            if error_code == "FlagMustBeSetForRestore":
                logger.warning(
                    "Cognitive Services account %s is soft-deleted; purging and retrying.",
                    account_name,
                )
                AzureOpenAIChatService._purge_soft_deleted_account(client, region, account_name)
                try:
                    account = _create_account()
                except HttpResponseError as retry_exc:
                    raise RuntimeError(_format_http_error(retry_exc)) from retry_exc
            else:
                raise RuntimeError(_format_http_error(exc)) from exc

        try:
            deployment_payload = Deployment(
                sku=Sku(
                    name=settings.AZURE_OPENAI_CHAT_SKU,
                    capacity=settings.AZURE_OPENAI_CHAT_CAPACITY,
                ),
                properties=DeploymentProperties(
                    model=DeploymentModel(
                        format="OpenAI",
                        name=settings.AZURE_OPENAI_CHAT_MODEL,
                        version=settings.AZURE_OPENAI_CHAT_MODEL_VERSION,
                    ),
                    version_upgrade_option="OnceCurrentVersionExpired",
                ),
            )

            client.deployments.begin_create_or_update(
                resource_group_name=rg_name,
                account_name=account_name,
                deployment_name=settings.AZURE_OPENAI_CHAT_DEPLOYMENT,
                deployment=deployment_payload,
            ).result()

            keys = client.accounts.list_keys(resource_group_name=rg_name, account_name=account_name)
            raw_api_key = getattr(keys, "key1", None) or getattr(keys, "primary_key", None)
            if not raw_api_key:
                raise RuntimeError("Azure OpenAI did not return an API key after deployment")

            return {
                "account_name": account_name,
                "deployment_name": settings.AZURE_OPENAI_CHAT_DEPLOYMENT,
                "model": settings.AZURE_OPENAI_CHAT_MODEL,
                "model_version": settings.AZURE_OPENAI_CHAT_MODEL_VERSION,
                "region": region,
                "endpoint": account.properties.endpoint,
                "api_key_encrypted": encrypt_secret(raw_api_key),
                # Transient: the orchestrator writes this to shared Key Vault then strips
                # it before persisting provider_status. Never goes to Postgres.
                "_api_key_raw": raw_api_key,
                "status": "provisioned",
            }
        except HttpResponseError as exc:
            raise RuntimeError(_format_http_error(exc)) from exc
        except Exception as exc:
            raise RuntimeError(f"Failed to provision Azure OpenAI chat deployment: {exc}") from exc

    @staticmethod
    async def delete_chat_account(rg_name: str, account_name: str) -> bool:
        """Best-effort delete of a Cognitive Services account for rollback. Never raises."""
        if not settings.AZURE_SUBSCRIPTION_ID:
            return True
        try:
            credential = AzureAuth.get_credential()
            client = CognitiveServicesManagementClient(credential, settings.AZURE_SUBSCRIPTION_ID)
            client.accounts.begin_delete(resource_group_name=rg_name, account_name=account_name)
            return True
        except Exception:
            logger.exception(
                "Failed to delete Azure OpenAI account %s in %s during rollback",
                account_name,
                rg_name,
            )
            return False
