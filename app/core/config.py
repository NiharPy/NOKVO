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
    PLIVO_AUTO_PROVISION: bool = False
    
    # Provider APIs
    QDRANT_URL: str = ":memory:"
    QDRANT_API_KEY: str = ""
    QDRANT_COLLECTION_PREFIX: str = "tenant"
    QDRANT_VECTOR_SIZE: int = 1536

    model_config = SettingsConfigDict(env_file=".env", case_sensitive=True, extra="ignore")

settings = Settings()
