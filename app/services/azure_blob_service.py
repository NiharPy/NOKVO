import os
from azure.storage.blob import BlobServiceClient
from app.core.config import settings
from app.core.azure_auth import AzureAuth

class AzureBlobService:
    @staticmethod
    async def provision_blob_storage(tenant_id: str) -> dict:
        """
        Creates blob folder prefixes for the tenant in Azure Blob Storage.
        Returns the storage configuration dictionary.
        """
        prefix = f"tenants/{tenant_id}/"
        account_name = settings.AZURE_SHARED_STORAGE_ACCOUNT
        container_name = settings.AZURE_SHARED_STORAGE_CONTAINER
        
        result = {
            "storage_account_name": account_name,
            "container_name": container_name,
            "blob_prefix": prefix
        }
        
        if not account_name:
            # Skip actual provisioning if no account is configured
            return result
            
        account_url = f"https://{account_name}.blob.core.windows.net"
        credential = AzureAuth.get_credential()
            
        try:
            blob_service_client = BlobServiceClient(account_url=account_url, credential=credential)
            container_client = blob_service_client.get_container_client(container_name)
            
            # Create container if it doesn't exist
            if not container_client.exists():
                container_client.create_container()
                
            # Create prefix markers
            folders = ["tools/", "documents/", "recordings/", "exports/", "logs/"]
            for folder in folders:
                blob_client = container_client.get_blob_client(f"{prefix}{folder}.keep")
                blob_client.upload_blob(b"", overwrite=True)
                
            return result
        except Exception as e:
            raise RuntimeError(f"Failed to provision Azure Blob prefixes: {str(e)}")
