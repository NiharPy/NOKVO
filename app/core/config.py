from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    PROJECT_NAME: str = "SuperAdmin Privileged Access Management"
    
    # Database
    POSTGRES_SERVER: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: str
    POSTGRES_DB: str
    
    @property
    def SQLALCHEMY_DATABASE_URI(self) -> str:
        return f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}@{self.POSTGRES_SERVER}/{self.POSTGRES_DB}?ssl=require"

    # JWT
    SECRET_KEY: str
    ALGORITHM: str = "HS256"
    ACCESS_TOKEN_EXPIRE_MINUTES: int = 15
    REFRESH_TOKEN_EXPIRE_HOURS: int = 4

    # Nokvo One TOTP encryption (Fernet key; if unset, derived from SECRET_KEY).
    NOKVO_TOTP_ENCRYPTION_KEY: str = ""

    # Nokvo One signup/invite
    NOKVO_ONE_PUBLIC_BASE_URL: str = "http://localhost:5173"
    NOKVO_ONE_EMAIL_TOKEN_TTL_HOURS: int = 24
    NOKVO_ONE_INVITE_TOKEN_TTL_HOURS: int = 72

    # SMTP (optional — when unset, emails are logged only)
    SMTP_HOST: str = ""
    SMTP_PORT: int = 587
    SMTP_USERNAME: str = ""
    SMTP_PASSWORD: str = ""
    SMTP_FROM_EMAIL: str = "no-reply@nokvo.ai"
    SMTP_FROM_NAME: str = "Nokvo"
    SMTP_USE_TLS: bool = True

    # Google OAuth
    GOOGLE_OAUTH_CLIENT_ID: str = ""
    GOOGLE_OAUTH_CLIENT_SECRET: str = ""
    ZOHO_CLIENT_ID: str = ""
    ZOHO_CLIENT_SECRET: str = ""
    ZOHO_REDIRECT_URI: str = ""
    ZOHO_ACCOUNTS_URL: str = "https://accounts.zoho.com"
    
    # WebAuthn
    RP_ID: str = "localhost"
    RP_NAME: str = "NOKVO SuperAdmin"
    EXPECTED_ORIGIN: str = "http://localhost:5173"
    AGENT_PUBLIC_BASE_URL: str = "http://localhost:8000"

    # Redis (Rate Limiting & Tenant Cache)
    REDIS_URL: str = "redis://localhost:6379"

    # Azure Provisioning
    AZURE_SUBSCRIPTION_ID: str = ""
    AZURE_DEFAULT_REGION: str = "centralindia"
    AZURE_OPENAI_REGION: str = "swedencentral"
    AZURE_OPENAI_GLOBAL_ENDPOINT: str = ""
    AZURE_OPENAI_GLOBAL_API_KEY: str = ""
    AZURE_OPENAI_GLOBAL_DEPLOYMENT: str = "gpt-5.4-mini"
    AZURE_OPENAI_GLOBAL_API_VERSION: str = "2024-10-21"
    AZURE_OPENAI_AGENT_DEPLOYMENT: str = "gpt-4-1-mini"
    AZURE_OPENAI_AGENT_MODEL: str = "gpt-4.1-mini"
    AZURE_OPENAI_AGENT_API_VERSION: str = "2024-10-21"

    # Nokvo One Azure OpenAI realtime-mini deployment.
    # Per Azure Foundry docs (May 2026):
    #   - gpt-realtime-mini valid versions: 2025-10-06, 2025-12-15
    #   - Supported regions: canadacentral, centralus, eastus, eastus2, northcentralus,
    #     francecentral, norwayeast, swedencentral, switzerlandnorth, southindia
    #   - SKU: GlobalStandard everywhere; DataZoneStandard only in southindia.
    # 2025-08-28 belongs to the full gpt-realtime (not the mini) — pairing it with
    # gpt-realtime-mini returned DeploymentModelNotSupported. Use 2025-12-15 (newest mini).
    AZURE_OPENAI_REALTIME_MODEL: str = "gpt-realtime-mini"
    AZURE_OPENAI_REALTIME_MODEL_VERSION: str = "2025-12-15"
    AZURE_OPENAI_REALTIME_DEPLOYMENT: str = "gpt-realtime-mini"
    AZURE_OPENAI_REALTIME_SKU: str = "GlobalStandard"
    AZURE_OPENAI_REALTIME_REGION: str = "swedencentral"
    AZURE_MANAGED_IDENTITY_CLIENT_ID: str = ""
    AZURE_TENANT_ID: str = ""
    AZURE_CLIENT_ID: str = ""
    AZURE_CLIENT_SECRET: str = ""
    ALLOW_AZURE_CLIENT_SECRET_FALLBACK: bool = True
    AZURE_PREFER_MANAGED_IDENTITY: bool = False
    AZURE_SHARED_STORAGE_ACCOUNT: str = ""
    AZURE_SHARED_STORAGE_CONTAINER: str = "nokvo-tenants"
    AZURE_SHARED_KEY_VAULT_NAME: str = ""
    AZURE_LOG_ANALYTICS_WORKSPACE_ID: str = ""
    ENFORCE_KEY_VAULT_AUDIT_LOGS: bool = False
    KEY_VAULT_SECRET_ROTATION_DAYS: int = 90
    
    # Provisioning Flags
    CREATE_STORAGE_PER_TENANT: bool = False
    CREATE_KEYVAULT_PER_TENANT: bool = False
    TWILIO_AUTO_PROVISION: bool = False
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_BASE_URL: str = "https://api.twilio.com/2010-04-01"
    SONIOX_API_KEY: str = ""
    SONIOX_STT_WEBSOCKET_URL: str = "wss://stt-rt.soniox.com/transcribe-websocket"
    SONIOX_STT_MODEL: str = "stt-rt-v4"
    SONIOX_STT_AUDIO_FORMAT: str = "auto"
    SONIOX_TTS_MODEL: str = "tts-rt-v1-preview"
    SONIOX_TTS_REST_URL: str = "https://tts-rt.soniox.com/tts"
    SONIOX_TTS_STREAM_URL: str = "wss://tts-rt.soniox.com/tts-websocket"
    SONIOX_TTS_VOICE: str = "Adrian"
    SONIOX_TTS_SAMPLE_RATE: int = 24000
    SONIOX_TTS_AUDIO_FORMAT: str = "wav"
    AGENT_LLM_TIMEOUT_MS: int = 350
    AGENT_RETRIEVAL_TOP_K: int = 3
    AGENT_MAX_CONTEXT_CHARS: int = 3000
    AGENT_MIN_RELEVANCE_SCORE: float = 0.25
    AGENT_INTENT_CLASSIFIER_TIMEOUT_MS: int = 500
    # Voice agent latency tuning
    FILLER_TRIGGER_MS: int = 650          # Play filler only if the real answer is not ready quickly
    TTS_SEGMENT_IDLE_DONE_MS: int = 750   # Treat a TTS segment as done after audio goes idle
    TTS_SEGMENT_FIRST_AUDIO_TIMEOUT_MS: int = 2500
    AGENT_LLM_STREAM_TOTAL_MS: int = 6000 # Max total LLM stream wait
    AGENT_TOPIC_CONTINUITY_OVERLAP: float = 0.35  # Word overlap to reuse last chunks
    AGENT_MAX_FIRST_SENTENCE_CHARS: int = 110     # Force TTS dispatch after this many chars

    # Billing and Usage Tracking
    ORGANIZATION_BASE_MONTHLY_COST_USD: float = 5.00
    QDRANT_COLLECTION_MONTHLY_COST_USD: float = 8.00
    SHARED_REDIS_NAMESPACE_MONTHLY_COST_USD: float = 2.00
    BLOB_PREFIX_MONTHLY_COST_USD: float = 1.50
    KEY_VAULT_REFERENCE_MONTHLY_COST_USD: float = 1.00
    AZURE_OPENAI_MONTHLY_COST_USD: float = 12.00
    SONIOX_STT_MONTHLY_COST_USD: float = 4.00
    SONIOX_TTS_MONTHLY_COST_USD: float = 4.00
    TWILIO_SUBACCOUNT_MONTHLY_COST_USD: float = 3.00
    COST_PER_VOICE_MINUTE_USD: float = 0.0200
    COST_PER_STT_MINUTE_USD: float = 0.0120
    COST_PER_TWILIO_MINUTE_USD: float = 0.0130
    COST_PER_TTS_1K_CHARS_USD: float = 0.0180
    COST_PER_LLM_INPUT_1K_TOKENS_USD: float = 0.0030
    COST_PER_LLM_OUTPUT_1K_TOKENS_USD: float = 0.0120
    
    # Provider APIs
    QDRANT_URL: str = ":memory:"
    QDRANT_API_KEY: str = ""
    QDRANT_COLLECTION_PREFIX: str = "tenant"
    QDRANT_VECTOR_SIZE: int = 1536
    
    OPENAI_API_KEY: str = ""
    OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    OPENAI_EMBEDDING_BATCH_SIZE: int = 96
    OPENAI_EMBEDDING_TIMEOUT_SECONDS: float = 20.0
    OPENAI_EMBEDDING_ALLOW_ZERO_FALLBACK: bool = False

    # Voice agent answer cache (Redis)
    AGENT_ANSWER_CACHE_ENABLED: bool = True
    AGENT_ANSWER_CACHE_TTL_SECONDS: int = 300

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

settings = Settings()
