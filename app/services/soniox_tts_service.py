from app.core.config import settings
from app.services.azure_keyvault_service import AzureKeyVaultService


class SonioxTTSService:
    @staticmethod
    def _default_voice(language: str | None) -> str:
        if (language or "").strip().lower().startswith("en"):
            return "Adrian"
        return "Adrian"

    @staticmethod
    async def provision_tts(
        tenant_id: str,
        language: str | None = None,
        secret_refs: dict | None = None,
    ) -> dict:
        api_key_ref = None
        if secret_refs:
            api_key_ref = (secret_refs.get("tts_api_key") or {}).get("secret_name")
        if not api_key_ref:
            api_key_ref = AzureKeyVaultService._secret_name(tenant_id, "tts-api-key")

        if settings.SONIOX_API_KEY and api_key_ref:
            await AzureKeyVaultService.set_secret_value(
                api_key_ref,
                settings.SONIOX_API_KEY,
                tenant_id,
                "tts_api_key",
            )

        target_language = (language or "en-IN").split("-", 1)[0]
        return {
            "tts_provider": "soniox",
            "tts_status": "provisioned" if settings.SONIOX_API_KEY else "pending_credentials",
            "tts_model": settings.SONIOX_TTS_MODEL,
            "tts_api_key_ref": api_key_ref,
            "tts_rest_endpoint": settings.SONIOX_TTS_REST_URL,
            "tts_stream_endpoint": settings.SONIOX_TTS_STREAM_URL,
            "tts_target_language_code": target_language,
            "tts_voice": settings.SONIOX_TTS_VOICE or SonioxTTSService._default_voice(language),
            "tts_sample_rate": settings.SONIOX_TTS_SAMPLE_RATE,
            "tts_audio_format": settings.SONIOX_TTS_AUDIO_FORMAT,
        }
