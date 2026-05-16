from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.tenant_resources import TenantResources


class ExotelService:
    @staticmethod
    def _provider_status(tenant_res: TenantResources) -> dict:
        return dict(tenant_res.provider_status or {})

    @staticmethod
    def _exotel_config(tenant_res: TenantResources) -> dict:
        provider_status = ExotelService._provider_status(tenant_res)
        return dict(provider_status.get("exotel") or {})

    @staticmethod
    def _credentials(tenant_res: TenantResources) -> tuple[str, str, str, str]:
        cfg = ExotelService._exotel_config(tenant_res)
        api_key = cfg.get("api_key") or settings.EXOTEL_API_KEY
        api_token = cfg.get("api_token") or settings.EXOTEL_API_TOKEN
        account_sid = cfg.get("account_sid") or cfg.get("sid") or settings.EXOTEL_ACCOUNT_SID
        subdomain = cfg.get("subdomain") or settings.EXOTEL_SUBDOMAIN
        if not api_key or not api_token or not account_sid:
            raise RuntimeError("Exotel API key, token, and account SID are required for outbound calls.")
        return api_key, api_token, account_sid, subdomain

    @staticmethod
    def phone_link_response(tenant_res: TenantResources) -> dict:
        provider_status = ExotelService._provider_status(tenant_res)
        link = dict(provider_status.get("agent_phone_link") or {})
        if not link:
            links = list(provider_status.get("agent_phone_links") or [])
            link = next((item for item in links if item.get("provider") == "exotel" and item.get("status") == "linked"), {})
        if not link:
            return {
                "status": "not_linked",
                "provider": "exotel",
                "phone_number": settings.EXOTEL_CALLER_ID or tenant_res.twilio_phone_number,
                "latency_target_ms": 800,
            }
        return {
            "status": link.get("status") or "not_linked",
            "provider": "exotel",
            "phone_number": link.get("phone_number") or tenant_res.twilio_phone_number,
            "link_id": link.get("link_id"),
            "voice_url": link.get("voice_url"),
            "incoming_phone_number_sid": link.get("incoming_phone_number_sid"),
            "linked_at": link.get("linked_at"),
            "unlinked_at": link.get("unlinked_at"),
            "latency_target_ms": int(link.get("latency_target_ms") or 800),
        }

    @staticmethod
    async def link_agent_phone_number(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        phone_number: str,
        public_base_url: str,
    ) -> dict:
        link_id = str(uuid.uuid4())
        base = public_base_url.rstrip("/")
        voice_url = f"{base}/api/org-auth/agent/exotel/voice/{link_id}"
        media_url = f"{base.replace('https://', 'wss://').replace('http://', 'ws://')}/api/org-auth/agent/exotel/media/{link_id}"
        linked_at = datetime.now(timezone.utc).isoformat()
        link = {
            "link_id": link_id,
            "status": "linked",
            "provider": "exotel",
            "phone_number": phone_number,
            "voice_url": voice_url,
            "media_url": media_url,
            "linked_at": linked_at,
            "unlinked_at": None,
            "latency_target_ms": 800,
        }
        provider_status = ExotelService._provider_status(tenant_res)
        provider_status["agent_phone_link"] = link
        links = [item for item in list(provider_status.get("agent_phone_links") or []) if item.get("link_id") != link_id]
        links.append(link)
        provider_status["agent_phone_links"] = links[-10:]
        exotel_cfg = dict(provider_status.get("exotel") or {})
        exotel_cfg.update(
            {
                "provider": "exotel",
                "status": "linked",
                "from_number": phone_number,
                "voice_url": voice_url,
                "stream_url": media_url,
            }
        )
        provider_status["exotel"] = exotel_cfg
        tenant_res.twilio_phone_number = phone_number
        tenant_res.twilio_status = "linked_to_agent"
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return ExotelService.phone_link_response(tenant_res)

    @staticmethod
    async def unlink_agent_phone_number(tenant_res: TenantResources, db: AsyncSession) -> dict:
        provider_status = ExotelService._provider_status(tenant_res)
        link = dict(provider_status.get("agent_phone_link") or {})
        if link:
            link["status"] = "unlinked"
            link["unlinked_at"] = datetime.now(timezone.utc).isoformat()
            provider_status["agent_phone_link"] = link
        links = list(provider_status.get("agent_phone_links") or [])
        for item in links:
            if item.get("link_id") == link.get("link_id"):
                item["status"] = "unlinked"
                item["unlinked_at"] = link.get("unlinked_at")
        provider_status["agent_phone_links"] = links
        exotel_cfg = dict(provider_status.get("exotel") or {})
        exotel_cfg["status"] = "unlinked"
        provider_status["exotel"] = exotel_cfg
        tenant_res.twilio_status = "pending_assignment"
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return ExotelService.phone_link_response(tenant_res)

    @staticmethod
    async def initiate_outbound_call(
        tenant_res: TenantResources,
        *,
        to_number: str,
        stream_url: str,
        status_callback: str | None = None,
        custom_field: str | None = None,
        from_number: str | None = None,
    ) -> dict[str, Any]:
        api_key, api_token, account_sid, subdomain = ExotelService._credentials(tenant_res)
        caller_id = from_number or ExotelService._exotel_config(tenant_res).get("from_number") or settings.EXOTEL_CALLER_ID
        if not caller_id:
            raise RuntimeError("An Exotel caller ID / exophone is required for outbound calls.")
        url = f"https://{subdomain.rstrip('/')}/v1/accounts/{account_sid}/calls/connect"
        data: dict[str, Any] = {
            "from": to_number,
            "callerid": caller_id,
            "streamurl": stream_url,
            "streamtype": "bidirectional",
        }
        if status_callback:
            data["statuscallback"] = status_callback
            data["statuscallbackevents[]"] = settings.EXOTEL_STATUS_CALLBACK_EVENTS.split(",")
        if custom_field:
            data["customfield"] = custom_field[:128]
        async with httpx.AsyncClient(timeout=30.0, auth=(api_key, api_token)) as client:
            response = await client.post(url, data=data)
            if response.status_code >= 400:
                raise RuntimeError(f"Exotel outbound call failed ({response.status_code}): {response.text[:300]}")
            try:
                return response.json()
            except Exception:
                return {"raw": response.text}
