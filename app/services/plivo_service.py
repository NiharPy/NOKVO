"""Plivo telephony service — the sole telephony provider.

Per-tenant isolation uses Plivo **subaccounts**: each tenant gets its own
subaccount, an Application whose ``answer_url`` points at our inbound webhook
(automatic webhook provisioning — no portal config), and a DID assigned to that
Application + subaccount. Inbound works by call-forwarding: the tenant forwards
their own number (at their carrier) to the assigned Plivo DID.

REST is called directly via httpx (no SDK dependency, easily mockable). Auth is
HTTP basic with the MASTER account (PLIVO_AUTH_ID/TOKEN); subaccount tokens are
stored encrypted in ``provider_status.plivo``.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.core.config import settings
from app.core.secret_crypto import decrypt_secret, encrypt_secret
from app.models.tenant_resources import TenantResources


class PlivoError(RuntimeError):
    """Raised on a Plivo API failure."""


class PlivoService:
    # ── low-level REST ────────────────────────────────────────────────────────
    @staticmethod
    def _master_auth() -> tuple[str, str]:
        if not settings.PLIVO_AUTH_ID or not settings.PLIVO_AUTH_TOKEN:
            raise PlivoError("PLIVO_AUTH_ID and PLIVO_AUTH_TOKEN are required.")
        return settings.PLIVO_AUTH_ID, settings.PLIVO_AUTH_TOKEN

    @staticmethod
    def _base(auth_id: str) -> str:
        return f"{settings.PLIVO_API_BASE.rstrip('/')}/Account/{auth_id}"

    @staticmethod
    async def _request(method: str, url: str, *, auth: tuple[str, str], json_body: dict | None = None) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=30.0, auth=auth) as client:
            resp = await client.request(method, url, json=json_body)
        if resp.status_code >= 400:
            raise PlivoError(f"Plivo {method} {url.split('/Account/')[-1]} failed ({resp.status_code}): {resp.text[:300]}")
        try:
            return resp.json()
        except Exception:
            return {"raw": resp.text}

    # ── subaccounts ─────────────────────────────────────────────────────────────
    @classmethod
    async def create_subaccount(cls, name: str) -> dict[str, str]:
        """Create a Plivo subaccount. Returns {auth_id, auth_token}."""
        auth = cls._master_auth()
        data = await cls._request(
            "POST", f"{cls._base(auth[0])}/Subaccount/",
            auth=auth, json_body={"name": name[:64], "enabled": True},
        )
        sub_auth_id = data.get("auth_id") or data.get("api_id")
        sub_auth_token = data.get("auth_token")
        if not sub_auth_id or not sub_auth_token:
            raise PlivoError(f"Subaccount create returned no creds: {data}")
        return {"auth_id": str(sub_auth_id), "auth_token": str(sub_auth_token)}

    @classmethod
    async def delete_subaccount(cls, sub_auth_id: str) -> None:
        auth = cls._master_auth()
        await cls._request("DELETE", f"{cls._base(auth[0])}/Subaccount/{sub_auth_id}/", auth=auth)

    # ── applications (the inbound webhook) ───────────────────────────────────────
    @classmethod
    async def create_application(cls, *, app_name: str, answer_url: str, hangup_url: str | None = None) -> str:
        """Create an Application with answer_url → our inbound webhook. Returns app_id."""
        auth = cls._master_auth()
        body: dict[str, Any] = {"app_name": app_name[:64], "answer_url": answer_url, "answer_method": "POST"}
        if hangup_url:
            body["hangup_url"] = hangup_url
            body["hangup_method"] = "POST"
        data = await cls._request("POST", f"{cls._base(auth[0])}/Application/", auth=auth, json_body=body)
        app_id = data.get("app_id")
        if not app_id:
            raise PlivoError(f"Application create returned no app_id: {data}")
        return str(app_id)

    @classmethod
    async def update_application(
        cls, app_id: str, *, answer_url: str | None = None, hangup_url: str | None = None
    ) -> None:
        """Re-point an existing Application's webhooks. The answer_url is set
        once at provisioning; when the public base URL changes (domain move,
        tunnel rotation) every tenant's Application goes stale and inbound
        calls stop connecting — this is the repair path."""
        auth = cls._master_auth()
        body: dict[str, Any] = {}
        if answer_url:
            body["answer_url"] = answer_url
            body["answer_method"] = "POST"
        if hangup_url:
            body["hangup_url"] = hangup_url
            body["hangup_method"] = "POST"
        if not body:
            return
        await cls._request("POST", f"{cls._base(auth[0])}/Application/{app_id}/", auth=auth, json_body=body)

    @classmethod
    async def delete_application(cls, app_id: str) -> None:
        auth = cls._master_auth()
        await cls._request("DELETE", f"{cls._base(auth[0])}/Application/{app_id}/", auth=auth)

    # ── webhook re-sync ──────────────────────────────────────────────────────────
    @staticmethod
    def expected_answer_url(link_id: str, base: str) -> str:
        """The answer_url a tenant's Application SHOULD carry for ``base``."""
        return f"{base.rstrip('/')}/api/nokvo-one/agents/plivo/voice/{link_id}"

    @classmethod
    def needs_webhook_resync(cls, plivo_cfg: dict | None, base: str) -> bool:
        """True when the stored Application answer_url doesn't match what the
        current public base URL implies. False when there's nothing to sync
        (no application / no link_id / no base)."""
        cfg = plivo_cfg or {}
        if not cfg.get("application_id") or not cfg.get("link_id") or not base:
            return False
        expected = cls.expected_answer_url(str(cfg["link_id"]), base)
        return (cfg.get("answer_url") or "") != expected

    @classmethod
    async def resync_tenant_webhook(cls, tenant_res: TenantResources, db, *, base: str) -> dict:
        """Update one tenant's Plivo Application answer_url to match ``base``
        and persist the new URL into provider_status.plivo."""
        from sqlalchemy.orm.attributes import flag_modified

        cfg = cls._plivo_config(tenant_res)
        if not cfg.get("application_id") or not cfg.get("link_id"):
            return {"tenant_id": tenant_res.tenant_id, "updated": False, "reason": "no_application"}
        expected = cls.expected_answer_url(str(cfg["link_id"]), base)
        if (cfg.get("answer_url") or "") == expected:
            return {"tenant_id": tenant_res.tenant_id, "updated": False, "reason": "in_sync"}
        await cls.update_application(str(cfg["application_id"]), answer_url=expected)
        provider_status = dict(tenant_res.provider_status or {})
        plivo_cfg = dict(provider_status.get("plivo") or {})
        plivo_cfg["answer_url"] = expected
        provider_status["plivo"] = plivo_cfg
        tenant_res.provider_status = provider_status
        try:
            flag_modified(tenant_res, "provider_status")
        except Exception:
            pass  # non-ORM stand-ins (tests) have no instrumentation
        db.add(tenant_res)
        await db.commit()
        return {"tenant_id": tenant_res.tenant_id, "updated": True, "answer_url": expected}

    @classmethod
    async def startup_webhook_sync_check(cls) -> None:
        """Startup pass: count tenants with stale Application answer_urls and
        log loudly. Mutates Plivo only when PLIVO_WEBHOOK_AUTOSYNC is enabled
        (default off — tunnel rotation + multi-instance makes silent
        auto-mutation risky; the superadmin resync endpoint is the primary
        repair path). Best-effort: any failure is logged, never fatal."""
        import logging

        log = logging.getLogger(__name__)
        try:
            from sqlalchemy import select

            from app.db.session import AsyncSessionLocal
            from app.services.public_url import public_base_url

            base = public_base_url()
            if not base or "localhost" in base:
                return
            async with AsyncSessionLocal() as db:
                rows = (await db.execute(select(TenantResources))).scalars().all()
                stale = [
                    tr for tr in rows
                    if cls.needs_webhook_resync((tr.provider_status or {}).get("plivo"), base)
                ]
                if not stale:
                    return
                if not settings.PLIVO_WEBHOOK_AUTOSYNC:
                    log.error(
                        "PLIVO-WEBHOOKS: %d tenant(s) have a STALE Plivo Application answer_url "
                        "(base is now %s) — inbound calls for them will NOT connect. Run "
                        "POST /superadmin/tenants/plivo/resync-webhooks (or set "
                        "PLIVO_WEBHOOK_AUTOSYNC=true).",
                        len(stale), base,
                    )
                    return
                for tr in stale:
                    try:
                        result = await cls.resync_tenant_webhook(tr, db, base=base)
                        log.warning("PLIVO-WEBHOOKS: autosync %s", result)
                    except Exception:
                        log.exception(
                            "PLIVO-WEBHOOKS: autosync failed for tenant %s", tr.tenant_id
                        )
        except Exception:
            log.exception("PLIVO-WEBHOOKS: startup sync check failed")

    # ── numbers (DIDs) ───────────────────────────────────────────────────────────
    @classmethod
    async def rent_number(cls, *, country: str | None = None, app_id: str | None = None, sub_auth_id: str | None = None) -> dict[str, Any]:
        """Search + buy a DID, then assign it to the Application + subaccount.

        India DIDs need KYC/regulatory approval and may not be instantly buyable —
        the caller treats a PlivoError / empty result as ``pending_verification``
        rather than failing provisioning.
        """
        auth = cls._master_auth()
        iso = (country or settings.PLIVO_NUMBER_COUNTRY or "IN").upper()
        found = await cls._request("GET", f"{cls._base(auth[0])}/PhoneNumber/?country_iso={iso}&services=voice", auth=auth)
        objs = found.get("objects") or []
        if not objs:
            raise PlivoError(f"No rentable {iso} DIDs available (likely KYC/regulatory — provision a pool).")
        number = str(objs[0].get("number") or "")
        if not number:
            raise PlivoError(f"DID search returned no usable number: {objs[0]}")
        buy_body: dict[str, Any] = {}
        if app_id:
            buy_body["app_id"] = app_id
        await cls._request("POST", f"{cls._base(auth[0])}/PhoneNumber/{number}/", auth=auth, json_body=buy_body or None)
        if app_id or sub_auth_id:
            await cls.assign_number(number, app_id=app_id, sub_auth_id=sub_auth_id)
        return {"number": number, "status": "active"}

    @classmethod
    async def assign_number(cls, number: str, *, app_id: str | None = None, sub_auth_id: str | None = None) -> None:
        """Bind a rented DID to an Application and/or move it to a subaccount."""
        auth = cls._master_auth()
        body: dict[str, Any] = {}
        if app_id:
            body["app_id"] = app_id
        if sub_auth_id:
            body["subaccount"] = sub_auth_id
        if body:
            await cls._request("POST", f"{cls._base(auth[0])}/Number/{number}/", auth=auth, json_body=body)

    @classmethod
    async def release_number(cls, number: str) -> None:
        auth = cls._master_auth()
        await cls._request("DELETE", f"{cls._base(auth[0])}/Number/{number}/", auth=auth)

    # ── outbound ─────────────────────────────────────────────────────────────────
    @classmethod
    async def initiate_outbound_call(
        cls,
        tenant_res: TenantResources,
        *,
        to_number: str,
        answer_url: str,
        status_callback: str | None = None,
    ) -> dict[str, Any]:
        """Place an outbound call from the tenant's assigned DID. answer_url returns
        the <Stream> XML that bridges audio to the agent."""
        cfg = cls._plivo_config(tenant_res)
        from_number = cfg.get("number") or tenant_res.twilio_phone_number
        if not from_number:
            raise PlivoError("Tenant has no assigned Plivo DID for outbound caller ID.")
        # Calls are placed on the tenant's subaccount when available, else master.
        sub_auth_id = cfg.get("subaccount_auth_id")
        sub_token = cls._subaccount_token(cfg)
        auth = (sub_auth_id, sub_token) if (sub_auth_id and sub_token) else cls._master_auth()
        body: dict[str, Any] = {
            "from": from_number,
            "to": to_number,
            "answer_url": answer_url,
            "answer_method": "POST",
        }
        if status_callback:
            body["hangup_url"] = status_callback
            body["hangup_method"] = "POST"
        return await cls._request("POST", f"{cls._base(auth[0])}/Call/", auth=auth, json_body=body)

    # ── config helpers ───────────────────────────────────────────────────────────
    @staticmethod
    def _plivo_config(tenant_res: TenantResources) -> dict:
        return dict((tenant_res.provider_status or {}).get("plivo") or {})

    @staticmethod
    def _subaccount_token(cfg: dict) -> str | None:
        enc = cfg.get("subaccount_auth_token_enc")
        if not enc:
            return None
        try:
            return decrypt_secret(enc)
        except Exception:
            return None

    @staticmethod
    def encrypt_token(token: str) -> str:
        return encrypt_secret(token)

    # ── legacy org-auth phone-link (Exotel-compatible shape) ─────────────────────
    @staticmethod
    def legacy_phone_link_response(tenant_res: TenantResources) -> dict:
        """Exotel-shaped link response for the legacy /api/org-auth endpoints."""
        cfg = PlivoService._plivo_config(tenant_res)
        link = dict((tenant_res.provider_status or {}).get("agent_phone_link") or {})
        number = cfg.get("number") or tenant_res.twilio_phone_number
        return {
            "status": link.get("status") or ("linked" if cfg.get("application_id") else "not_linked"),
            "phone_number": number,
            "link_id": link.get("link_id"),
            "voice_url": cfg.get("answer_url"),
            "incoming_phone_number_sid": None,
            "linked_at": link.get("linked_at"),
            "unlinked_at": link.get("unlinked_at"),
            "latency_target_ms": 800,
        }

    @staticmethod
    async def link_agent_phone_number(tenant_res, db, *, phone_number: str, public_base_url: str | None = None) -> dict:
        """The DID + Application are auto-provisioned; 'linking' records the tenant's
        own number to forward and marks the link active."""
        from sqlalchemy.orm.attributes import flag_modified

        ps = dict(tenant_res.provider_status or {})
        cfg = dict(ps.get("plivo") or {})
        cfg["forward_from_number"] = phone_number
        ps["plivo"] = cfg
        link = dict(ps.get("agent_phone_link") or {})
        link.update({"status": "linked", "provider": "plivo", "linked_at": datetime.now(timezone.utc).isoformat()})
        ps["agent_phone_link"] = link
        tenant_res.provider_status = ps
        flag_modified(tenant_res, "provider_status")
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return PlivoService.legacy_phone_link_response(tenant_res)

    @staticmethod
    async def unlink_agent_phone_number(tenant_res, db) -> dict:
        from sqlalchemy.orm.attributes import flag_modified

        ps = dict(tenant_res.provider_status or {})
        link = dict(ps.get("agent_phone_link") or {})
        link.update({"status": "not_linked", "unlinked_at": datetime.now(timezone.utc).isoformat()})
        ps["agent_phone_link"] = link
        tenant_res.provider_status = ps
        flag_modified(tenant_res, "provider_status")
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return PlivoService.legacy_phone_link_response(tenant_res)

    @staticmethod
    def phone_link_response(tenant_res: TenantResources) -> dict:
        cfg = PlivoService._plivo_config(tenant_res)
        link = dict((tenant_res.provider_status or {}).get("agent_phone_link") or {})
        number = cfg.get("number") or tenant_res.twilio_phone_number
        number_status = cfg.get("number_status") or ("active" if number else "pending_verification")
        return {
            "provider": "plivo",
            "status": link.get("status") or ("linked" if cfg.get("application_id") else "not_linked"),
            "link_id": link.get("link_id"),
            "plivo_number": number,                       # forward your number HERE
            "number_status": number_status,               # active | pending_verification
            "forward_from_number": cfg.get("forward_from_number"),
            "answer_url": cfg.get("answer_url"),
            "latency_target_ms": 800,
        }

    # ── WhatsApp sender connect (per-tenant WABA sender) ─────────────────────────
    # Voice DIDs are auto-provisioned; a WhatsApp sender is NOT — the number must
    # first be onboarded to a WhatsApp Business Account in Plivo/Meta. These let a
    # tenant connect that already-onboarded number so WhatsAppService._resolve_sender
    # picks it up (auth still resolves to the tenant's subaccount, exactly like voice).
    @staticmethod
    def _normalize_whatsapp_number(raw: str | None) -> str | None:
        """Trim formatting to a bare sender. Keeps a leading '+' when given;
        returns None when there aren't enough digits to be a phone number."""
        s = (raw or "").strip()
        if not s:
            return None
        plus = s.startswith("+")
        digits = "".join(ch for ch in s if ch.isdigit())
        if len(digits) < 8:
            return None
        return ("+" + digits) if plus else digits

    @staticmethod
    def whatsapp_link_response(tenant_res: TenantResources) -> dict:
        """Connection status for the tenant's WhatsApp sender. ``ready_to_send``
        is the honest end-to-end gate: a number is connected AND the feature
        switch (PLIVO_WHATSAPP_ENABLED) is on. Per-project brochure/location
        templates must still be Meta-approved before anything delivers."""
        cfg = PlivoService._plivo_config(tenant_res)
        number = cfg.get("whatsapp_number")
        connected = bool(number)
        enabled = bool(settings.PLIVO_WHATSAPP_ENABLED)
        return {
            "provider": "plivo",
            "whatsapp_number": number,
            "status": cfg.get("whatsapp_status") or ("connected" if connected else "not_connected"),
            "connected": connected,
            "feature_enabled": enabled,          # global master switch
            "ready_to_send": connected and enabled,
            "uses_subaccount": bool(cfg.get("subaccount_auth_id")),
            "connected_at": cfg.get("whatsapp_connected_at"),
        }

    @classmethod
    async def connect_whatsapp_number(cls, tenant_res, db, *, whatsapp_number: str) -> dict:
        """Connect (or update) the tenant's WhatsApp Business sender number.

        Records WHICH number our sends originate from; authenticates as the
        tenant's subaccount when present (else master), mirroring voice. The
        number must already be WABA-onboarded in Plivo/Meta — this does not
        provision it. Idempotent."""
        from sqlalchemy.orm.attributes import flag_modified

        number = cls._normalize_whatsapp_number(whatsapp_number)
        if not number:
            raise PlivoError("A valid WhatsApp sender number is required.")
        ps = dict(tenant_res.provider_status or {})
        cfg = dict(ps.get("plivo") or {})
        cfg["whatsapp_number"] = number
        cfg["whatsapp_status"] = "connected"
        cfg["whatsapp_connected_at"] = datetime.now(timezone.utc).isoformat()
        ps["plivo"] = cfg
        tenant_res.provider_status = ps
        try:
            flag_modified(tenant_res, "provider_status")
        except Exception:
            pass  # non-ORM stand-ins (tests) have no instrumentation
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return cls.whatsapp_link_response(tenant_res)

    @classmethod
    async def disconnect_whatsapp_number(cls, tenant_res, db) -> dict:
        """Disconnect the tenant's WhatsApp sender — sends then skip
        (``no_whatsapp_sender``); never borrows another tenant's sender."""
        from sqlalchemy.orm.attributes import flag_modified

        ps = dict(tenant_res.provider_status or {})
        cfg = dict(ps.get("plivo") or {})
        cfg.pop("whatsapp_number", None)
        cfg["whatsapp_status"] = "not_connected"
        cfg["whatsapp_disconnected_at"] = datetime.now(timezone.utc).isoformat()
        ps["plivo"] = cfg
        tenant_res.provider_status = ps
        try:
            flag_modified(tenant_res, "provider_status")
        except Exception:
            pass
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return cls.whatsapp_link_response(tenant_res)
