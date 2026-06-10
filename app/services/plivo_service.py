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
    async def delete_application(cls, app_id: str) -> None:
        auth = cls._master_auth()
        await cls._request("DELETE", f"{cls._base(auth[0])}/Application/{app_id}/", auth=auth)

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
