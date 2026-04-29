from decimal import Decimal, ROUND_HALF_UP
from app.core.config import settings
from app.models.tenant_resources import TenantResources
from app.models.tenant_usage_event import TenantUsageEvent


class TenantBillingService:
    @staticmethod
    def _money(value: float | Decimal) -> Decimal:
        return Decimal(str(value)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)

    @staticmethod
    def monthly_provisioned_cost(tenant_res: TenantResources | None) -> Decimal:
        if not tenant_res:
            return TenantBillingService._money(0)

        total = Decimal("0")
        total += TenantBillingService._money(settings.ORGANIZATION_BASE_MONTHLY_COST_USD)

        if tenant_res.qdrant_collection_name:
            total += TenantBillingService._money(settings.QDRANT_COLLECTION_MONTHLY_COST_USD)
        if tenant_res.redis_namespace:
            total += TenantBillingService._money(settings.SHARED_REDIS_NAMESPACE_MONTHLY_COST_USD)
        if tenant_res.blob_prefix:
            total += TenantBillingService._money(settings.BLOB_PREFIX_MONTHLY_COST_USD)
        if tenant_res.key_vault_name:
            total += TenantBillingService._money(settings.KEY_VAULT_REFERENCE_MONTHLY_COST_USD)

        provider_status = tenant_res.provider_status or {}
        if provider_status.get("llm_status") in {"provisioned", "skipped"}:
            total += TenantBillingService._money(settings.AZURE_OPENAI_MONTHLY_COST_USD)
        if provider_status.get("stt_provider") == "soniox":
            total += TenantBillingService._money(settings.SONIOX_STT_MONTHLY_COST_USD)
        if provider_status.get("tts_provider") == "sarvam":
            total += TenantBillingService._money(settings.SARVAM_TTS_MONTHLY_COST_USD)
        if provider_status.get("twilio_provider") == "twilio":
            total += TenantBillingService._money(settings.TWILIO_SUBACCOUNT_MONTHLY_COST_USD)

        return total

    @staticmethod
    def usage_cost(
        minutes: int = 0,
        stt_minutes: float = 0,
        telephony_minutes: float = 0,
        tts_characters: int = 0,
        llm_api_calls: int = 0,
        llm_input_tokens: int = 0,
        llm_output_tokens: int = 0,
        storage_gb: float = 0,
        infrastructure_units: int = 0,
    ) -> Decimal:
        total = Decimal("0")
        total += TenantBillingService._money(minutes * settings.COST_PER_VOICE_MINUTE_USD)
        total += TenantBillingService._money(Decimal(str(stt_minutes)) * Decimal(str(settings.COST_PER_STT_MINUTE_USD)))
        total += TenantBillingService._money(Decimal(str(telephony_minutes)) * Decimal(str(settings.COST_PER_TWILIO_MINUTE_USD)))
        total += TenantBillingService._money((Decimal(tts_characters) / Decimal("1000")) * Decimal(str(settings.COST_PER_TTS_1K_CHARS_USD)))
        total += TenantBillingService._money((Decimal(llm_input_tokens) / Decimal("1000")) * Decimal(str(settings.COST_PER_LLM_INPUT_1K_TOKENS_USD)))
        total += TenantBillingService._money((Decimal(llm_output_tokens) / Decimal("1000")) * Decimal(str(settings.COST_PER_LLM_OUTPUT_1K_TOKENS_USD)))
        total += TenantBillingService._money((Decimal(llm_api_calls) / Decimal("1000")) * Decimal("0.50"))
        total += TenantBillingService._money(Decimal(str(storage_gb)) * Decimal("0.10"))
        total += TenantBillingService._money(Decimal(infrastructure_units) * Decimal("0.25"))
        return total

    @staticmethod
    def event_cost_breakdown(event: TenantUsageEvent | dict) -> dict:
        source = event if isinstance(event, dict) else {
            "minutes": event.minutes,
            "stt_minutes": event.stt_minutes,
            "telephony_minutes": event.telephony_minutes,
            "tts_characters": event.tts_characters,
            "llm_input_tokens": event.llm_input_tokens,
            "llm_output_tokens": event.llm_output_tokens,
        }
        return {
            "voice_minutes_cost_usd": float(TenantBillingService._money((source.get("minutes", 0) or 0) * settings.COST_PER_VOICE_MINUTE_USD)),
            "stt_cost_usd": float(TenantBillingService._money(Decimal(str(source.get("stt_minutes", 0) or 0)) * Decimal(str(settings.COST_PER_STT_MINUTE_USD)))),
            "telephony_cost_usd": float(TenantBillingService._money(Decimal(str(source.get("telephony_minutes", 0) or 0)) * Decimal(str(settings.COST_PER_TWILIO_MINUTE_USD)))),
            "tts_cost_usd": float(TenantBillingService._money((Decimal(source.get("tts_characters", 0) or 0) / Decimal("1000")) * Decimal(str(settings.COST_PER_TTS_1K_CHARS_USD)))),
            "llm_api_calls_cost_usd": float(TenantBillingService._money((Decimal(source.get("llm_api_calls", 0) or 0) / Decimal("1000")) * Decimal("0.50"))),
            "llm_input_cost_usd": float(TenantBillingService._money((Decimal(source.get("llm_input_tokens", 0) or 0) / Decimal("1000")) * Decimal(str(settings.COST_PER_LLM_INPUT_1K_TOKENS_USD)))),
            "llm_output_cost_usd": float(TenantBillingService._money((Decimal(source.get("llm_output_tokens", 0) or 0) / Decimal("1000")) * Decimal(str(settings.COST_PER_LLM_OUTPUT_1K_TOKENS_USD)))),
            "storage_cost_usd": float(TenantBillingService._money(Decimal(str(source.get("storage_gb", 0) or 0)) * Decimal("0.10"))),
            "infrastructure_cost_usd": float(TenantBillingService._money(Decimal(source.get("infrastructure_units", 0) or 0) * Decimal("0.25"))),
        }
