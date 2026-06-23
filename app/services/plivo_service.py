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
    def normalize_number(num: str | None) -> str:
        """Plivo's Call API rejects formatted numbers ('+91 22 6423 2977'). Strip
        to bare digits (the convention DIDs are stored in elsewhere, e.g.
        '918031321315'). Returns '' for falsy input."""
        return "".join(ch for ch in str(num or "") if ch.isdigit())

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

    @staticmethod
    async def _request_multipart(
        url: str, *, auth: tuple[str, str], data: dict | None = None, files: dict | None = None
    ) -> dict[str, Any]:
        """POST multipart/form-data — used for Plivo compliance document uploads
        (the file is sent as a part, not JSON)."""
        async with httpx.AsyncClient(timeout=60.0, auth=auth) as client:
            resp = await client.post(url, data=data or {}, files=files or {})
        if resp.status_code >= 400:
            raise PlivoError(
                f"Plivo POST {url.split('/Account/')[-1]} failed ({resp.status_code}): {resp.text[:300]}"
            )
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
    async def rent_number(cls, *, country: str | None = None, app_id: str | None = None, sub_auth_id: str | None = None, compliance_application_id: str | None = None) -> dict[str, Any]:
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
        # Regulated markets (India) won't sell a DID without an APPROVED business
        # compliance application — pass its id so Plivo links the purchase.
        if compliance_application_id:
            buy_body["compliance_application_id"] = compliance_application_id
        # Always send a JSON body (even ``{}``) — Plivo's buy endpoint requires an
        # application/json POST and 400s ("use 'application/json'…") on a bodyless
        # request, which is why bare ``buy_body or None`` failed.
        await cls._request("POST", f"{cls._base(auth[0])}/PhoneNumber/{number}/", auth=auth, json_body=buy_body)
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

    @classmethod
    async def set_tenant_number(
        cls,
        tenant_res: TenantResources,
        db,
        *,
        number: str,
        reassign: bool = True,
        base: str | None = None,
    ) -> dict[str, Any]:
        """Point a tenant at a different DID (operator override).

        Persists ``provider_status.plivo.number`` (+ ``twilio_phone_number``) and,
        when ``reassign`` and the tenant has a Plivo Application, best-effort binds
        the DID to that Application + subaccount and re-syncs the answer webhook so
        inbound calls route. Telephony API failures are reported, never fatal — the
        stored number still updates so outbound caller-ID is correct.
        """
        from sqlalchemy.orm.attributes import flag_modified

        number = (number or "").strip()
        if not number:
            raise PlivoError("A phone number is required.")

        cfg = cls._plivo_config(tenant_res)
        previous = cfg.get("number") or tenant_res.twilio_phone_number
        app_id = cfg.get("application_id")
        sub_auth_id = cfg.get("subaccount_auth_id")

        result: dict[str, Any] = {"number": number, "previous": previous, "assigned": False, "webhook": None}

        if reassign and app_id:
            try:
                await cls.assign_number(number, app_id=str(app_id), sub_auth_id=sub_auth_id)
                result["assigned"] = True
            except Exception as exc:  # noqa: BLE001
                result["assign_error"] = str(exc)

        provider_status = dict(tenant_res.provider_status or {})
        plivo_cfg = dict(provider_status.get("plivo") or {})
        plivo_cfg["number"] = number
        provider_status["plivo"] = plivo_cfg
        tenant_res.provider_status = provider_status
        tenant_res.twilio_phone_number = number
        try:
            flag_modified(tenant_res, "provider_status")
        except Exception:
            pass
        db.add(tenant_res)
        await db.commit()

        if reassign and base and app_id:
            try:
                result["webhook"] = await cls.resync_tenant_webhook(tenant_res, db, base=base)
            except Exception as exc:  # noqa: BLE001
                result["webhook"] = {"updated": False, "error": str(exc)}

        return result

    # ── outbound ─────────────────────────────────────────────────────────────────
    @classmethod
    def outbound_caller_id(cls, tenant_res: TenantResources) -> str | None:
        """The tenant's allotted outbound caller-ID number (rented Plivo DID),
        or None when telephony isn't provisioned yet."""
        cfg = cls._plivo_config(tenant_res)
        return cfg.get("number") or tenant_res.twilio_phone_number or None

    # ── bulk CSV calling (dedicated, operator-provisioned telephony) ─────────────
    # Bulk calling does NOT reuse the tenant's inbound DID/subaccount — an operator
    # provisions a separate Plivo number + auth from the SuperAdmin console. It's
    # stored under ``provider_status["bulk_calling"]`` so it can't clash with the
    # tenant's own ``plivo`` config:  {auth_id, auth_token_enc, number, enabled}.
    @staticmethod
    def bulk_calling_config(tenant_res: TenantResources) -> dict:
        return dict((tenant_res.provider_status or {}).get("bulk_calling") or {})

    @classmethod
    def bulk_calling_enabled(cls, tenant_res: TenantResources) -> bool:
        cfg = cls.bulk_calling_config(tenant_res)
        return bool(cfg.get("enabled") and cfg.get("number") and cfg.get("auth_id") and cfg.get("auth_token_enc"))

    @classmethod
    def bulk_calling_caller_id(cls, tenant_res: TenantResources) -> str | None:
        # Normalize at the source: operators hand-enter the dedicated number in the
        # SuperAdmin grant (often formatted, e.g. "+91 22 6423 2977"). Plivo is fed
        # bare digits at dial time regardless, but normalizing here also keeps the
        # value we store on ``campaign.from_number`` clean for the UI.
        return cls.normalize_number(cls.bulk_calling_config(tenant_res).get("number")) or None

    @classmethod
    async def validate_bulk_telephony(cls, auth_id: str, auth_token: str, number: str) -> str | None:
        """Read-only pre-flight for a SuperAdmin bulk-calling grant. Returns an
        error string when the dedicated telephony can't place calls, else None.

        Catches the two silent-failure modes seen in practice: bad credentials,
        and a ``from`` number that isn't actually rented on that Plivo account
        (Plivo 400s every /Call with an unowned caller ID, so the campaign just
        never dials). Validating here turns "silently not calling" into an
        actionable error before the feature is enabled."""
        num = cls.normalize_number(number)
        if not num:
            return "Enter a valid phone number (digits only)."
        auth = (auth_id, auth_token)
        base = cls._base(auth_id)
        try:
            await cls._request("GET", f"{base}/", auth=auth)
        except PlivoError:
            return "Plivo rejected these credentials — check the Auth ID and Auth Token."
        try:
            await cls._request("GET", f"{base}/Number/{num}/", auth=auth)
        except PlivoError:
            return (
                f"The number {num} isn't rented on that Plivo account, so it can't "
                "place calls. Rent/assign the DID to this account in Plivo first, "
                "then grant again."
            )
        return None

    @classmethod
    def bulk_calling_auth(cls, tenant_res: TenantResources) -> tuple[str, str] | None:
        """(auth_id, decrypted auth_token) for the dedicated bulk account, or None."""
        cfg = cls.bulk_calling_config(tenant_res)
        auth_id = cfg.get("auth_id")
        enc = cfg.get("auth_token_enc")
        if not auth_id or not enc:
            return None
        try:
            return (str(auth_id), decrypt_secret(enc))
        except Exception:
            return None

    @classmethod
    async def list_account_numbers(cls, auth: tuple[str, str]) -> list[str]:
        """Every voice-capable DID rented on the (sub)account identified by ``auth``,
        in bare-digit form. Reads ``GET /Number/`` — the owned-number list, same
        ``objects`` envelope as the DID search. Used to rotate a campaign's outbound
        caller ID across all of a bulk sub-account's numbers instead of hammering
        one. Returns ``[]`` on any error so callers fall back to a single configured
        caller ID. Pools are tiny, so the first page (limit 20) suffices."""
        try:
            resp = await cls._request(
                "GET", f"{cls._base(auth[0])}/Number/?limit=20", auth=auth
            )
        except Exception:
            return []
        out: list[str] = []
        seen: set[str] = set()
        for obj in (resp.get("objects") or []):
            if not obj.get("voice_enabled", True):
                continue  # SMS-only DIDs can't originate voice calls
            num = cls.normalize_number(obj.get("number"))
            if num and num not in seen:
                seen.add(num)
                out.append(num)
        return out

    @classmethod
    async def initiate_outbound_call(
        cls,
        tenant_res: TenantResources,
        *,
        to_number: str,
        answer_url: str,
        status_callback: str | None = None,
        from_number: str | None = None,
        auth_override: tuple[str, str] | None = None,
    ) -> dict[str, Any]:
        """Place an outbound call from the tenant's assigned DID. answer_url returns
        the <Stream> XML that bridges audio to the agent. ``from_number`` overrides
        the caller ID (callers pass the campaign's resolved allotted number); when
        omitted it falls back to the tenant's configured DID.

        ``auth_override`` (auth_id, auth_token) places the call on a DIFFERENT
        Plivo (sub)account than the tenant's — used by Bulk CSV Calling, whose
        dedicated number + credentials are provisioned by an operator and live
        under ``provider_status["bulk_calling"]`` rather than the tenant's own
        ``plivo`` config."""
        cfg = cls._plivo_config(tenant_res)
        from_number = from_number or cls.outbound_caller_id(tenant_res)
        if not from_number:
            raise PlivoError("Tenant has no assigned Plivo DID for outbound caller ID.")
        # Bulk calling places on its dedicated account; otherwise the tenant's
        # subaccount when available, else master.
        if auth_override and auth_override[0] and auth_override[1]:
            auth = auth_override
        else:
            sub_auth_id = cfg.get("subaccount_auth_id")
            sub_token = cls._subaccount_token(cfg)
            auth = (sub_auth_id, sub_token) if (sub_auth_id and sub_token) else cls._master_auth()
        # Plivo rejects formatted numbers — normalize to bare digits. An operator
        # may have entered the bulk DID as "+91 22 6423 2977"; without this the
        # /Call POST 400s and the contact silently fails to dial.
        body: dict[str, Any] = {
            "from": cls.normalize_number(from_number),
            "to": cls.normalize_number(to_number),
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
        onboarding = dict(cfg.get("whatsapp_onboarding") or {})
        if connected:
            onboarding_step = "connected"
        elif onboarding.get("step") == "requested":
            onboarding_step = "setting_up"
        else:
            onboarding_step = "not_requested"
        next_step = {
            "not_requested": "request",
            "setting_up": "awaiting_setup",
            "connected": "ready",
        }[onboarding_step]
        return {
            "provider": "plivo",
            "whatsapp_number": number,
            "status": cfg.get("whatsapp_status") or ("connected" if connected else "not_connected"),
            "connected": connected,
            "feature_enabled": enabled,          # global master switch
            "ready_to_send": connected and enabled,
            "uses_subaccount": bool(cfg.get("subaccount_auth_id")),
            "connected_at": cfg.get("whatsapp_connected_at"),
            # Concierge onboarding state — the client UI shows
            # not_requested → setting_up → connected from these (never sees Plivo).
            "onboarding_step": onboarding_step,
            "next_step": next_step,
            "onboarding": {
                "business_name": onboarding.get("business_name"),
                "contact_number": onboarding.get("contact_number"),
                "display_name": onboarding.get("display_name"),
                "requested_at": onboarding.get("requested_at"),
            } if onboarding else None,
        }

    @classmethod
    async def connect_whatsapp_number(
        cls, tenant_res, db, *, whatsapp_number: str,
        waba_id: str | None = None, phone_number_id: str | None = None,
    ) -> dict:
        """Connect (or update) the tenant's WhatsApp Business sender number.

        Records WHICH number our sends originate from; authenticates as the
        tenant's subaccount when present (else master), mirroring voice. The
        number must already be WABA-onboarded in Plivo/Meta — this does not
        provision it. In the concierge model this is the operator's fulfilment
        call (after they onboard the number in the Plivo Console). Idempotent."""
        from sqlalchemy.orm.attributes import flag_modified

        number = cls._normalize_whatsapp_number(whatsapp_number)
        if not number:
            raise PlivoError("A valid WhatsApp sender number is required.")
        ps = dict(tenant_res.provider_status or {})
        cfg = dict(ps.get("plivo") or {})
        cfg["whatsapp_number"] = number
        cfg["whatsapp_status"] = "connected"
        cfg["whatsapp_connected_at"] = datetime.now(timezone.utc).isoformat()
        if waba_id:
            cfg["waba_id"] = str(waba_id).strip()
        if phone_number_id:
            cfg["phone_number_id"] = str(phone_number_id).strip()
        # If this fulfils a concierge request, mark it done so it clears the queue.
        ob = dict(cfg.get("whatsapp_onboarding") or {})
        if ob:
            ob["step"] = "connected"
            ob["connected_at"] = cfg["whatsapp_connected_at"]
            cfg["whatsapp_onboarding"] = ob
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
    async def request_whatsapp_setup(
        cls, tenant_res, db, *, business_name: str, contact_number: str,
        display_name: str | None = None, requested_by: str | None = None,
    ) -> dict:
        """Concierge onboarding: the client requests WhatsApp and never sees Plivo.

        Records the request on the tenant (step ``requested``) and alerts ops by
        email. The operator then onboards the number in the Plivo Console and
        records the connected sender via ``connect_whatsapp_number`` (which flips
        the step to ``connected``). Best-effort email — a mail failure must not
        fail the client's request."""
        from sqlalchemy.orm.attributes import flag_modified

        biz = (business_name or "").strip()
        contact = cls._normalize_whatsapp_number(contact_number)
        if not biz:
            raise PlivoError("Business name is required.")
        if not contact:
            raise PlivoError("A valid WhatsApp number is required.")
        ps = dict(tenant_res.provider_status or {})
        cfg = dict(ps.get("plivo") or {})
        onboarding = {
            "step": "requested",
            "business_name": biz,
            "contact_number": contact,
            "display_name": (display_name or "").strip() or None,
            "requested_at": datetime.now(timezone.utc).isoformat(),
            "requested_by": (requested_by or "").strip() or None,
        }
        cfg["whatsapp_onboarding"] = onboarding
        cfg["whatsapp_status"] = "setting_up"
        ps["plivo"] = cfg
        tenant_res.provider_status = ps
        try:
            flag_modified(tenant_res, "provider_status")
        except Exception:
            pass
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        await cls._notify_ops_whatsapp_request(tenant_res, onboarding)
        return cls.whatsapp_link_response(tenant_res)

    @staticmethod
    async def _notify_ops_whatsapp_request(tenant_res, onboarding: dict) -> None:
        """Email the ops inbox that a tenant requested WhatsApp setup. Best-effort
        — never raises. No-op when WHATSAPP_ONBOARDING_ALERT_EMAIL is unset (the
        request is still visible in the superadmin requests queue)."""
        to_email = (settings.WHATSAPP_ONBOARDING_ALERT_EMAIL or "").strip()
        if not to_email:
            return
        try:
            from app.services.email_service import EmailService

            biz = onboarding.get("business_name") or "—"
            num = onboarding.get("contact_number") or "—"
            disp = onboarding.get("display_name") or "—"
            org_id = getattr(tenant_res, "organization_id", None)
            tenant_id = getattr(tenant_res, "tenant_id", None)
            text = (
                "A client requested WhatsApp setup (concierge onboarding).\n\n"
                f"Business name : {biz}\n"
                f"Number        : {num}\n"
                f"Display name  : {disp}\n"
                f"Organization  : {org_id}\n"
                f"Tenant        : {tenant_id}\n\n"
                "Next: onboard this number to a WABA in the Plivo Console, then record "
                "it in the superadmin console (WhatsApp setup requests → Mark connected)."
            )
            await EmailService.send(to_email, f"[NOKVO] WhatsApp setup requested — {biz}", text)
        except Exception:
            pass

    @classmethod
    async def disconnect_whatsapp_number(cls, tenant_res, db) -> dict:
        """Disconnect the tenant's WhatsApp sender — sends then skip
        (``no_whatsapp_sender``); never borrows another tenant's sender."""
        from sqlalchemy.orm.attributes import flag_modified

        ps = dict(tenant_res.provider_status or {})
        cfg = dict(ps.get("plivo") or {})
        cfg.pop("whatsapp_number", None)
        cfg.pop("whatsapp_onboarding", None)  # back to not_requested (also cancels a pending request)
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
