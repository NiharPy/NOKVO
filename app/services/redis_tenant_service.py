import os
import redis.asyncio as redis

class RedisTenantService:
    @staticmethod
    async def provision_redis(tenant_id: str) -> str:
        """
        Creates/caches Redis keys for the tenant in Azure Cache for Redis.
        """
        namespace = f"tenant:{tenant_id}"
        
        # Configure Redis Client
        redis_url = os.environ.get("REDIS_URL", "redis://localhost:6379")
        
        try:
            client = redis.from_url(redis_url, decode_responses=True)
            
            # Initialize default keys
            await client.set(f"{namespace}:config", "{}")
            await client.set(f"{namespace}:tool_manifest", "{}")
            await client.set(f"{namespace}:runtime_status", "pending")
            
            await client.aclose()
            return namespace
        except Exception as e:
            raise RuntimeError(f"Failed to provision Redis namespace: {str(e)}")
