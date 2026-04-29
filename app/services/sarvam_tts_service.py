from app.core.config import settings
from app.services.azure_keyvault_service import AzureKeyVaultService


class SarvamTTSService:
    @staticmethod
    def _default_speaker(language: str | None) -> str:
        return "Shubh" if (language or "").strip().lower() == "en-in" else "Anand"

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

        if settings.SARVAM_API_KEY and api_key_ref:
            await AzureKeyVaultService.set_secret_value(
                api_key_ref,
                settings.SARVAM_API_KEY,
                tenant_id,
                "tts_api_key",
            )

        target_language = language or "en-IN"
        return {
            "tts_provider": "sarvam",
            "tts_status": "provisioned" if settings.SARVAM_API_KEY else "pending_credentials",
            "tts_model": settings.SARVAM_TTS_MODEL,
            "tts_api_key_ref": api_key_ref,
            "tts_rest_endpoint": settings.SARVAM_TTS_REST_URL,
            "tts_stream_endpoint": settings.SARVAM_TTS_STREAM_URL,
            "tts_target_language_code": target_language,
            "tts_speaker": settings.SARVAM_TTS_SPEAKER or SarvamTTSService._default_speaker(target_language),
            "tts_sample_rate": settings.SARVAM_TTS_SAMPLE_RATE,
            "tts_audio_format": settings.SARVAM_TTS_AUDIO_FORMAT,
        }
