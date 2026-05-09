import re
from azure.storage.blob import BlobServiceClient, ContentSettings
from app.core.config import settings
from app.core.azure_auth import AzureAuth

class AzureBlobService:
    @staticmethod
    def _safe_blob_part(value: str) -> str:
        value = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip())
        return value.strip(".-") or "document"

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

    @staticmethod
    async def upload_agent_knowledge_document(
        tenant_id: str,
        blob_prefix: str | None,
        document_id: str,
        filename: str,
        content: bytes,
        content_type: str | None = None,
    ) -> dict:
        prefix = blob_prefix or f"tenants/{tenant_id}/"
        account_name = settings.AZURE_SHARED_STORAGE_ACCOUNT
        container_name = settings.AZURE_SHARED_STORAGE_CONTAINER
        safe_filename = AzureBlobService._safe_blob_part(filename)
        blob_name = f"{prefix.rstrip('/')}/agent-knowledge/{document_id}/{safe_filename}"
        result = {
            "storage_account_name": account_name,
            "container_name": container_name,
            "blob_name": blob_name,
            "blob_path": f"{container_name}/{blob_name}" if container_name else blob_name,
            "content_type": content_type or "application/octet-stream",
            "size_bytes": len(content),
        }
        if not account_name:
            return result

        account_url = f"https://{account_name}.blob.core.windows.net"
        credential = AzureAuth.get_credential()
        try:
            blob_service_client = BlobServiceClient(account_url=account_url, credential=credential)
            container_client = blob_service_client.get_container_client(container_name)
            if not container_client.exists():
                container_client.create_container()
            blob_client = container_client.get_blob_client(blob_name)
            blob_client.upload_blob(
                content,
                overwrite=True,
                content_settings=ContentSettings(content_type=result["content_type"]),
            )
            return result
        except Exception as e:
            raise RuntimeError(f"Failed to upload Agent Knowledge document: {str(e)}")
