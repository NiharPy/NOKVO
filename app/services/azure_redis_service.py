from azure.mgmt.redis import RedisManagementClient
from azure.mgmt.redis.models import RedisCreateParameters, Sku
from app.core.config import settings
from app.core.azure_auth import AzureAuth

class AzureRedisService:
    @staticmethod
    async def provision_redis_instance(rg_name: str, tenant_id: str, slug: str, region: str) -> dict:
        """
        Provisions a dedicated Azure Cache for Redis (Basic C0) for the tenant.
        WARNING: This operation can take 15-20 minutes on Azure.
        We initiate the creation but in a production environment, this should be an async task.
        """
        if not settings.AZURE_SUBSCRIPTION_ID:
            return {
                "redis_name": f"redis-{slug}",
                "status": "skipped",
                "host": "localhost"
            }

        credential = AzureAuth.get_credential()
        client = RedisManagementClient(credential, settings.AZURE_SUBSCRIPTION_ID)

        redis_name = f"nokvo-redis-{slug}"[:63] # Max 63 chars

        try:
            # We use begin_create which returns a poller. 
            # We DO NOT wait for result() because it takes 15-20 minutes.
            client.redis.begin_create(
                resource_group_name=rg_name,
                name=redis_name,
                parameters=RedisCreateParameters(
                    location=region,
                    sku=Sku(name="Basic", family="C", capacity=0), # Cheapest C0 tier
                    tags={
                        "app": "nokvo",
                        "tenant_id": tenant_id,
                        "managed_by": "nokvo-platform"
                    },
                    enable_non_ssl_port=True
                )
            )
            
            # Predict host name (standard Azure format)
            host_name = f"{redis_name}.redis.cache.windows.net"

            return {
                "redis_name": redis_name,
                "host": host_name,
                "port": 6380, # Default SSL port
                "status": "creating",
                "message": "Azure Redis creation initiated. Will be ready in ~15 mins."
            }

        except Exception as e:
            raise RuntimeError(f"Failed to provision Azure Redis: {str(e)}")
