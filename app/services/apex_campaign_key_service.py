"""APEX campaign-scoped API keys — mint / reveal / rotate + result-webhook config.

One ``nkap_live_…`` key per campaign is the entire CRM integration credential:
it authenticates lead ingest (in the URL path, catch-hook style, or via the
usual headers) and resolves to exactly one campaign. Verification reuses the
argon2 hash on ``organization_api_keys``; the raw key is ADDITIONALLY kept in
KeyVault so the dashboard's [Reveal] works for non-developer operators (blast
radius of a campaign key is one campaign's contact list). When KeyVault isn't
configured (dev), reveal returns ``None`` and the UI falls back to
shown-once-at-mint.

The result-webhook config (URL + HMAC signing secret) lives on
``campaign.agent_config["crm_webhook"]`` — JSONB like ``call_window``, so it
survives key rotation and needs no migration. The signing secret must be
recoverable to sign deliveries, so its raw value also lives in KeyVault.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core import api_keys as api_key_utils
from app.models.connect_api_key import OrganizationApiKey
from app.models.organization import Organization
from app.models.outbound_campaign import OutboundCampaign
from app.models.tenant_resources import TenantResources
from app.services.azure_keyvault_service import AzureKeyVaultService

logger = logging.getLogger(__name__)

APEX_KEY_SCOPES = ["apex.leads"]


def _key_secret_name(tenant_id: str, campaign: OutboundCampaign) -> str:
    return AzureKeyVaultService._secret_name(tenant_id, f"campaign-{campaign.id.hex}-apikey")


def _whsec_secret_name(tenant_id: str, campaign: OutboundCampaign) -> str:
    return AzureKeyVaultService._secret_name(tenant_id, f"campaign-{campaign.id.hex}-whsec")


async def active_key_for_campaign(
    db: AsyncSession, campaign_id
) -> OrganizationApiKey | None:
    res = await db.execute(
        select(OrganizationApiKey).where(
            OrganizationApiKey.campaign_id == campaign_id,
            OrganizationApiKey.status == "active",
        )
    )
    return res.scalars().first()


async def mint_for_campaign(
    db: AsyncSession,
    org: Organization,
    campaign: OutboundCampaign,
    tenant_res: TenantResources,
    *,
    created_by_user_id=None,
) -> tuple[OrganizationApiKey, str]:
    """Mint the campaign's ``nkap_`` key: argon2 hash on the key row (the
    verification source of truth) + raw copy in KeyVault (the [Reveal] source).
    Returns ``(record, raw_key)`` — raw is also shown once in the response."""
    minted = api_key_utils.mint("live", family="nkap")
    record = OrganizationApiKey(
        organization_id=org.id,
        campaign_id=campaign.id,
        label=f"APEX campaign: {campaign.name}"[:120],
        key_prefix=minted.key_prefix,
        secret_hash=minted.secret_hash,
        mode="live",
        scopes=list(APEX_KEY_SCOPES),
        created_by_user_id=created_by_user_id,
    )
    db.add(record)
    try:
        await AzureKeyVaultService.set_secret_value(
            _key_secret_name(tenant_res.tenant_id, campaign),
            minted.raw,
            tenant_res.tenant_id,
            "apex-campaign-api-key",
        )
    except Exception:
        # Reveal degrades to shown-once; the key itself still works (hash row).
        logger.exception("APEX-KEY: KeyVault store failed for campaign=%s", campaign.id)
    return record, minted.raw


async def reveal(tenant_res: TenantResources, campaign: OutboundCampaign) -> str | None:
    try:
        return await AzureKeyVaultService.get_secret_value(
            _key_secret_name(tenant_res.tenant_id, campaign)
        )
    except Exception:
        logger.exception("APEX-KEY: KeyVault reveal failed for campaign=%s", campaign.id)
        return None


async def rotate(
    db: AsyncSession,
    org: Organization,
    campaign: OutboundCampaign,
    tenant_res: TenantResources,
    *,
    created_by_user_id=None,
) -> tuple[OrganizationApiKey, str]:
    """Revoke the campaign's active key(s) and mint a fresh one. The KeyVault
    copy is overwritten, so [Reveal] always returns the CURRENT key. Webhook
    config lives on agent_config → untouched by rotation."""
    res = await db.execute(
        select(OrganizationApiKey).where(
            OrganizationApiKey.campaign_id == campaign.id,
            OrganizationApiKey.status == "active",
        )
    )
    now = datetime.now(timezone.utc)
    for old in res.scalars().all():
        old.status = "revoked"
        old.revoked_at = now
        old.revoke_reason = "rotated"
        db.add(old)
    return await mint_for_campaign(
        db, org, campaign, tenant_res, created_by_user_id=created_by_user_id
    )


def webhook_config(campaign: OutboundCampaign) -> dict[str, Any]:
    return dict((campaign.agent_config or {}).get("crm_webhook") or {})


async def set_result_webhook(
    db: AsyncSession,
    campaign: OutboundCampaign,
    tenant_res: TenantResources,
    *,
    url: str | None,
) -> str | None:
    """Set (or clear, with ``url=None``) the campaign's result-webhook URL.
    Setting a URL for the first time — or after a clear — generates a fresh
    ``whsec_…`` signing secret and returns its raw value (shown to the operator
    to paste into their verifier; retrievable again via KeyVault). Updating the
    URL with a secret already in place keeps the secret and returns ``None``."""
    cfg = webhook_config(campaign)
    raw_secret: str | None = None
    if not url:
        cfg = {}
    else:
        cfg["url"] = url.strip()
        cfg["enabled"] = True
        if not cfg.get("secret_name"):
            raw_secret, _hash = api_key_utils.mint_webhook_secret()
            secret_name = _whsec_secret_name(tenant_res.tenant_id, campaign)
            cfg["secret_name"] = secret_name
            try:
                await AzureKeyVaultService.set_secret_value(
                    secret_name, raw_secret, tenant_res.tenant_id, "apex-campaign-whsec"
                )
            except Exception:
                # Without a recoverable secret we can't sign later — keep the
                # raw in config as a dev-mode fallback (KeyVault-less envs).
                logger.exception(
                    "APEX-KEY: KeyVault whsec store failed for campaign=%s", campaign.id
                )
                cfg["secret_fallback"] = raw_secret
    campaign.agent_config = {**(campaign.agent_config or {}), "crm_webhook": cfg}
    flag_modified(campaign, "agent_config")
    db.add(campaign)
    return raw_secret


async def signing_secret(campaign: OutboundCampaign) -> str | None:
    """The raw ``whsec_…`` for delivery signing. KeyVault first, dev fallback second."""
    cfg = webhook_config(campaign)
    name = cfg.get("secret_name")
    if name:
        try:
            value = await AzureKeyVaultService.get_secret_value(name)
            if value:
                return value
        except Exception:
            logger.exception(
                "APEX-KEY: KeyVault whsec fetch failed for campaign=%s", campaign.id
            )
    return cfg.get("secret_fallback")
