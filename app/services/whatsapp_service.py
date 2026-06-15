"""WhatsApp template sender (Plivo WhatsApp Business API).

Sends a **Meta-approved template** message to a customer's phone. Business-
initiated WhatsApp (the customer reached us by *phone* — there's no open 24h
chat window) only delivers via a registered template, so free-form text is not
supported here by design.

Multi-tenant correctness
------------------------
Both the auth credentials AND the sender number are resolved from the tenant's
own config (``tenant_res.provider_status['plivo']``), exactly the way
``PlivoService.initiate_outbound_call`` resolves the tenant's voice DID +
subaccount token. The WABA sender is bound to the tenant's Plivo subaccount, so
a single global sender would be wrong. ``settings.PLIVO_WHATSAPP_FROM`` is only a
fallback for local/master testing.

Best-effort
-----------
Every public method returns a dict and never raises out — callers (the booking
macro, the brochure macro) treat a non-``ok`` result as "not sent" and continue.
A WhatsApp/Plivo hiccup must never break a call, a booking, or teardown.
"""
from __future__ import annotations

import logging
from typing import Any

import httpx

from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.services.plivo_service import PlivoError, PlivoService

logger = logging.getLogger(__name__)


class WhatsAppService:
    @staticmethod
    def _resolve_sender(cfg: dict) -> str | None:
        """The tenant's registered WABA sender, with the global env var as a
        test-only fallback. Never a shared production sender."""
        return (cfg.get("whatsapp_number") or settings.PLIVO_WHATSAPP_FROM or "").strip() or None

    @staticmethod
    def _build_components(body_params: list[str] | None, media_url: str | None) -> list[dict[str, Any]]:
        """Map plain values to WhatsApp template components: an optional document
        header (the brochure link) + a body with text parameters."""
        components: list[dict[str, Any]] = []
        if media_url:
            components.append({
                "type": "header",
                "parameters": [{"type": "document", "document": {"link": media_url}}],
            })
        params = [{"type": "text", "text": str(p)} for p in (body_params or []) if str(p or "").strip()]
        if params:
            components.append({"type": "body", "parameters": params})
        return components

    @classmethod
    async def send_template(
        cls,
        *,
        tenant_res: TenantResources,
        to_number: str,
        template_name: str,
        language: str = "en",
        body_params: list[str] | None = None,
        media_url: str | None = None,
        sender_override: str | None = None,
    ) -> dict[str, Any]:
        """Send an approved WhatsApp template to ``to_number``. Returns
        ``{ok: bool, ...}``; ``skipped`` when the feature is off / unconfigured."""
        if not settings.PLIVO_WHATSAPP_ENABLED:
            return {"ok": False, "skipped": True, "reason": "whatsapp_disabled"}
        if not (to_number and template_name):
            return {"ok": False, "skipped": True, "reason": "missing_recipient_or_template"}

        cfg = PlivoService._plivo_config(tenant_res)
        # Precedence: a per-PROJECT sender (sender_override) wins over the tenant's
        # WABA number, which wins over the test-only global fallback. So a project
        # configured with its own WhatsApp number sends its brochure/location from
        # that number.
        sender = (sender_override or "").strip() or cls._resolve_sender(cfg)
        if not sender:
            # No project/tenant WABA number (and no test fallback) → never borrow
            # another tenant's sender; just skip.
            return {"ok": False, "skipped": True, "reason": "no_whatsapp_sender"}

        # Same auth resolution as voice: tenant subaccount when available, else
        # the master account.
        sub_auth_id = cfg.get("subaccount_auth_id")
        sub_token = PlivoService._subaccount_token(cfg)
        try:
            auth = (sub_auth_id, sub_token) if (sub_auth_id and sub_token) else PlivoService._master_auth()
        except PlivoError as exc:
            return {"ok": False, "skipped": True, "reason": f"no_auth:{exc}"}

        body: dict[str, Any] = {
            "src": sender,
            "dst": to_number,
            "type": "whatsapp",
            "template": {
                "name": template_name,
                "language": language or "en",
            },
        }
        components = cls._build_components(body_params, media_url)
        if components:
            body["template"]["components"] = components

        url = f"{PlivoService._base(auth[0])}/Message/"
        try:
            data = await PlivoService._request("POST", url, auth=auth, json_body=body)
        except PlivoError as exc:
            logger.warning("NOKVO-WA: template send failed (%s → %s): %s", template_name, to_number, exc)
            return {"ok": False, "error": str(exc)[:300]}
        except (httpx.HTTPError, Exception) as exc:  # noqa: BLE001 — never raise out
            logger.warning("NOKVO-WA: template send error (%s → %s): %s", template_name, to_number, exc)
            return {"ok": False, "error": str(exc)[:300]}

        message_uuid = None
        if isinstance(data, dict):
            uuids = data.get("message_uuid") or data.get("api_id")
            message_uuid = uuids[0] if isinstance(uuids, list) and uuids else uuids
        logger.info("NOKVO-WA: template '%s' sent to %s (uuid=%s)", template_name, to_number, message_uuid)
        return {"ok": True, "message_uuid": message_uuid, "sender": sender}

    @classmethod
    async def send_for_org(
        cls,
        db,
        organization_id,
        *,
        to_number: str,
        template_name: str,
        language: str = "en",
        body_params: list[str] | None = None,
        media_url: str | None = None,
        sender_override: str | None = None,
    ) -> dict[str, Any]:
        """Convenience for tool handlers that only carry ``db`` + ``organization_id``:
        resolve the tenant, then send. Best-effort — never raises."""
        if not settings.PLIVO_WHATSAPP_ENABLED:
            return {"ok": False, "skipped": True, "reason": "whatsapp_disabled"}
        try:
            from sqlalchemy import select

            res = await db.execute(
                select(TenantResources).where(TenantResources.organization_id == organization_id)
            )
            tenant_res = res.scalars().first()
        except Exception as exc:  # noqa: BLE001
            logger.debug("NOKVO-WA: tenant lookup failed for org %s: %s", organization_id, exc)
            return {"ok": False, "skipped": True, "reason": "tenant_lookup_failed"}
        if tenant_res is None:
            return {"ok": False, "skipped": True, "reason": "no_tenant"}
        return await cls.send_template(
            tenant_res=tenant_res,
            to_number=to_number,
            template_name=template_name,
            language=language,
            body_params=body_params,
            media_url=media_url,
            sender_override=sender_override,
        )
