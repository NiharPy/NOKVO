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

    # Redis (Rate Limiting & Tenant Cache)
    REDIS_URL: str = "redis://localhost:6379"

    # Azure Provisioning
    AZURE_SUBSCRIPTION_ID: str = ""
    AZURE_DEFAULT_REGION: str = "centralindia"
    AZURE_OPENAI_REGION: str = "swedencentral"
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

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

settings = Settings()
