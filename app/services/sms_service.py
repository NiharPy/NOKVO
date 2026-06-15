"""Plain SMS sender (Plivo Messaging API).

The brochure + location links are texted to the caller at the end of a call. Uses
the same Plivo ``/Message/`` endpoint as WhatsApp but a plain ``{src, dst, text}``
body — no template, no ``type``.

Multi-tenant correctness
------------------------
Auth credentials AND the sender resolve from the tenant's own config
(``tenant_res.provider_status['plivo']``), exactly like voice + WhatsApp. The
sender is the tenant's ``sms_number``; ``settings.PLIVO_SMS_FROM`` is only a
local/master test fallback — never a shared production sender.

Best-effort
-----------
Every public method returns a dict and never raises out — callers (end-of-call
teardown) treat a non-``ok`` result as "not sent" and continue. A Plivo hiccup
must never break a call or teardown.

India A2P note: delivery to Indian numbers requires TRAI DLT (a registered sender
header + DLT-approved content templates, link domains whitelisted). This service
sends regardless; registration is an operator concern in the Plivo/DLT console.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.plivo_service import PlivoError, PlivoService

logger = logging.getLogger(__name__)


class SmsService:
    @staticmethod
    def _resolve_sender(cfg: dict, sender_override: str | None = None) -> str | None:
        """The tenant's SMS sender (DLT header / long code), with the global env
        var as a test-only fallback. Never a shared production sender."""
        return (
            (sender_override or "").strip()
            or (cfg.get("sms_number") or "").strip()
            or (settings.PLIVO_SMS_FROM or "").strip()
            or None
        )

    @classmethod
    async def send_sms(
        cls,
        *,
        tenant_res: TenantResources,
        to_number: str,
        text: str,
        sender_override: str | None = None,
    ) -> dict[str, Any]:
        """Send a plain SMS to ``to_number``. Returns ``{ok: bool, ...}``;
        ``skipped`` when the feature is off / unconfigured."""
        if not settings.PLIVO_SMS_ENABLED:
            return {"ok": False, "skipped": True, "reason": "sms_disabled"}
        if not (to_number and (text or "").strip()):
            return {"ok": False, "skipped": True, "reason": "missing_recipient_or_text"}

        cfg = PlivoService._plivo_config(tenant_res)
        sender = cls._resolve_sender(cfg, sender_override)
        if not sender:
            # No tenant sms_number (and no test fallback) → never borrow another
            # tenant's sender; just skip.
            return {"ok": False, "skipped": True, "reason": "no_sms_sender"}

        # Same auth resolution as voice/WhatsApp: tenant subaccount when available,
        # else the master account.
        sub_auth_id = cfg.get("subaccount_auth_id")
        sub_token = PlivoService._subaccount_token(cfg)
        try:
            auth = (sub_auth_id, sub_token) if (sub_auth_id and sub_token) else PlivoService._master_auth()
        except PlivoError as exc:
            return {"ok": False, "skipped": True, "reason": f"no_auth:{exc}"}

        body: dict[str, Any] = {"src": sender, "dst": to_number, "text": text}
        url = f"{PlivoService._base(auth[0])}/Message/"
        try:
            data = await PlivoService._request("POST", url, auth=auth, json_body=body)
        except PlivoError as exc:
            logger.warning("NOKVO-SMS: send failed (→ %s): %s", to_number, exc)
            return {"ok": False, "error": str(exc)[:300]}
        except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001 — never raise out
            logger.warning("NOKVO-SMS: send error (→ %s): %s", to_number, exc)
            return {"ok": False, "error": str(exc)[:300]}

        message_uuid = None
        if isinstance(data, dict):
            uuids = data.get("message_uuid") or data.get("api_id")
            message_uuid = uuids[0] if isinstance(uuids, list) and uuids else uuids
        logger.info("NOKVO-SMS: sent to %s (uuid=%s)", to_number, message_uuid)
        return {"ok": True, "message_uuid": message_uuid, "sender": sender}

    @classmethod
    async def send_for_org(
        cls,
        db,
        organization_id,
        *,
        to_number: str,
        text: str,
        sender_override: str | None = None,
    ) -> dict[str, Any]:
        """Convenience for handlers that only carry ``db`` + ``organization_id``:
        resolve the tenant, then send. Best-effort — never raises."""
        if not settings.PLIVO_SMS_ENABLED:
            return {"ok": False, "skipped": True, "reason": "sms_disabled"}
        try:
            from sqlalchemy import select

            res = await db.execute(
                select(TenantResources).where(TenantResources.organization_id == organization_id)
            )
            tenant_res = res.scalars().first()
        except Exception as exc:  # noqa: BLE001
            logger.debug("NOKVO-SMS: tenant lookup failed for org %s: %s", organization_id, exc)
            return {"ok": False, "skipped": True, "reason": "tenant_lookup_failed"}
        if tenant_res is None:
            return {"ok": False, "skipped": True, "reason": "no_tenant"}
        return await cls.send_sms(
            tenant_res=tenant_res,
            to_number=to_number,
            text=text,
            sender_override=sender_override,
        )
