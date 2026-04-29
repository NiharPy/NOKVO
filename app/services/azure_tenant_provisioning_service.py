import uuid
import re
from typing import Optional
from app.db.session import AsyncSessionLocal
from app.models.tenant_resources import TenantResources
from sqlalchemy import select
from app.core.config import settings

from app.services.azure_resource_group_service import AzureResourceGroupService
from app.services.qdrant_service import QdrantService
from app.services.redis_tenant_service import RedisTenantService
from app.services.azure_blob_service import AzureBlobService
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.services.plivo_service import PlivoService
from app.services.azure_redis_service import AzureRedisService
from app.services.azure_ai_service import AzureAIService

class AzureTenantProvisioningService:
    @staticmethod
    def _generate_slug(name: str) -> str:
        slug = re.sub(r'[^a-z0-9\s-]', '', name.lower())
        slug = re.sub(r'[\s-]+', '-', slug).strip('-')
        return slug[:24]

    @staticmethod
    async def provision(organization_id: uuid.UUID, organization_name: str, environment: str, region: str, industry: str = "Technology", country_code: str = "IN", on_step=None) -> dict:
        async with AsyncSessionLocal() as db:
            # Idempotency check
            res = await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
            existing = res.scalars().first()
            if existing and existing.provisioning_status == "success":
                return {
                    "tenant_id": existing.tenant_id,
                    "organization_id": str(existing.organization_id),
                    "status": existing.provisioning_status,
                    "azure": {
                        "resource_group": existing.azure_resource_group_name,
                        "region": existing.azure_region
                    },
                    "resources": {
                        "qdrant_collection": existing.qdrant_collection_name,
                        "qdrant_url_ref": existing.qdrant_url_ref,
                        "redis_namespace": existing.redis_namespace,
                        "redis_host": existing.redis_host,
                        "blob_prefix": existing.blob_prefix,
                        "key_vault": existing.key_vault_name,
                        "plivo_status": existing.plivo_status,
                        "llm_provider": existing.provider_status.get("llm_provider") if existing.provider_status else None,
                        "llm_model": existing.provider_status.get("llm_model") if existing.provider_status else None,
                        "llm_endpoint": existing.provider_status.get("llm_endpoint") if existing.provider_status else None,
                    },
                    "steps": existing.provisioning_steps,
                    "next_steps": []
                }
            
            tenant_id = existing.tenant_id if existing else str(uuid.uuid4())
            slug = AzureTenantProvisioningService._generate_slug(organization_name)
            rg_name = f"rg-nokvo-{slug}-{environment}"
            
            steps = list(existing.provisioning_steps) if existing and existing.provisioning_steps else []
            
            def get_step_status(name):
                for s in steps:
                    if s["name"] == name:
                        return s["status"]
                return None
            
            async def run_step(name, func):
                if get_step_status(name) == "success":
                    if on_step:
                        await on_step({"name": name, "status": "skipped", "message": "Already completed."})
                    return None
                try:
                    if on_step:
                        await on_step({"name": name, "status": "running", "message": f"Provisioning {name}..."})
                    result = await func()
                    steps[:] = [s for s in steps if s["name"] != name]
                    step_info = {"name": name, "status": "success", "message": f"{name} provisioned successfully."}
                    steps.append(step_info)
                    if on_step:
                        await on_step(step_info)
                    return result
                except Exception as e:
                    steps[:] = [s for s in steps if s["name"] != name]
                    step_info = {"name": name, "status": "failed", "message": str(e)}
                    steps.append(step_info)
                    if on_step:
                        await on_step(step_info)
                    return None
            
            # === Execute all provisioning steps ===
            
            # 1. Resource Group
            rg_res = await run_step(
                "resource_group",
                lambda: AzureResourceGroupService.provision_resource_group(
                    rg_name, tenant_id, str(organization_id), environment, region
                ),
            )
            
            # 2. Blob Storage
            blob_res = await run_step("blob_prefixes", lambda: AzureBlobService.provision_blob_storage(tenant_id))
            
            # 3. Key Vault Refs
            kv_res = await run_step("key_vault_secret_refs", lambda: AzureKeyVaultService.provision_secret_refs(tenant_id))
            
            # 4. Qdrant
            qdrant_res = await run_step("qdrant_collection", lambda: QdrantService.provision_collection(tenant_id))
            
            # 5. Redis namespace (local/shared)
            redis_res = await run_step("redis_namespace", lambda: RedisTenantService.provision_redis(tenant_id))
            
            # 6. Plivo
            plivo_res = await run_step("plivo_placeholder", lambda: PlivoService.provision_plivo(tenant_id))
            
            # 7. Dedicated Azure Redis (non-blocking)
            azure_redis_res = await run_step("azure_redis_instance", lambda: AzureRedisService.provision_redis_instance(rg_name, tenant_id, slug, region))

            # 8. Azure OpenAI GPT-4o-mini
            ai_res = await run_step("azure_openai_gpt4o_mini", lambda: AzureAIService.provision_ai_resource(
                rg_name=rg_name, tenant_id=tenant_id, slug=slug, region=region,
                organization_name=organization_name, industry=industry, country_code=country_code
            ))
            
            # === Determine final status ===
            has_failures = any(s["status"] == "failed" for s in steps)
            final_status = "partial" if has_failures else "success"
            
            # === Build provider_status ===
            provider_status = {
                "qdrant_status": "provisioned" if qdrant_res else "pending",
                "qdrant_collection": qdrant_res,
                "qdrant_url_ref": QdrantService.cluster_ref() if qdrant_res else None,
                "llm_status": ai_res.get("status", "pending") if ai_res else "pending",
                "llm_provider": "azure_openai",
                "llm_model": ai_res.get("model") if ai_res else "gpt-4.1-mini",
                "llm_endpoint": ai_res.get("endpoint") if ai_res else None,
                "llm_account": ai_res.get("account_name") if ai_res else None,
                "llm_system_prompt": ai_res.get("system_prompt") if ai_res else None,
                "stt_status": "pending_key",
                "tts_status": "pending_key",
                "crm_status": "not_connected",
                "db_status": "not_connected",
                "redis_dedicated": True if azure_redis_res else False,
                "redis_host": azure_redis_res.get("host") if azure_redis_res else None,
            }
            
            # === Save or update record ===
            if existing:
                record = existing
                record.provisioning_status = final_status
                record.provisioning_steps = steps
                record.cleanup_required = has_failures
                record.provider_status = provider_status
                if rg_res: record.azure_resource_group_name = rg_res
                if qdrant_res: record.qdrant_collection_name = qdrant_res
                if qdrant_res: record.qdrant_url_ref = QdrantService.cluster_ref()
                if redis_res: record.redis_namespace = redis_res
                if azure_redis_res:
                    record.redis_host = azure_redis_res.get("host")
                    record.redis_port = azure_redis_res.get("port")
                if blob_res:
                    record.storage_account_name = blob_res.get("storage_account_name")
                    record.storage_container_name = blob_res.get("container_name")
                    record.blob_prefix = blob_res.get("blob_prefix")
                if kv_res:
                    record.secret_refs = kv_res
                if plivo_res:
                    record.plivo_status = plivo_res.get("phone_number_status", "failed")
            else:
                record = TenantResources(
                    tenant_id=tenant_id,
                    organization_id=organization_id,
                    azure_resource_group_name=rg_res if rg_res else rg_name,
                    azure_region=region,
                    qdrant_collection_name=qdrant_res,
                    qdrant_url_ref=QdrantService.cluster_ref() if qdrant_res else None,
                    redis_namespace=redis_res,
                    redis_host=azure_redis_res.get("host") if azure_redis_res else None,
                    redis_port=azure_redis_res.get("port") if azure_redis_res else None,
                    storage_account_name=blob_res.get("storage_account_name") if blob_res else None,
                    storage_container_name=blob_res.get("container_name") if blob_res else None,
                    blob_prefix=blob_res.get("blob_prefix") if blob_res else None,
                    key_vault_name=settings.AZURE_SHARED_KEY_VAULT_NAME,
                    secret_refs=kv_res or {},
                    plivo_status=plivo_res.get("phone_number_status") if plivo_res else "failed",
                    provider_status=provider_status,
                    provisioning_status=final_status,
                    provisioning_steps=steps,
                    cleanup_required=has_failures
                )
                db.add(record)
                
            steps.append({"name": "postgres_record", "status": "success", "message": "Record saved to DB."})
            await db.commit()
            
            return {
                "tenant_id": tenant_id,
                "organization_id": str(organization_id),
                "status": final_status,
                "azure": {
                    "resource_group": record.azure_resource_group_name,
                    "region": record.azure_region
                },
                "resources": {
                    "qdrant_collection": record.qdrant_collection_name,
                    "qdrant_status": provider_status.get("qdrant_status"),
                    "qdrant_url_ref": record.qdrant_url_ref,
                    "redis_namespace": record.redis_namespace,
                    "redis_host": record.redis_host,
                    "blob_prefix": record.blob_prefix,
                    "key_vault": record.key_vault_name,
                    "plivo_status": record.plivo_status,
                    "llm_provider": "Azure OpenAI",
                    "llm_model": provider_status.get("llm_model"),
                    "llm_endpoint": provider_status.get("llm_endpoint"),
                    "llm_status": provider_status.get("llm_status"),
                    "llm_system_prompt": provider_status.get("llm_system_prompt"),
                },
                "steps": steps,
                "next_steps": [
                    "Connect client database",
                    "Connect CRM",
                    "Upload knowledge documents to blob storage",
                    "Assign or connect Plivo number"
                ]
            }
