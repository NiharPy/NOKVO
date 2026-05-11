from azure.mgmt.resource import ResourceManagementClient
from app.core.config import settings
from app.core.azure_auth import AzureAuth

class AzureResourceGroupService:
    @staticmethod
    async def provision_resource_group(rg_name: str, tenant_id: str, org_id: str, environment: str, region: str) -> str:
        """
        Creates an Azure Resource Group for the organization.
        Uses the selected tenant region.
        """
        if not settings.AZURE_SUBSCRIPTION_ID:
            return rg_name # Mock/Skip if not configured
            
        credential = AzureAuth.get_credential()
        
        # Note: ResourceManagementClient is primarily sync in the azure-mgmt-resource package
        # For production async, an executor or the specific aio package should be used.
        # We will wrap it synchronously here but it executes in an async context.
        client = ResourceManagementClient(credential, settings.AZURE_SUBSCRIPTION_ID)
        
        rg_params = {
            "location": region or settings.AZURE_DEFAULT_REGION,
            "tags": {
                "app": "nokvo",
                "organization_id": str(org_id),
                "tenant_id": str(tenant_id),
                "environment": environment,
                "managed_by": "nokvo-platform"
            }
        }
        
        try:
            # Create or update resource group
            client.resource_groups.create_or_update(rg_name, rg_params)
            return rg_name
        except Exception as e:
            raise RuntimeError(f"Failed to create Azure Resource Group: {str(e)}")

    @staticmethod
    async def delete_resource_group(rg_name: str) -> bool:
        """
        Best-effort delete of a resource group. Returns True on success or if the
        RG no longer exists (idempotent). Logs and swallows other errors so that
        cleanup paths never raise; the caller should already be in an error flow.
        """
        if not settings.AZURE_SUBSCRIPTION_ID:
            return True
        try:
            credential = AzureAuth.get_credential()
            client = ResourceManagementClient(credential, settings.AZURE_SUBSCRIPTION_ID)
            client.resource_groups.begin_delete(rg_name)
            return True
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Failed to delete Azure resource group %s during rollback", rg_name
            )
            return False
