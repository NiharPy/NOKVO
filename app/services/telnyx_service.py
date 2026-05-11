"""Telnyx phone number management and inbound call linking.

Telnyx uses Bearer-token auth and a REST API at api.telnyx.com/v2.
Numbers are linked to a TeXML App (TELNYX_APP_ID) which routes inbound
calls to our webhook. Each tenant can have up to TELNYX_MAX_PHONE_LINKS
numbers linked simultaneously.

provider_status["agent_phone_links"] = [
    {
        "link_id": str,          # UUID, used in webhook URL
        "status": "linked",
        "provider": "telnyx",
        "phone_number": str,     # E.164
        "telnyx_number_id": str, # Telnyx resource ID
        "voice_url": str,
        "linked_at": str,        # ISO datetime
        "unlinked_at": str|None,
    },
    ...  # up to TELNYX_MAX_PHONE_LINKS entries
]
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.tenant_resources import TenantResources


class TelnyxService:

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _headers() -> dict[str, str]:
        return {
            "Authorization": f"Bearer {settings.TELNYX_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Number normalization and search
    # ------------------------------------------------------------------

    @staticmethod
    def normalize_phone_number(phone: str) -> str:
        """Normalize to E.164. 10-digit Indian numbers get +91 prefix."""
        phone = phone.strip()
        raw = "".join(c for c in phone if c.isdigit())
        if phone.startswith("+"):
            return phone
        if len(raw) == 10:
            return f"+91{raw}"
        if len(raw) == 12 and raw.startswith("91"):
            return f"+{raw}"
        if len(raw) == 11 and raw.startswith("1"):
            return f"+{raw}"
        return phone

    @staticmethod
    async def search_available_numbers(
        country_code: str = "US",
        number_type: str = "local",
        contains: str | None = None,
        limit: int = 10,
    ) -> list[dict]:
        """Search Telnyx available numbers inventory."""
        params: dict = {
            "filter[country_code]": country_code.upper(),
            "filter[type]": number_type,
            "filter[limit]": min(limit, 50),
        }
        if contains:
            params["filter[phone_number][contains]"] = contains

        async with httpx.AsyncClient(headers=TelnyxService._headers(), timeout=30.0) as client:
            resp = await client.get(
                f"{settings.TELNYX_BASE_URL}/available_phone_numbers",
                params=params,
            )
            if resp.status_code != 200:
                raise RuntimeError(
                    f"Telnyx available numbers search failed: {resp.status_code} {resp.text[:300]}"
                )
            items = resp.json().get("data") or []
            results = []
            for item in items:
                cost_info = (item.get("cost_information") or {})
                results.append({
                    "phone_number": item.get("phone_number", ""),
                    "locality": item.get("locality"),
                    "region": item.get("region"),
                    "country_code": item.get("country_code", country_code),
                    "monthly_cost": cost_info.get("monthly_cost"),
                    "features": [f.get("name", "") for f in (item.get("features") or [])],
                    "raw": item,
                })
            return results

    @staticmethod
    async def assign_number_to_texml_app(number_id: str, connection_id: str | None = None) -> None:
        """Idempotently assign a Telnyx phone number to the TeXML app."""
        conn_id = connection_id or settings.TELNYX_APP_ID
        if not conn_id:
            return
        async with httpx.AsyncClient(headers=TelnyxService._headers(), timeout=30.0) as client:
            resp = await client.patch(
                f"{settings.TELNYX_BASE_URL}/phone_numbers/{number_id}",
                json={"connection_id": conn_id},
            )
            if resp.status_code not in (200, 204):
                raise RuntimeError(
                    f"Failed to assign number to TeXML app: {resp.status_code} {resp.text[:300]}"
                )

    # ------------------------------------------------------------------
    # Number lookup
    # ------------------------------------------------------------------

    @staticmethod
    def _digit_suffix(s: str, n: int = 10) -> str:
        digits = "".join(c for c in s if c.isdigit())
        return digits[-n:] if len(digits) >= n else digits

    @staticmethod
    async def find_phone_number(phone_number: str) -> dict:
        """Return the Telnyx phone number object matching phone_number.

        Searches by E.164 exact match first, then falls back to digit-suffix
        scan across all owned numbers.
        """
        digits = TelnyxService._digit_suffix(phone_number)
        # Build E.164 candidates
        raw_digits = "".join(c for c in phone_number if c.isdigit())
        candidates: list[str] = []
        if phone_number.startswith("+"):
            candidates.append(phone_number)
        if len(raw_digits) == 10:
            candidates.append(f"+91{raw_digits}")
            candidates.append(f"+1{raw_digits}")
        if len(raw_digits) == 12 and raw_digits.startswith("91"):
            candidates.append(f"+{raw_digits}")
        if len(raw_digits) == 11 and raw_digits.startswith("1"):
            candidates.append(f"+{raw_digits}")
        if phone_number not in candidates:
            candidates.append(phone_number)

        async with httpx.AsyncClient(headers=TelnyxService._headers(), timeout=30.0) as client:
            # Try exact filter first
            for candidate in candidates:
                resp = await client.get(
                    f"{settings.TELNYX_BASE_URL}/phone_numbers",
                    params={"filter[phone_number]": candidate, "page[size]": 1},
                )
                if resp.status_code == 200:
                    items = resp.json().get("data") or []
                    if items:
                        return items[0]

            # Fallback: page through all numbers and suffix-match
            page_token: str | None = None
            while True:
                params: dict = {"page[size]": 250}
                if page_token:
                    params["page[after]"] = page_token
                resp = await client.get(
                    f"{settings.TELNYX_BASE_URL}/phone_numbers", params=params
                )
                resp.raise_for_status()
                body = resp.json()
                for num in body.get("data") or []:
                    stored = TelnyxService._digit_suffix(num.get("phone_number", ""))
                    if stored.endswith(digits) or digits.endswith(stored):
                        return num
                # Pagination
                next_token = (body.get("meta") or {}).get("page_after")
                if not next_token:
                    break
                page_token = next_token

        raise RuntimeError(
            f"Phone number {phone_number!r} was not found in this Telnyx account. "
            "Verify it is an active number in your Telnyx portal."
        )

    # ------------------------------------------------------------------
    # Webhook configuration on a Telnyx number
    # ------------------------------------------------------------------

    @staticmethod
    async def _configure_number_webhook(number_id: str, voice_url: str) -> None:
        """Point a Telnyx phone number at our TeXML voice webhook.

        Assigns the TeXML App (so Telnyx knows to use TeXML mode) AND sets a
        per-number webhook URL override so each number routes to its own link_id.
        Telnyx respects the per-number webhook even when a connection_id is set.
        """
        body: dict = {}
        if settings.TELNYX_APP_ID:
            body["connection_id"] = settings.TELNYX_APP_ID

        # Always set per-number webhook override — this is what Telnyx actually calls
        body["voice"] = {
            "webhooks": {
                "call_control_url": voice_url,
                "call_control_fallback_url": voice_url,
                "call_control_method": "POST",
            }
        }

        async with httpx.AsyncClient(headers=TelnyxService._headers(), timeout=30.0) as client:
            resp = await client.patch(
                f"{settings.TELNYX_BASE_URL}/phone_numbers/{number_id}",
                json=body,
            )
            if resp.status_code not in (200, 204):
                raise RuntimeError(
                    f"Failed to configure Telnyx webhook: {resp.status_code} {resp.text[:300]}"
                )

    @staticmethod
    async def _clear_number_webhook(number_id: str) -> None:
        try:
            async with httpx.AsyncClient(headers=TelnyxService._headers(), timeout=30.0) as client:
                await client.patch(
                    f"{settings.TELNYX_BASE_URL}/phone_numbers/{number_id}",
                    json={"connection_id": None},
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public link / unlink API
    # ------------------------------------------------------------------

    @staticmethod
    def _get_links(tenant_res: TenantResources) -> list[dict]:
        ps = dict(tenant_res.provider_status or {})
        links = ps.get("agent_phone_links") or []
        # backward-compat: migrate single-link format
        if not links and ps.get("agent_phone_link"):
            old = ps["agent_phone_link"]
            if old.get("status") == "linked":
                links = [old]
        return [l for l in links if isinstance(l, dict)]

    @staticmethod
    async def link_agent_phone_number(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        phone_number: str,
        public_base_url: str | None = None,
    ) -> dict:
        links = TelnyxService._get_links(tenant_res)
        active = [l for l in links if l.get("status") == "linked"]

        if len(active) >= settings.TELNYX_MAX_PHONE_LINKS:
            raise RuntimeError(
                f"Maximum of {settings.TELNYX_MAX_PHONE_LINKS} phone numbers can be linked. "
                "Unlink one before adding another."
            )

        # Check not already linked
        raw_digits = "".join(c for c in phone_number if c.isdigit())
        for existing in active:
            existing_digits = "".join(c for c in existing.get("phone_number", "") if c.isdigit())
            if existing_digits[-10:] == raw_digits[-10:]:
                raise RuntimeError(f"Phone number {phone_number!r} is already linked.")

        num = await TelnyxService.find_phone_number(phone_number)
        number_id = num.get("id")
        if not number_id:
            raise RuntimeError("Telnyx number response did not include an ID.")

        link_id = str(uuid.uuid4())
        base_url = (public_base_url or settings.AGENT_PUBLIC_BASE_URL).rstrip("/")
        voice_url = f"{base_url}/api/org-auth/agent/telnyx/voice/{link_id}"

        await TelnyxService._configure_number_webhook(number_id, voice_url)

        canonical = num.get("phone_number") or phone_number
        link = {
            "link_id": link_id,
            "status": "linked",
            "provider": "telnyx",
            "phone_number": canonical,
            "telnyx_number_id": number_id,
            "voice_url": voice_url,
            "linked_at": datetime.now(timezone.utc).isoformat(),
            "unlinked_at": None,
        }
        links.append(link)

        ps = dict(tenant_res.provider_status or {})
        ps["agent_phone_links"] = links
        tenant_res.provider_status = ps
        tenant_res.twilio_phone_number = canonical
        tenant_res.twilio_status = "linked_to_agent"
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return link

    @staticmethod
    async def unlink_agent_phone_number(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        link_id: str,
    ) -> dict:
        links = TelnyxService._get_links(tenant_res)
        target = next((l for l in links if l.get("link_id") == link_id), None)
        if not target:
            raise RuntimeError(f"Link ID {link_id!r} not found for this tenant.")

        number_id = target.get("telnyx_number_id")
        if number_id:
            await TelnyxService._clear_number_webhook(number_id)

        target["status"] = "unlinked"
        target["unlinked_at"] = datetime.now(timezone.utc).isoformat()

        # Remove from active list (keep only active links)
        active = [l for l in links if l.get("status") == "linked"]
        if not active:
            tenant_res.twilio_status = "unlinked"

        ps = dict(tenant_res.provider_status or {})
        ps["agent_phone_links"] = links
        tenant_res.provider_status = ps
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return target

    @staticmethod
    def list_linked_numbers(tenant_res: TenantResources) -> list[dict]:
        return [
            l for l in TelnyxService._get_links(tenant_res)
            if l.get("status") == "linked"
        ]

    # ------------------------------------------------------------------
    # Outbound call initiation
    # ------------------------------------------------------------------

    @staticmethod
    async def initiate_call(
        *,
        from_number: str,
        to_number: str,
        webhook_url: str,
        client_state: str | None = None,
    ) -> dict:
        """Initiate an outbound call via Telnyx Call Control API."""
        payload: dict = {
            "from": from_number,
            "to": to_number,
            "webhook_url": webhook_url,
            "webhook_url_method": "POST",
        }
        if client_state:
            import base64
            payload["client_state"] = base64.b64encode(client_state.encode()).decode()

        async with httpx.AsyncClient(headers=TelnyxService._headers(), timeout=30.0) as client:
            resp = await client.post(
                f"{settings.TELNYX_BASE_URL}/calls",
                json=payload,
            )
            if resp.status_code not in (200, 201):
                raise RuntimeError(
                    f"Telnyx call initiation failed: {resp.status_code} {resp.text[:300]}"
                )
            return resp.json().get("data") or resp.json()
