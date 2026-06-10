"""Deprovision LEGACY per-tenant Azure resources after the provisioning overhaul.

Tenants provisioned before the overhaul each own an Azure OpenAI account (in a
per-tenant Resource Group) + Key Vault secrets. Those are now dead weight:

    chat       → shared LLM pool (app.services.llm_pool)
    embeddings → global OPENAI_API_KEY
    STT/TTS    → global SARVAM_API_KEY

This script tears down, per still-active tenant, the **Resource Group** (which
nukes the Azure OpenAI account inside it) and the **Key Vault secrets**, then
rewrites ``provider_status`` to the pooled/global shape (and clears
``azure_resource_group_name`` / ``key_vault_name`` / ``secret_refs``). The tenant
keeps running on the pool — its org, Redis, Qdrant collection, Blob prefix and
Exotel config are LEFT INTACT.

(Qdrant + Blob are intentionally NOT torn down here — they're still provisioned
and only become removable once the KB/document-RAG retirement is finished; see
KB_RETIREMENT_REMAINING.md PR5.)

USAGE
-----
Dry run (default — lists what WOULD change, mutates nothing):

    source venv/bin/activate
    python -m app.scripts.deprovision_legacy_resources

Live run:

    python -m app.scripts.deprovision_legacy_resources --i-understand

SAFETY
------
- Dry-run by default; ``--i-understand`` + a typed confirmation phrase required to mutate.
- Idempotent: a tenant already migrated (no resource group, ``llm_status='pooled'``)
  is skipped, so re-running is a no-op.
- Per-resource failures are logged and don't abort the run; the DB row is only
  rewritten after the cloud teardown is attempted.
- Only ``product_tier='nokvo_one'`` tenants with a legacy resource group are touched.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm.attributes import flag_modified

from app.db.session import AsyncSessionLocal
from app.models.tenant_resources import TenantResources
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.azure_resource_group_service import AzureResourceGroupService

CONFIRMATION_PHRASE = "DEPROVISION LEGACY RESOURCES"

_LEGACY_PS_PREFIXES = ("llm_", "embedding_", "key_vault_")


def _log(msg: str) -> None:
    print(msg, flush=True)


def _is_legacy(tenant: TenantResources) -> bool:
    """A tenant still carrying a per-tenant Azure footprint to tear down."""
    ps = dict(tenant.provider_status or {})
    return bool(tenant.azure_resource_group_name or ps.get("llm_account") or ps.get("key_vault_name"))


def _pooled_provider_status(ps: dict[str, Any]) -> dict[str, Any]:
    """Rewrite provider_status to the pooled/global shape, dropping the legacy
    per-tenant LLM / embedding / Key Vault keys while preserving everything else
    (exotel, qdrant_*, redis_*, stt/tts provider, twilio, etc.)."""
    new_ps = {k: v for k, v in ps.items() if not k.startswith(_LEGACY_PS_PREFIXES)}
    new_ps.update(
        {
            "llm_provider": "azure_openai_pool",
            "llm_status": "pooled",
            "embedding_provider": "openai_global",
            "embedding_status": "global",
            "stt_provider": ps.get("stt_provider", "sarvam"),
            "stt_status": "global",
            "tts_provider": ps.get("tts_provider", "sarvam"),
            "tts_status": "global",
        }
    )
    return new_ps


async def _collect_secret_names(tenant: TenantResources) -> list[str]:
    names: list[str] = []
    ps = dict(tenant.provider_status or {})
    for ref in (ps.get("key_vault_secret_refs") or {}).values():
        if isinstance(ref, str):
            names.append(ref)
        elif isinstance(ref, dict) and ref.get("secret_name"):
            names.append(ref["secret_name"])
    for ref in (tenant.secret_refs or {}).values():
        if isinstance(ref, dict) and ref.get("secret_name"):
            names.append(ref["secret_name"])
        elif isinstance(ref, str):
            names.append(ref)
    return sorted(set(n for n in names if n))


async def _teardown(tenant: TenantResources, *, live: bool) -> None:
    rg = tenant.azure_resource_group_name
    secret_names = await _collect_secret_names(tenant)
    _log(f"  tenant {tenant.tenant_id}:")
    _log(f"    resource_group : {rg or '(none)'}")
    _log(f"    kv_secrets     : {len(secret_names)} -> {secret_names or '(none)'}")
    _log(f"    keep intact    : qdrant={tenant.qdrant_collection_name!r} blob={tenant.blob_prefix!r} redis={tenant.redis_namespace!r}")
    if not live:
        _log("    [dry-run] would delete RG + KV secrets and rewrite provider_status to pooled/global")
        return

    if rg:
        try:
            ok = await AzureResourceGroupService.delete_resource_group(rg)
            _log(f"    azure_rg: {'delete queued' if ok else 'returned False'} for {rg}")
        except Exception as exc:
            _log(f"    azure_rg: FAILED for {rg}: {exc!r}")
    for name in secret_names:
        try:
            await AzureKeyVaultService.delete_secret(name)
        except Exception as exc:
            _log(f"    keyvault: skip {name}: {exc!r}")
    _log(f"    keyvault: attempted {len(secret_names)} secret deletions")


async def run(live: bool) -> None:
    async with AsyncSessionLocal() as db:
        res = await db.execute(select(TenantResources))
        all_tenants = list(res.scalars().all())
        targets = [t for t in all_tenants if _is_legacy(t)]

        _log("=" * 70)
        _log(f"{len(targets)} legacy tenant(s) to deprovision "
             f"({len(all_tenants) - len(targets)} already pooled / skipped)")
        _log("=" * 70)
        if not targets:
            _log("Nothing to do.")
            return

        if not live:
            for t in targets:
                await _teardown(t, live=False)
            _log("")
            _log("DRY RUN. Pass --i-understand to actually deprovision.")
            return

        _log("")
        _log("This will DELETE the per-tenant Azure Resource Groups + Key Vault")
        _log("secrets for the tenants above (IRREVERSIBLE at the cloud level) and")
        _log("rewrite their provider_status to pooled/global. Tenants stay alive.")
        _log(f"\nType exactly:  {CONFIRMATION_PHRASE}")
        if input("> ").strip() != CONFIRMATION_PHRASE:
            _log("Aborted (phrase did not match).")
            return

        succeeded, failed = 0, 0
        for t in targets:
            try:
                await _teardown(t, live=True)
                t.provider_status = _pooled_provider_status(dict(t.provider_status or {}))
                t.azure_resource_group_name = None
                t.azure_region = None
                t.key_vault_name = None
                t.secret_refs = {}
                flag_modified(t, "provider_status")
                flag_modified(t, "secret_refs")
                succeeded += 1
            except Exception as exc:
                _log(f"    FAILED to rewrite db row: {exc!r}")
                failed += 1
        await db.commit()

        _log("")
        _log("=" * 70)
        _log(f"SUMMARY: {succeeded} deprovisioned, {failed} failed")
        _log("=" * 70)
        if failed:
            sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--i-understand",
        action="store_true",
        help="Required to switch from dry-run to live deprovisioning.",
    )
    args = parser.parse_args()
    asyncio.run(run(live=args.i_understand))


if __name__ == "__main__":
    main()
