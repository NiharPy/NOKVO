from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import StreamingResponse
from app.api.deps import RequireRole
from app.models.user import SuperAdminUser
from app.schemas.organization import OrganizationCreate
from app.db.session import get_db
from app.models.organization import Organization
from app.services.azure_tenant_provisioning_service import AzureTenantProvisioningService
from app.models.tenant_resources import TenantResources
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID
import json
import asyncio

router = APIRouter()

ALLOWED_REGIONS = [
    "centralindia", "southindia", "westindia",
    "eastus", "westus", "westeurope", "southeastasia"
]


def _organization_profile(org: Organization) -> dict:
    return {
        "admin_email": org.admin_email,
        "admin_name": org.admin_name,
        "call_type": org.call_type,
        "language": org.language,
        "plan_type": org.plan_type,
        "stores_pii": org.stores_pii,
        "record_calls": org.record_calls,
        "create_resource_group": org.create_resource_group,
        "plivo_auto_provision": org.plivo_auto_provision,
    }

@router.post("/provision", status_code=status.HTTP_201_CREATED)
async def provision_tenant(
    org_in: OrganizationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(["founder", "engineering"]))
):
    """
    Provision a new tenant environment for an organization.
    Creates Azure Resource Group, Blob prefixes, Key Vault refs,
    Qdrant collection, Redis namespace, and Plivo placeholder.
    Only accessible by founder and engineering roles.
    """
    # Validate region
    region = org_in.region if org_in.region else "centralindia"
    if region not in ALLOWED_REGIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid region '{region}'. Allowed: {', '.join(ALLOWED_REGIONS)}"
        )

    # Create Organization record if not exists
    existing_org = await db.execute(
        select(Organization).where(
            Organization.name == org_in.organization_name,
            Organization.region == region,
            Organization.environment == org_in.environment,
        )
    )
    org = existing_org.scalars().first()

    if not org:
        org = Organization(
            name=org_in.organization_name,
            admin_email=org_in.admin_email,
            admin_name=org_in.admin_name,
            region=region,
            environment=org_in.environment,
            call_type=org_in.call_type,
            language=org_in.language,
            plan_type=org_in.plan_type,
            stores_pii=org_in.stores_pii,
            record_calls=org_in.record_calls,
            create_resource_group=org_in.create_resource_group,
            plivo_auto_provision=org_in.plivo_auto_provision,
            industry=org_in.industry,
            country_code=org_in.country_code
        )
        db.add(org)
        await db.commit()
        await db.refresh(org)

    # Trigger Azure Tenant Provisioning
    try:
        result = await AzureTenantProvisioningService.provision(
            organization_id=org.id,
            organization_name=org.name,
            environment=org.environment,
            region=region,
            industry=org.industry,
            country_code=org.country_code
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Provisioning orchestrator failed: {str(e)}"
        )

    result["organization_profile"] = _organization_profile(org)
    return result


@router.post("/provision/stream")
async def provision_tenant_stream(
    org_in: OrganizationCreate,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(["founder", "engineering"]))
):
    """
    SSE streaming endpoint for real-time provisioning progress.
    Each step emits an event as it starts, completes, or fails.
    """
    region = org_in.region if org_in.region else "centralindia"
    if region not in ALLOWED_REGIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid region '{region}'. Allowed: {', '.join(ALLOWED_REGIONS)}"
        )

    # Create Organization record if not exists
    existing_org = await db.execute(
        select(Organization).where(
            Organization.name == org_in.organization_name,
            Organization.region == region,
            Organization.environment == org_in.environment,
        )
    )
    org = existing_org.scalars().first()

    if not org:
        org = Organization(
            name=org_in.organization_name,
            admin_email=org_in.admin_email,
            admin_name=org_in.admin_name,
            region=region,
            environment=org_in.environment,
            call_type=org_in.call_type,
            language=org_in.language,
            plan_type=org_in.plan_type,
            stores_pii=org_in.stores_pii,
            record_calls=org_in.record_calls,
            create_resource_group=org_in.create_resource_group,
            plivo_auto_provision=org_in.plivo_auto_provision,
            industry=org_in.industry,
            country_code=org_in.country_code
        )
        db.add(org)
        await db.commit()
        await db.refresh(org)

    # Capture org details before the generator runs (db session may close)
    org_id = org.id
    org_name = org.name
    org_env = org.environment
    org_industry = org.industry
    org_country = org.country_code

    async def event_generator():
        queue = asyncio.Queue()

        async def on_step(step_info):
            await queue.put(step_info)

        async def run_provisioning():
            try:
                result = await AzureTenantProvisioningService.provision(
                    organization_id=org_id,
                    organization_name=org_name,
                    environment=org_env,
                    region=region,
                    industry=org_industry,
                    country_code=org_country,
                    on_step=on_step
                )
                result["organization_profile"] = _organization_profile(org)
                await queue.put({"event": "complete", "data": result})
            except Exception as e:
                await queue.put({"event": "error", "data": str(e)})
            await queue.put(None)  # Signal end

        # Start provisioning in background
        task = asyncio.create_task(run_provisioning())

        while True:
            item = await queue.get()
            if item is None:
                break
            if item.get("event") == "complete":
                yield f"event: complete\ndata: {json.dumps(item['data'], default=str)}\n\n"
            elif item.get("event") == "error":
                yield f"event: error\ndata: {json.dumps({'error': item['data']})}\n\n"
            else:
                yield f"event: step\ndata: {json.dumps(item)}\n\n"

        await task

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        }
    )


@router.get("/provision/{organization_id}/status")
async def get_provision_status(
    organization_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(["founder", "engineering"]))
):
    org_res = await db.execute(select(Organization).where(Organization.id == organization_id))
    org = org_res.scalars().first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    tr_res = await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
    tenant_res = tr_res.scalars().first()
    if not tenant_res:
        return {
            "organization_id": organization_id,
            "organization_name": org.name,
            "organization_profile": _organization_profile(org),
            "status": "pending",
            "steps": [],
        }

    return {
        "tenant_id": tenant_res.tenant_id,
        "organization_id": organization_id,
        "organization_name": org.name,
        "organization_profile": _organization_profile(org),
        "status": tenant_res.provisioning_status,
        "azure": {
            "resource_group": tenant_res.azure_resource_group_name,
            "region": tenant_res.azure_region,
        },
        "resources": {
            "qdrant_collection": tenant_res.qdrant_collection_name,
            "qdrant_url_ref": tenant_res.qdrant_url_ref,
            "redis_namespace": tenant_res.redis_namespace,
            "redis_host": tenant_res.redis_host,
            "blob_prefix": tenant_res.blob_prefix,
            "key_vault": tenant_res.key_vault_name,
            "plivo_status": tenant_res.plivo_status,
            "llm_provider": (tenant_res.provider_status or {}).get("llm_provider"),
            "llm_model": (tenant_res.provider_status or {}).get("llm_model"),
            "llm_status": (tenant_res.provider_status or {}).get("llm_status"),
            "llm_system_prompt": (tenant_res.provider_status or {}).get("llm_system_prompt"),
        },
        "steps": tenant_res.provisioning_steps or [],
        "next_steps": [
            "Connect client database",
            "Connect CRM",
            "Upload knowledge documents to blob storage",
            "Assign or connect Plivo number",
        ],
    }
