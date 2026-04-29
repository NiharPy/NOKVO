from azure.identity import ClientSecretCredential, CredentialUnavailableError, ManagedIdentityCredential
from app.core.config import settings

class AzureAuth:
    _credential = None

    @classmethod
    def get_credential(cls):
        if cls._credential is None:
            has_client_secret_creds = bool(
                settings.AZURE_TENANT_ID
                and settings.AZURE_CLIENT_ID
                and settings.AZURE_CLIENT_SECRET
            )

            if has_client_secret_creds and not settings.AZURE_PREFER_MANAGED_IDENTITY:
                cls._credential = ClientSecretCredential(
                    tenant_id=settings.AZURE_TENANT_ID,
                    client_id=settings.AZURE_CLIENT_ID,
                    client_secret=settings.AZURE_CLIENT_SECRET,
                )
                return cls._credential

            mi_kwargs = {}
            if settings.AZURE_MANAGED_IDENTITY_CLIENT_ID:
                mi_kwargs["client_id"] = settings.AZURE_MANAGED_IDENTITY_CLIENT_ID
            managed_identity = ManagedIdentityCredential(**mi_kwargs)

            try:
                # Validate early so local development can fall back cleanly.
                managed_identity.get_token("https://management.azure.com/.default")
                cls._credential = managed_identity
            except CredentialUnavailableError:
                if not settings.ALLOW_AZURE_CLIENT_SECRET_FALLBACK:
                    raise
                if not has_client_secret_creds:
                    raise
                cls._credential = ClientSecretCredential(
                    tenant_id=settings.AZURE_TENANT_ID,
                    client_id=settings.AZURE_CLIENT_ID,
                    client_secret=settings.AZURE_CLIENT_SECRET,
                )
        return cls._credential
