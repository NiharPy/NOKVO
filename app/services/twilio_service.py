import httpx
from datetime import datetime, timezone
import uuid
from app.core.config import settings
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.models.tenant_resources import TenantResources
from sqlalchemy.ext.asyncio import AsyncSession


class TwilioService:
    @staticmethod
    def _format_http_error(error: httpx.HTTPStatusError) -> str:
        response = error.response
        pieces = [f"status={response.status_code}"]
        try:
            body = response.json()
        except Exception:
            body = response.text
        if body:
            pieces.append(f"body={body}")
        return " | ".join(pieces)

    @staticmethod
    async def provision_subaccount(
        tenant_id: str,
        organization_name: str,
        secret_refs: dict | None = None,
        auto_provision: bool = False,
    ) -> dict:
        if not settings.TWILIO_ACCOUNT_SID or not settings.TWILIO_AUTH_TOKEN:
            return {
                "twilio_provider": "twilio",
                "subaccount_id": None,
                "phone_number_status": "pending_credentials",
                "subaccount_status": "skipped",
            }

        endpoint = f"{settings.TWILIO_BASE_URL.rstrip('/')}/Accounts.json"
        payload = {"FriendlyName": organization_name[:64]}

        try:
            async with httpx.AsyncClient(
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                timeout=30.0,
            ) as client:
                response = await client.post(endpoint, data=payload)
                response.raise_for_status()
                data = response.json()

            account_sid = data.get("sid")
            auth_token = data.get("auth_token")
            if not account_sid or not auth_token:
                raise RuntimeError("Twilio subaccount response did not include credentials.")

            if secret_refs:
                sid_ref = (secret_refs.get("twilio_account_sid") or {}).get("secret_name")
                token_ref = (secret_refs.get("twilio_auth_token") or {}).get("secret_name")
                if sid_ref:
                    await AzureKeyVaultService.set_secret_value(
                        sid_ref, account_sid, tenant_id, "twilio_account_sid"
                    )
                if token_ref:
                    await AzureKeyVaultService.set_secret_value(
                        token_ref, auth_token, tenant_id, "twilio_auth_token"
                    )

            return {
                "twilio_provider": "twilio",
                "subaccount_id": account_sid,
                "phone_number_status": "pending_number_purchase" if auto_provision else "pending_assignment",
                "subaccount_status": "provisioned",
            }
        except httpx.HTTPStatusError as e:
            return {
                "twilio_provider": "twilio",
                "subaccount_id": None,
                "phone_number_status": "pending_credentials",
                "subaccount_status": "failed",
                "error": TwilioService._format_http_error(e),
            }
        except httpx.HTTPError as e:
            return {
                "twilio_provider": "twilio",
                "subaccount_id": None,
                "phone_number_status": "pending_credentials",
                "subaccount_status": "failed",
                "error": f"transport_error={str(e)}",
            }
        except Exception as e:
            raise RuntimeError(f"Failed to provision Twilio subaccount: {str(e)}")

    @staticmethod
    async def ensure_subaccount(
        tenant_res: TenantResources,
        organization_name: str,
        db: AsyncSession,
    ) -> dict:
        provider_status = dict(tenant_res.provider_status or {})
        if provider_status.get("twilio_subaccount_id"):
            return {
                "twilio_provider": "twilio",
                "subaccount_id": provider_status.get("twilio_subaccount_id"),
                "subaccount_status": provider_status.get("twilio_subaccount_status") or "provisioned",
                "phone_number_status": tenant_res.twilio_status or provider_status.get("phone_number_status") or "pending_assignment",
            }

        result = await TwilioService.provision_subaccount(
            tenant_id=tenant_res.tenant_id,
            organization_name=organization_name,
            secret_refs=tenant_res.secret_refs or {},
            auto_provision=False,
        )
        provider_status["twilio_provider"] = result.get("twilio_provider")
        provider_status["twilio_subaccount_id"] = result.get("subaccount_id")
        provider_status["twilio_subaccount_status"] = result.get("subaccount_status")
        provider_status["twilio_error"] = result.get("error")
        tenant_res.twilio_status = result.get("phone_number_status") or tenant_res.twilio_status
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return result

    @staticmethod
    async def _credentials(tenant_res: TenantResources) -> tuple[str, str]:
        provider_status = dict(tenant_res.provider_status or {})
        account_sid = provider_status.get("twilio_subaccount_id") or settings.TWILIO_ACCOUNT_SID
        auth_token = None
        token_ref = ((tenant_res.secret_refs or {}).get("twilio_auth_token") or {}).get("secret_name")
        if token_ref:
            try:
                auth_token = await AzureKeyVaultService.get_secret_value(token_ref)
            except Exception:
                auth_token = None
        auth_token = auth_token or settings.TWILIO_AUTH_TOKEN
        if not account_sid or not auth_token:
            raise RuntimeError("Twilio credentials are not configured for this organization.")
        return account_sid, auth_token

    @staticmethod
    async def _find_incoming_number(account_sid: str, auth_token: str, phone_number: str) -> dict:
        endpoint = f"{settings.TWILIO_BASE_URL.rstrip('/')}/Accounts/{account_sid}/IncomingPhoneNumbers.json"
        async with httpx.AsyncClient(auth=(account_sid, auth_token), timeout=30.0) as client:
            response = await client.get(endpoint, params={"PhoneNumber": phone_number})
            response.raise_for_status()
            numbers = response.json().get("incoming_phone_numbers") or []
        if not numbers:
            raise RuntimeError("Phone number was not found in the organization's Twilio account.")
        return numbers[0]

    @staticmethod
    async def link_agent_phone_number(
        tenant_res: TenantResources,
        db: AsyncSession,
        *,
        phone_number: str,
        public_base_url: str | None = None,
    ) -> dict:
        account_sid, auth_token = await TwilioService._credentials(tenant_res)
        number = await TwilioService._find_incoming_number(account_sid, auth_token, phone_number)
        incoming_sid = number.get("sid")
        if not incoming_sid:
            raise RuntimeError("Twilio phone number response did not include an IncomingPhoneNumber SID.")

        link_id = str(uuid.uuid4())
        base_url = (public_base_url or settings.AGENT_PUBLIC_BASE_URL).rstrip("/")
        voice_url = f"{base_url}/api/org-auth/agent/twilio/voice/{link_id}"
        endpoint = f"{settings.TWILIO_BASE_URL.rstrip('/')}/Accounts/{account_sid}/IncomingPhoneNumbers/{incoming_sid}.json"
        try:
            async with httpx.AsyncClient(auth=(account_sid, auth_token), timeout=30.0) as client:
                response = await client.post(
                    endpoint,
                    data={
                        "VoiceUrl": voice_url,
                        "VoiceMethod": "POST",
                        "StatusCallback": f"{base_url}/api/org-auth/agent/twilio/status/{link_id}",
                        "StatusCallbackMethod": "POST",
                    },
                )
                response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(f"Failed to configure Twilio phone webhook: {TwilioService._format_http_error(exc)}") from exc

        linked_at = datetime.now(timezone.utc).isoformat()
        link = {
            "link_id": link_id,
            "status": "linked",
            "phone_number": phone_number,
            "incoming_phone_number_sid": incoming_sid,
            "twilio_account_sid": account_sid,
            "voice_url": voice_url,
            "linked_at": linked_at,
            "unlinked_at": None,
            "latency_target_ms": 800,
        }
        provider_status = dict(tenant_res.provider_status or {})
        provider_status["agent_phone_link"] = link
        tenant_res.twilio_phone_number = phone_number
        tenant_res.twilio_status = "linked_to_agent"
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return link

    @staticmethod
    async def unlink_agent_phone_number(tenant_res: TenantResources, db: AsyncSession) -> dict:
        provider_status = dict(tenant_res.provider_status or {})
        link = dict(provider_status.get("agent_phone_link") or {})
        if not link:
            return {"status": "not_linked", "phone_number": tenant_res.twilio_phone_number}

        account_sid, auth_token = await TwilioService._credentials(tenant_res)
        incoming_sid = link.get("incoming_phone_number_sid")
        if incoming_sid:
            endpoint = f"{settings.TWILIO_BASE_URL.rstrip('/')}/Accounts/{account_sid}/IncomingPhoneNumbers/{incoming_sid}.json"
            try:
                async with httpx.AsyncClient(auth=(account_sid, auth_token), timeout=30.0) as client:
                    response = await client.post(endpoint, data={"VoiceUrl": "", "StatusCallback": ""})
                    response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise RuntimeError(f"Failed to clear Twilio phone webhook: {TwilioService._format_http_error(exc)}") from exc

        link["status"] = "unlinked"
        link["unlinked_at"] = datetime.now(timezone.utc).isoformat()
        provider_status["agent_phone_link"] = link
        tenant_res.twilio_phone_number = None
        tenant_res.twilio_status = "pending_assignment"
        tenant_res.provider_status = provider_status
        db.add(tenant_res)
        await db.commit()
        await db.refresh(tenant_res)
        return link
