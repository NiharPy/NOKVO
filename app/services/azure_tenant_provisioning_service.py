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
from app.services.twilio_service import TwilioService
from app.services.soniox_stt_service import SonioxSTTService
from app.services.sarvam_tts_service import SarvamTTSService
from app.services.azure_ai_service import AzureAIService
from app.services.tenant_billing_service import TenantBillingService

class AzureTenantProvisioningService:
    @staticmethod
    def _generate_slug(name: str) -> str:
        slug = re.sub(r'[^a-z0-9\s-]', '', name.lower())
        slug = re.sub(r'[\s-]+', '-', slug).strip('-')
        return slug[:24]

    @staticmethod
    async def provision(
        organization_id: uuid.UUID,
        organization_name: str,
        environment: str,
        region: str,
        industry: str = "Technology",
        country_code: str = "IN",
        language: str | None = None,
        twilio_auto_provision: bool = False,
        on_step=None,
    ) -> dict:
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
                        "redis_mode": existing.provider_status.get("redis_mode") if existing.provider_status else "shared",
                        "blob_prefix": existing.blob_prefix,
                        "key_vault": existing.key_vault_name,
                        "twilio_status": existing.twilio_status,
                        "twilio_subaccount_id": existing.provider_status.get("twilio_subaccount_id") if existing.provider_status else None,
                        "stt_provider": existing.provider_status.get("stt_provider") if existing.provider_status else None,
                        "stt_model": existing.provider_status.get("stt_model") if existing.provider_status else None,
                        "stt_endpoint": existing.provider_status.get("stt_endpoint") if existing.provider_status else None,
                        "stt_status": existing.provider_status.get("stt_status") if existing.provider_status else None,
                        "tts_provider": existing.provider_status.get("tts_provider") if existing.provider_status else None,
                        "tts_model": existing.provider_status.get("tts_model") if existing.provider_status else None,
                        "tts_status": existing.provider_status.get("tts_status") if existing.provider_status else None,
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

            def get_step_message(name):
                for s in steps:
                    if s["name"] == name:
                        return s.get("message")
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
            
            # 6. Twilio
            twilio_res = await run_step(
                "twilio_subaccount",
                lambda: TwilioService.provision_subaccount(
                    tenant_id=tenant_id,
                    organization_name=organization_name,
                    secret_refs=kv_res,
                    auto_provision=twilio_auto_provision or settings.TWILIO_AUTO_PROVISION,
                ),
            )
            
            # 7. Soniox STT
            stt_res = await run_step(
                "soniox_stt",
                lambda: SonioxSTTService.provision_stt(
                    tenant_id=tenant_id,
                    language=language,
                    secret_refs=kv_res,
                ),
            )

            # 8. Sarvam TTS
            tts_res = await run_step(
                "sarvam_tts",
                lambda: SarvamTTSService.provision_tts(
                    tenant_id=tenant_id,
                    language=language,
                    secret_refs=kv_res,
                ),
            )

            # 9. Azure OpenAI GPT-4o-mini
            ai_res = await run_step("azure_openai_gpt4o_mini", lambda: AzureAIService.provision_ai_resource(
                rg_name=rg_name, tenant_id=tenant_id, slug=slug, region=region,
                organization_name=organization_name, industry=industry, country_code=country_code,
                secret_refs=kv_res,
            ))
            
            # === Determine final status ===
            has_failures = any(s["status"] == "failed" for s in steps)
            final_status = "partial" if has_failures else "success"
            
            # === Build provider_status ===
            provider_status = {
                "qdrant_status": "provisioned" if qdrant_res else "pending",
                "qdrant_collection": qdrant_res,
                "qdrant_url_ref": QdrantService.cluster_ref() if qdrant_res else None,
                "llm_status": ai_res.get("status", "pending") if ai_res else ("failed" if get_step_status("azure_openai_gpt4o_mini") == "failed" else "pending"),
                "llm_provider": "azure_openai",
                "llm_model": ai_res.get("model") if ai_res else "gpt-4.1-mini",
                "llm_endpoint": ai_res.get("endpoint") if ai_res else None,
                "llm_account": ai_res.get("account_name") if ai_res else None,
                "llm_api_key_ref": ai_res.get("api_key_ref") if ai_res else (kv_res.get("llm_api_key", {}) if kv_res else {}).get("secret_name"),
                "llm_api_key_stored": ai_res.get("api_key_stored", False) if ai_res else False,
                "llm_system_prompt": ai_res.get("system_prompt") if ai_res else None,
                "llm_error": get_step_message("azure_openai_gpt4o_mini") if get_step_status("azure_openai_gpt4o_mini") == "failed" else None,
                "stt_status": stt_res.get("stt_status", "pending_credentials") if stt_res else "pending_credentials",
                "stt_provider": stt_res.get("stt_provider") if stt_res else "soniox",
                "stt_model": stt_res.get("stt_model") if stt_res else settings.SONIOX_STT_MODEL,
                "stt_endpoint": stt_res.get("stt_endpoint") if stt_res else settings.SONIOX_STT_WEBSOCKET_URL,
                "stt_audio_format": stt_res.get("stt_audio_format") if stt_res else settings.SONIOX_STT_AUDIO_FORMAT,
                "stt_transport": stt_res.get("stt_transport") if stt_res else "websocket",
                "stt_api_key_ref": stt_res.get("stt_api_key_ref") if stt_res else None,
                "stt_language_hints": stt_res.get("stt_language_hints") if stt_res else [],
                "tts_status": tts_res.get("tts_status", "pending_credentials") if tts_res else "pending_credentials",
                "tts_provider": tts_res.get("tts_provider") if tts_res else "sarvam",
                "tts_model": tts_res.get("tts_model") if tts_res else settings.SARVAM_TTS_MODEL,
                "tts_api_key_ref": tts_res.get("tts_api_key_ref") if tts_res else None,
                "tts_rest_endpoint": tts_res.get("tts_rest_endpoint") if tts_res else settings.SARVAM_TTS_REST_URL,
                "tts_stream_endpoint": tts_res.get("tts_stream_endpoint") if tts_res else settings.SARVAM_TTS_STREAM_URL,
                "tts_target_language_code": tts_res.get("tts_target_language_code") if tts_res else (language or "en-IN"),
                "tts_speaker": tts_res.get("tts_speaker") if tts_res else settings.SARVAM_TTS_SPEAKER,
                "tts_sample_rate": tts_res.get("tts_sample_rate") if tts_res else settings.SARVAM_TTS_SAMPLE_RATE,
                "tts_audio_format": tts_res.get("tts_audio_format") if tts_res else settings.SARVAM_TTS_AUDIO_FORMAT,
                "twilio_provider": twilio_res.get("twilio_provider") if twilio_res else None,
                "twilio_subaccount_id": twilio_res.get("subaccount_id") if twilio_res else None,
                "twilio_subaccount_status": twilio_res.get("subaccount_status") if twilio_res else ("failed" if get_step_status("twilio_subaccount") == "failed" else "pending"),
                "twilio_error": twilio_res.get("error") if twilio_res else get_step_message("twilio_subaccount"),
                "crm_status": "not_connected",
                "db_status": "not_connected",
                "redis_mode": "shared",
                "redis_dedicated": False,
                "redis_host": None,
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
                record.redis_host = None
                record.redis_port = None
                if blob_res:
                    record.storage_account_name = blob_res.get("storage_account_name")
                    record.storage_container_name = blob_res.get("container_name")
                    record.blob_prefix = blob_res.get("blob_prefix")
                if kv_res:
                    record.secret_refs = kv_res
                if twilio_res:
                    record.twilio_status = twilio_res.get("phone_number_status", "failed")
                record.total_cost_usd = TenantBillingService.monthly_provisioned_cost(record)
            else:
                record = TenantResources(
                    tenant_id=tenant_id,
                    organization_id=organization_id,
                    azure_resource_group_name=rg_res if rg_res else rg_name,
                    azure_region=region,
                    qdrant_collection_name=qdrant_res,
                    qdrant_url_ref=QdrantService.cluster_ref() if qdrant_res else None,
                    redis_namespace=redis_res,
                    redis_host=None,
                    redis_port=None,
                    storage_account_name=blob_res.get("storage_account_name") if blob_res else None,
                    storage_container_name=blob_res.get("container_name") if blob_res else None,
                    blob_prefix=blob_res.get("blob_prefix") if blob_res else None,
                    key_vault_name=settings.AZURE_SHARED_KEY_VAULT_NAME,
                    secret_refs=kv_res or {},
                    twilio_status=twilio_res.get("phone_number_status") if twilio_res else "failed",
                    provider_status=provider_status,
                    provisioning_status=final_status,
                    provisioning_steps=steps,
                    cleanup_required=has_failures,
                    usage_minutes=0,
                    total_cost_usd=TenantBillingService.monthly_provisioned_cost(None),
                )
                db.add(record)
                await db.flush()
                record.total_cost_usd = TenantBillingService.monthly_provisioned_cost(record)
                
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
                    "redis_mode": provider_status.get("redis_mode"),
                    "blob_prefix": record.blob_prefix,
                    "key_vault": record.key_vault_name,
                    "twilio_status": record.twilio_status,
                    "twilio_provider": provider_status.get("twilio_provider"),
                    "twilio_subaccount_id": provider_status.get("twilio_subaccount_id"),
                    "stt_provider": provider_status.get("stt_provider"),
                    "stt_model": provider_status.get("stt_model"),
                    "stt_endpoint": provider_status.get("stt_endpoint"),
                    "stt_status": provider_status.get("stt_status"),
                    "tts_provider": provider_status.get("tts_provider"),
                    "tts_model": provider_status.get("tts_model"),
                    "tts_status": provider_status.get("tts_status"),
                    "tts_speaker": provider_status.get("tts_speaker"),
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
                    "Provide Sarvam API key if TTS is pending",
                    "Provide Soniox API key if STT is pending",
                    "Assign or connect Twilio number"
                ]
            }
