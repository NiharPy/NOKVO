from app.core.config import settings
from app.services.azure_keyvault_service import AzureKeyVaultService


class SonioxSTTService:
    @staticmethod
    def _language_hints(language: str | None) -> list[str]:
        if not language:
            return []
        primary = language.split("-", 1)[0].strip().lower()
        return [primary] if primary else []

    @staticmethod
    async def provision_stt(
        tenant_id: str,
        language: str | None = None,
        secret_refs: dict | None = None,
    ) -> dict:
        api_key_ref = None
        if secret_refs:
            api_key_ref = (secret_refs.get("stt_api_key") or {}).get("secret_name")
        if not api_key_ref:
            api_key_ref = AzureKeyVaultService._secret_name(tenant_id, "stt-api-key")

        if settings.SONIOX_API_KEY and api_key_ref:
            await AzureKeyVaultService.set_secret_value(
                api_key_ref,
                settings.SONIOX_API_KEY,
                tenant_id,
                "stt_api_key",
            )

        return {
            "stt_provider": "soniox",
            "stt_status": "provisioned" if settings.SONIOX_API_KEY else "pending_credentials",
            "stt_transport": "websocket",
            "stt_endpoint": settings.SONIOX_STT_WEBSOCKET_URL,
            "stt_model": settings.SONIOX_STT_MODEL,
            "stt_audio_format": settings.SONIOX_STT_AUDIO_FORMAT,
            "stt_api_key_ref": api_key_ref,
            "stt_language_hints": SonioxSTTService._language_hints(language),
            "enable_language_identification": True,
            "enable_speaker_diarization": True,
            "enable_endpoint_detection": True,
        }
