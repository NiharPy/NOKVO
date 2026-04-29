import httpx
from app.core.config import settings
from app.services.azure_keyvault_service import AzureKeyVaultService


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
