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

    @staticmethod
    async def delete_agent_knowledge_document(
        tenant_id: str,
        blob_path: str,
        *,
        blob_prefix: str | None = None,
        document_id: str | None = None,
    ) -> int:
        """Delete the source document blob(s) under this document_id.

        Two-stage cleanup so retry-orphans don't pile up:
          1. Delete the specific blob at ``blob_path`` (what the registry recorded).
          2. If ``document_id`` is provided, ALSO list and delete every
             other blob under ``tenants/{tenant_id}/agent-knowledge/{document_id}/``
             — catches blobs left over from earlier upload attempts that
             succeeded the blob step but failed downstream.

        Returns the number of blobs actually deleted. Best-effort: errors
        on individual blobs are logged but don't abort the operation.
        """
        account_name = settings.AZURE_SHARED_STORAGE_ACCOUNT
        if not account_name:
            print(f"[NOKVO-BLOB] no AZURE_SHARED_STORAGE_ACCOUNT, skipping delete for {blob_path!r}")
            return 0
        container_name = settings.AZURE_SHARED_STORAGE_CONTAINER

        # Compute the per-document_id prefix when we have enough info.
        sweep_prefix: str | None = None
        if document_id:
            tenant_prefix = (blob_prefix or f"tenants/{tenant_id}/").rstrip("/")
            sweep_prefix = f"{tenant_prefix}/agent-knowledge/{document_id}/"

        # Always also try the explicit blob_path even if it isn't under the
        # sweep prefix (legacy uploads may have a different shape).
        explicit_blob_name: str | None = None
        if blob_path:
            if container_name and blob_path.startswith(f"{container_name}/"):
                explicit_blob_name = blob_path[len(container_name) + 1:]
            else:
                explicit_blob_name = blob_path

        account_url = f"https://{account_name}.blob.core.windows.net"
        credential = AzureAuth.get_credential()
        deleted = 0
        try:
            blob_service_client = BlobServiceClient(account_url=account_url, credential=credential)
            container_client = blob_service_client.get_container_client(container_name)

            # Track names we've already deleted so the explicit-path step
            # doesn't double-count when it overlaps with the prefix sweep.
            seen: set[str] = set()

            if sweep_prefix:
                try:
                    for blob in container_client.list_blobs(name_starts_with=sweep_prefix):
                        try:
                            container_client.delete_blob(blob.name)
                            seen.add(blob.name)
                            deleted += 1
                        except Exception as exc:
                            print(f"[NOKVO-BLOB] failed to delete {blob.name}: {exc!r}")
                except Exception as exc:
                    print(f"[NOKVO-BLOB] list under {sweep_prefix!r} failed: {exc!r}")

            if explicit_blob_name and explicit_blob_name not in seen:
                try:
                    container_client.get_blob_client(explicit_blob_name).delete_blob()
                    deleted += 1
                except Exception as exc:
                    # 404 / already-gone is fine; anything else gets logged.
                    if "BlobNotFound" not in str(exc) and "404" not in str(exc):
                        print(f"[NOKVO-BLOB] failed to delete {explicit_blob_name}: {exc!r}")
        except Exception as exc:
            print(f"[NOKVO-BLOB] delete_agent_knowledge_document outer failure: {exc!r}")
            return deleted

        print(
            f"[NOKVO-BLOB] deleted {deleted} blob(s) for document_id={document_id!r} "
            f"under prefix={sweep_prefix!r}"
        )
        return deleted
