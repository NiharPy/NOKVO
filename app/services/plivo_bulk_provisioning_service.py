"""Automated bulk-calling DID provisioning for NOKVO APEX.

Replaces the manual SuperAdmin paste of a dedicated Plivo sub-account + number.
One click programmatically:
  1. targets the tenant's EXISTING Plivo sub-account — the one created at APEX
     account creation (provisioning Step 4) — so the DIDs are tied to it (no second
     sub-account is created),
  2. REUSES the tenant's onboarding compliance application — it lives on the MASTER
     account, so its ``compliance_application_id`` can buy numbers for that sub-account,
  3. buys India DIDs against it (assigned to the sub-account) until the pool holds
     exactly :data:`BULK_DID_COUNT` (5) — the onboarding DID already on the sub-account
     counts toward the 5, and
  4. writes ``provider_status["bulk_calling"]`` so the dialer's caller-pool rotation
     (``_resolve_caller_pool`` → ``PlivoService.list_account_numbers``) fans calls
     across the pool.

India DIDs need APPROVED KYC (async), so buying is DEFERRED: ``start_auto_provision``
creates the sub-account and tries an immediate buy; ``plivo_number_poller`` then tops
up to 5 once compliance approves. Idempotent + COST-SAFE: the sub-account is created
once and we only ever top UP to 5 owned DIDs (re-counting the live pool each run), so a
retry / partial buy can never over-buy. Everything is best-effort — any Plivo failure is
captured into ``bulk_calling["error"]`` and retried on the next poll.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from app.core.secret_crypto import decrypt_secret
from app.models.tenant_resources import TenantResources
from app.services.plivo_service import PlivoError, PlivoService

logger = logging.getLogger(__name__)

# The fixed size of the bulk caller-ID pool. NOT operator-configurable.
BULK_DID_COUNT = 5


class PlivoBulkProvisioningService:
    @staticmethod
    def _bulk_config(tenant_res: TenantResources) -> dict[str, Any]:
        return dict((tenant_res.provider_status or {}).get("bulk_calling") or {})

    @staticmethod
    def _save(tenant_res: TenantResources, cfg: dict) -> None:
        provider_status = dict(tenant_res.provider_status or {})
        provider_status["bulk_calling"] = cfg
        tenant_res.provider_status = provider_status
        try:
            flag_modified(tenant_res, "provider_status")
        except Exception:
            pass  # non-ORM stand-in (tests) — the reassignment above is enough

    @classmethod
    async def start_auto_provision(
        cls,
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        superadmin_id: str | None = None,
    ) -> dict[str, Any]:
        """Point the bulk DID pool at the tenant's EXISTING Plivo sub-account — the
        one created at APEX account creation (provisioning Step 4,
        ``provider_status["plivo"]["subaccount_auth_id"]``) — and attempt an immediate
        buy. We do NOT create a second sub-account: the 5 DIDs are bought on and tied
        to that account-creation sub-account (its token is encrypted with the same
        ``secret_crypto`` scheme ``bulk_calling_auth`` decrypts, so it's reused as-is).
        Idempotent — re-running tops the pool up to 5. Returns the live config."""
        cfg = cls._bulk_config(tenant_res)
        if not cfg.get("auth_id"):
            plivo_cfg = dict((tenant_res.provider_status or {}).get("plivo") or {})
            sub_auth_id = plivo_cfg.get("subaccount_auth_id")
            sub_token_enc = plivo_cfg.get("subaccount_auth_token_enc")
            if not sub_auth_id or not sub_token_enc:
                cfg["error"] = "no provisioned Plivo sub-account yet — finish account provisioning first"
                cls._save(tenant_res, cfg)
                await db.commit()
                return cfg
            cfg = {
                "auth_id": str(sub_auth_id),
                "auth_token_enc": sub_token_enc,  # reuse — same secret_crypto scheme
                "number": None,
                "numbers": [],
                "enabled": False,
                "status": "provisioning",
                "target_count": BULK_DID_COUNT,
                "auto_provisioned": True,
                "uses_account_subaccount": True,
                "granted_by_superadmin_id": superadmin_id,
                "granted_at": datetime.now(timezone.utc).isoformat(),
            }
            cls._save(tenant_res, cfg)
            await db.commit()
        # Try to buy now — succeeds only if onboarding compliance is already approved;
        # otherwise the poller keeps topping up until it is.
        return await cls._buy_pending_numbers(tenant_res, db)

    @classmethod
    async def grant_with_existing_subaccount(
        cls,
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        numbers: list[str] | None = None,
        superadmin_id: str | None = None,
    ) -> dict[str, Any]:
        """Enable bulk calling on the tenant's EXISTING account-creation sub-account
        WITHOUT buying anything — the operator rented the DIDs by hand in Plivo and
        just registers them here. Reuses the stored sub-account creds (no auth token
        to paste). ``numbers`` is optional: when given they're normalised and (best-
        effort) validated against what the sub-account owns; when blank we auto-detect
        every DID on the sub-account. The dialer's caller-pool rotation lists the
        sub-account LIVE, so the stored list is the record + primary caller-ID, not a
        hard gate. Idempotent — safe to re-run."""
        plivo_cfg = dict((tenant_res.provider_status or {}).get("plivo") or {})
        sub_auth_id = plivo_cfg.get("subaccount_auth_id")
        sub_token_enc = plivo_cfg.get("subaccount_auth_token_enc")
        if not sub_auth_id or not sub_token_enc:
            return {"error": "no provisioned Plivo sub-account yet — finish account provisioning first"}
        try:
            bulk_auth = (str(sub_auth_id), decrypt_secret(sub_token_enc))
        except Exception:
            return {"error": "could not decrypt the client's sub-account token"}

        # What the sub-account actually owns — the dialer rotates over this live.
        try:
            owned = await PlivoService.list_account_numbers(bulk_auth)
        except Exception:
            owned = []

        entered: list[str] = []
        for raw in (numbers or []):
            num = PlivoService.normalize_number(raw)
            if num and num not in entered:
                entered.append(num)

        if entered:
            if owned:
                pool = [n for n in entered if n in owned]
                if not pool:
                    return {
                        "error": "none of those numbers are on the client's sub-account — "
                        "check the account / numbers (a just-bought DID can take a moment to settle)"
                    }
            else:
                pool = entered  # listing unavailable → trust the operator's entry
        else:
            pool = owned  # auto-detect every DID on the sub-account

        if not pool:
            return {"error": "no DIDs found on the sub-account — rent them in Plivo first, or enter the numbers"}

        cfg = {
            "auth_id": str(sub_auth_id),
            "auth_token_enc": sub_token_enc,  # reuse — same secret_crypto scheme bulk_calling_auth decrypts
            "number": pool[0],
            "numbers": pool,
            "enabled": True,
            "status": "active",
            "target_count": len(pool),
            "auto_provisioned": False,
            "uses_account_subaccount": True,
            "granted_by_superadmin_id": superadmin_id,
            "granted_at": datetime.now(timezone.utc).isoformat(),
        }
        cls._save(tenant_res, cfg)
        await db.commit()
        logger.info(
            "PLIVO-BULK: tenant=%s enabled on existing sub-account, pool=%d",
            tenant_res.tenant_id, len(pool),
        )
        return cfg

    @classmethod
    async def _buy_pending_numbers(
        cls,
        tenant_res: TenantResources,
        db: AsyncSession,
    ) -> dict[str, Any]:
        """Top the bulk sub-account's DID pool up to :data:`BULK_DID_COUNT`.

        COST-SAFE: re-counts the live pool each run and only buys the remainder,
        gated on the tenant's onboarding compliance being APPROVED. Best-effort +
        idempotent — safe to call repeatedly (poller)."""
        cfg = cls._bulk_config(tenant_res)
        bulk_auth_id = cfg.get("auth_id")
        enc = cfg.get("auth_token_enc")
        if not bulk_auth_id or not enc:
            return cfg  # auto-provision never started
        if cfg.get("status") == "active":
            return cfg  # pool already complete

        # The compliance application filed during onboarding (master-level, reusable
        # to buy numbers for any sub-account).
        plivo_cfg = dict((tenant_res.provider_status or {}).get("plivo") or {})
        compliance_app_id = (dict(plivo_cfg.get("compliance") or {}).get("application_id")) or None
        if not compliance_app_id:
            cfg["error"] = "no onboarding compliance application — finish onboarding first"
            cls._save(tenant_res, cfg)
            await db.commit()
            return cfg

        # Gate on compliance APPROVAL — India DIDs aren't buyable pre-approval.
        from app.services.plivo_number_poller import _is_approved

        try:
            auth = PlivoService._master_auth()
            base = PlivoService._base(auth[0])
            appdata = await PlivoService._request(
                "GET", f"{base}/ComplianceApplication/{compliance_app_id}/", auth=auth
            )
            status = str(appdata.get("status") or appdata.get("application_status") or "")
        except PlivoError:
            status = ""
        if not _is_approved(status):
            cfg["status"] = "provisioning"
            cfg.pop("error", None)
            cls._save(tenant_res, cfg)
            await db.commit()
            return cfg

        try:
            bulk_auth = (str(bulk_auth_id), decrypt_secret(enc))
        except Exception:
            cfg["error"] = "could not decrypt bulk auth token"
            cls._save(tenant_res, cfg)
            await db.commit()
            return cfg

        # Re-count what the sub-account already owns (idempotency + cost safety) and
        # only buy the remainder up to the fixed 5.
        owned = await PlivoService.list_account_numbers(bulk_auth)
        buy_error: str | None = None
        for _ in range(max(0, BULK_DID_COUNT - len(owned))):
            try:
                rented = await PlivoService.rent_number(
                    sub_auth_id=str(bulk_auth_id),
                    compliance_application_id=compliance_app_id,
                )
            except PlivoError as exc:
                buy_error = str(exc)[:300]
                break  # stop; the poller retries on the next tick
            num = PlivoService.normalize_number(rented.get("number"))
            if num and num not in owned:
                owned.append(num)

        cfg["numbers"] = owned
        if owned:
            cfg["number"] = owned[0]
            cfg["enabled"] = True
        cfg["status"] = "active" if len(owned) >= BULK_DID_COUNT else "provisioning"
        if buy_error and len(owned) < BULK_DID_COUNT:
            cfg["error"] = buy_error
        else:
            cfg.pop("error", None)
        cls._save(tenant_res, cfg)
        await db.commit()
        logger.info(
            "PLIVO-BULK: tenant=%s pool=%d/%d status=%s",
            tenant_res.tenant_id, len(owned), BULK_DID_COUNT, cfg["status"],
        )
        return cfg
