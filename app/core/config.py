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

    # Nokvo One onboarding v2 (outcome wizard, deferred MFA, simplified nav).
    # When false, the legacy onboarding flow is preserved exactly. The v2 surface
    # piggybacks on the same auth states + endpoints; it only changes how the
    # frontend routes and which actions require MFA up-front.
    NOKVO_ONBOARDING_V2: bool = False
    NOKVO_ONE_NATIVE_TOOL_CALLING: bool = True
    NOKVO_ONE_TOOL_LOOP_MAX_ITERATIONS: int = 4

    # Call-center ambience mixing for the agent's voice output. When enabled,
    # the frontend mic tester (and, when wired, the Exotel media path) layers
    # a low-volume office hum under the agent so calls feel realistic.
    # Files live in app/assets/audio/call_center_ambience/ (download via
    # `python -m app.scripts.download_ambience_audio`).
    NOKVO_CALL_CENTER_AMBIENCE_ENABLED: bool = True
    NOKVO_CALL_CENTER_AMBIENCE_VOLUME: float = 0.28  # 0.0 (silent) to 1.0 (full)

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
    GOOGLE_LEADS_OAUTH_CLIENT_ID: str = ""
    GOOGLE_LEADS_OAUTH_CLIENT_SECRET: str = ""
    GOOGLE_LEADS_OAUTH_REDIRECT_URI: str = ""
    GOOGLE_ADS_DEVELOPER_TOKEN: str = ""
    GOOGLE_ADS_LOGIN_CUSTOMER_ID: str = ""
    GOOGLE_ADS_API_VERSION: str = "v23"
    META_ADS_APP_ID: str = ""
    META_ADS_APP_SECRET: str = ""
    META_ADS_REDIRECT_URI: str = ""
    META_GRAPH_VERSION: str = "v23.0"
    META_LEADGEN_WEBHOOK_VERIFY_TOKEN: str = ""
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

    # Nokvo One per-tenant chat deployment (gpt-4.1-mini in South India). Only used by
    # the Nokvo One signup provisioner; other tenants are unaffected.
    # gpt-4.1-mini's only Azure version is 2025-04-14 (verified against the
    # Cognitive Services models catalog for southindia). 2024-07-18 belongs to
    # gpt-4o-mini and was rejected with DeploymentModelNotSupported.
    AZURE_OPENAI_CHAT_MODEL: str = "gpt-4.1-mini"
    AZURE_OPENAI_CHAT_MODEL_VERSION: str = "2025-04-14"
    AZURE_OPENAI_CHAT_DEPLOYMENT: str = "gpt-4-1-mini"
    AZURE_OPENAI_CHAT_SKU: str = "GlobalStandard"
    # Capacity is in K-TPM (1 unit = 1,000 tokens/min, 6 requests/min). The Azure
    # floor of 1 (1K TPM / 6 RPM) is far too small for conversational voice — a
    # single turn burns ~2-5K tokens and interactive testing trips the quota
    # every couple of utterances. Bumping to 500K TPM / 3,000 RPM gives any new
    # tenant headroom for sustained calls without falling back to the global pool.
    AZURE_OPENAI_CHAT_CAPACITY: int = 500
    AZURE_OPENAI_CHAT_REGION: str = "southindia"

    # Nokvo One per-tenant text embedding deployment (text-embedding-3-small in
    # South India). Deployed onto the same Azure OpenAI account as the chat model
    # during signup so the tenant's knowledge base embeds against its own
    # endpoint rather than the shared platform OpenAI key.
    # SKU note: southindia only offers GlobalStandard for text-embedding-3-small.
    AZURE_OPENAI_EMBEDDING_MODEL: str = "text-embedding-3-small"
    AZURE_OPENAI_EMBEDDING_MODEL_VERSION: str = "1"
    AZURE_OPENAI_EMBEDDING_DEPLOYMENT: str = "text-embedding-3-small"
    AZURE_OPENAI_EMBEDDING_SKU: str = "GlobalStandard"
    AZURE_OPENAI_EMBEDDING_CAPACITY: int = 1
    AZURE_OPENAI_EMBEDDING_REGION: str = "southindia"
    AZURE_OPENAI_EMBEDDING_API_VERSION: str = "2024-02-15-preview"
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
    AGENT_VOICE_BACKEND: str = "sarvam_pipeline"
    SARVAM_API_KEY: str = ""
    SARVAM_STT_REST_URL: str = "https://api.sarvam.ai/speech-to-text"
    SARVAM_STT_WEBSOCKET_URL: str = "wss://api.sarvam.ai/speech-to-text/ws"
    SARVAM_STT_MODEL: str = "saaras:v3"
    SARVAM_STT_MODE: str = "transcribe"
    SARVAM_STT_SAMPLE_RATE: int = 16000
    SARVAM_STT_AUDIO_ENCODING: str = "audio/wav"
    SARVAM_TTS_REST_URL: str = "https://api.sarvam.ai/text-to-speech"
    SARVAM_TTS_STREAM_URL: str = "https://api.sarvam.ai/text-to-speech/stream"
    SARVAM_TTS_WEBSOCKET_URL: str = "wss://api.sarvam.ai/text-to-speech/ws"
    SARVAM_TTS_MODEL: str = "bulbul:v3"
    SARVAM_TTS_SPEAKER: str = "shubh"
    SARVAM_TTS_SAMPLE_RATE: int = 24000
    SARVAM_TTS_AUDIO_CODEC: str = "wav"
    SARVAM_TTS_ENABLE_CACHED_RESPONSES: bool = False
    EXOTEL_API_KEY: str = ""
    EXOTEL_API_TOKEN: str = ""
    EXOTEL_ACCOUNT_SID: str = ""
    EXOTEL_SUBDOMAIN: str = "api.in.exotel.com"
    EXOTEL_CALLER_ID: str = ""
    EXOTEL_DEFAULT_SAMPLE_RATE: int = 8000
    EXOTEL_STATUS_CALLBACK_EVENTS: str = "answered,terminal"
    TELNYX_API_KEY: str = ""
    TELNYX_BASE_URL: str = "https://api.telnyx.com/v2"
    TELNYX_APP_ID: str = ""
    TELNYX_MAX_PHONE_LINKS: int = 5
    AGENT_LLM_TIMEOUT_MS: int = 350
    AGENT_RETRIEVAL_TOP_K: int = 3
    AGENT_MAX_CONTEXT_CHARS: int = 3000
    AGENT_MIN_RELEVANCE_SCORE: float = 0.35
    AGENT_MIN_RELEVANCE_SCORE_SENSITIVE: float = 0.45
    AGENT_RETRIEVAL_TOP_K_SENSITIVE: int = 8
    AGENT_RAG_DEBUG: bool = False
    AGENT_RAG_MIN_QUERY_WORDS: int = 3
    AGENT_TRANSLATE_FOR_RETRIEVAL_ENABLED: bool = True
    # Per-call conversation memory. Set to 10 min so a 7-min call always has
    # full history available even with no agent activity for a stretch
    # (caller on hold, on-screen confirmation, etc.). Refreshed on every
    # append, so an active call effectively never times out.
    AGENT_SESSION_HISTORY_TTL_SECONDS: int = 600
    AGENT_SESSION_HISTORY_MAX_TURNS: int = 30
    AGENT_INTENT_CLASSIFIER_TIMEOUT_MS: int = 500
    # Voice agent latency tuning
    FILLER_TRIGGER_MS: int = 400          # Play filler only if the real answer is not ready quickly
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
    OPENAI_EMBEDDING_TIMEOUT_SECONDS: float = 4.0
    # The OpenAI SDK defaults to 2 retries with exponential backoff on 429,
    # which can push a single embed call past 20s during rate-limit bursts.
    # Cap retries so voice latency stays bounded.
    OPENAI_EMBEDDING_MAX_RETRIES: int = 1
    OPENAI_EMBEDDING_ALLOW_ZERO_FALLBACK: bool = False

    # Voice agent answer cache (Redis)
    AGENT_ANSWER_CACHE_ENABLED: bool = True
    AGENT_ANSWER_CACHE_TTL_SECONDS: int = 300

    # End-of-booking SMS confirmation offer. Disabled by default because SMS
    # dispatch isn't wired into the platform yet — offering it then silently
    # not sending is a worse caller experience than not offering at all. Each
    # tenant can opt in via provider_status["agent_offer_sms_confirmation"] =
    # True once their SMS gateway is connected.
    NOKVO_AGENT_OFFER_SMS_CONFIRMATION: bool = False

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

settings = Settings()
