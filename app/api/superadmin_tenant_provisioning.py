from fastapi import APIRouter, Depends, HTTPException, status, Request
from fastapi.responses import StreamingResponse
from app.api.deps import RequireRole
from app.core.email_policy import extract_email_domain, normalize_email, validate_work_email
from app.models.user import SuperAdminUser
from app.schemas.organization import OrganizationCreate
from app.db.session import get_db
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.services.azure_tenant_provisioning_service import AzureTenantProvisioningService
from app.models.tenant_resources import TenantResources
from app.models.tenant_usage_event import TenantUsageEvent
from app.models.audit import SuperAdminAuditLog
from app.schemas.tenant_usage import TenantUsageEventCreate
from app.services.tenant_billing_service import TenantBillingService
from app.services.azure_keyvault_service import AzureKeyVaultService
from app.core.azure_auth import AzureAuth
from app.core.config import settings
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select
from uuid import UUID
from decimal import Decimal
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
        "email_domain": org.email_domain,
        "call_type": org.call_type,
        "language": org.language,
        "plan_type": org.plan_type,
        "stores_pii": org.stores_pii,
        "record_calls": org.record_calls,
        "create_resource_group": org.create_resource_group,
        "twilio_auto_provision": org.twilio_auto_provision,
    }


def _tenant_cost_breakdown(tenant: TenantResources | None) -> dict:
    if not tenant:
        return {
            "provisioned_monthly_cost_usd": 0.0,
            "usage_cost_usd": 0.0,
            "total_cost_usd": 0.0,
        }
    provisioned = TenantBillingService.monthly_provisioned_cost(tenant)
    total = Decimal(str(tenant.total_cost_usd or 0))
    usage = total - provisioned
    if usage < 0:
        usage = Decimal("0")
    return {
        "provisioned_monthly_cost_usd": float(provisioned),
        "usage_cost_usd": float(usage),
        "total_cost_usd": float(total),
    }


def _usage_aggregate(events: list[TenantUsageEvent], tenant: TenantResources | None) -> dict:
    llm_input_tokens = sum(event.llm_input_tokens or 0 for event in events)
    llm_output_tokens = sum(event.llm_output_tokens or 0 for event in events)
    llm_api_calls = sum(event.llm_api_calls or 0 for event in events)
    stt_minutes = sum(float(event.stt_minutes or 0) for event in events)
    telephony_minutes = sum(float(event.telephony_minutes or 0) for event in events)
    tts_characters = sum(event.tts_characters or 0 for event in events)
    storage_gb = sum(float(event.storage_gb or 0) for event in events)
    infrastructure_units = sum(event.infrastructure_units or 0 for event in events)
    return {
        "minutes": sum(event.minutes or 0 for event in events),
        "llm_input_tokens": llm_input_tokens,
        "llm_output_tokens": llm_output_tokens,
        "llm_total_tokens": llm_input_tokens + llm_output_tokens,
        "llm_api_calls": llm_api_calls,
        "voice_minutes": telephony_minutes,
        "stt_minutes": stt_minutes,
        "tts_characters": tts_characters,
        "storage_gb": round(storage_gb, 3),
        "infrastructure_units": infrastructure_units + (
            1 if tenant and tenant.qdrant_collection_name else 0
        ) + (
            1 if tenant and tenant.redis_namespace else 0
        ) + (
            1 if tenant and tenant.blob_prefix else 0
        ) + (
            1 if tenant and tenant.key_vault_name else 0
        ),
    }


async def _ensure_organization_admin_user(
    db: AsyncSession,
    org: Organization,
) -> None:
    if not org.admin_email:
        raise HTTPException(status_code=400, detail="Organization admin email is required")

    normalized_admin_email = validate_work_email(org.admin_email)
    email_domain = extract_email_domain(normalized_admin_email)
    org.admin_email = normalized_admin_email
    org.email_domain = email_domain

    result = await db.execute(
        select(OrganizationUser).where(
            OrganizationUser.organization_id == org.id,
            OrganizationUser.email == normalized_admin_email,
        )
    )
    admin_user = result.scalars().first()
    if admin_user:
        admin_user.role = "admin"
        admin_user.full_name = org.admin_name or admin_user.full_name
        if admin_user.status == "disabled":
            admin_user.status = "invited"
        db.add(admin_user)
        return

    db.add(
        OrganizationUser(
            organization_id=org.id,
            email=normalized_admin_email,
            full_name=org.admin_name,
            role="admin",
            status="invited",
            auth_provider="google",
            email_verified=False,
        )
    )


@router.get("")
async def list_tenants(
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(["founder", "engineering", "billing", "readonly"]))
):
    org_rows = await db.execute(select(Organization).order_by(Organization.created_at.desc()))
    organizations = org_rows.scalars().all()

    tenant_rows = await db.execute(select(TenantResources))
    tenant_map = {str(row.organization_id): row for row in tenant_rows.scalars().all()}
    usage_rows = await db.execute(select(TenantUsageEvent))
    usage_map: dict[str, list[TenantUsageEvent]] = {}
    for row in usage_rows.scalars().all():
        usage_map.setdefault(str(row.organization_id), []).append(row)

    items = []
    for org in organizations:
        tenant = tenant_map.get(str(org.id))
        usage = _usage_aggregate(usage_map.get(str(org.id), []), tenant)
        cost_breakdown = _tenant_cost_breakdown(tenant)
        revenue_usd = round(cost_breakdown["total_cost_usd"] * 1.35, 2)
        margin_usd = round(revenue_usd - cost_breakdown["total_cost_usd"], 2)
        items.append({
            "organization_id": str(org.id),
            "organization_name": org.name,
            "organization_profile": _organization_profile(org),
            "environment": org.environment,
            "region": org.region,
            "created_at": org.created_at,
            "tenant_id": tenant.tenant_id if tenant else None,
            "provisioning_status": tenant.provisioning_status if tenant else "pending",
            "usage_minutes": tenant.usage_minutes if tenant else 0,
            "total_cost_usd": float(tenant.total_cost_usd or 0) if tenant else 0.0,
            "billing_status": "overdue" if cost_breakdown["total_cost_usd"] > 0 and (tenant.provisioning_status if tenant else "pending") == "partial" else "current",
            "money_tracker": {
                "revenue_usd": revenue_usd,
                "cost_usd": cost_breakdown["total_cost_usd"],
                "margin_usd": margin_usd,
                "outstanding_balance_usd": round(max(cost_breakdown["usage_cost_usd"], 0), 2),
            },
            "usage_tracker": usage,
            "cost_breakdown": cost_breakdown,
        })

    return {
        "organizations": items,
        "summary": {
            "count": len(items),
            "total_minutes": sum(item["usage_minutes"] for item in items),
            "total_cost_usd": round(sum(item["total_cost_usd"] for item in items), 2),
        },
    }


@router.post("/{organization_id}/usage-events")
async def record_usage_event(
    organization_id: UUID,
    payload: TenantUsageEventCreate,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(["founder", "engineering", "billing"]))
):
    tenant_res_query = await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
    tenant_res = tenant_res_query.scalars().first()
    if not tenant_res:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")

    minutes = payload.minutes
    stt_minutes = payload.stt_minutes
    telephony_minutes = payload.telephony_minutes
    tts_characters = payload.tts_characters
    llm_api_calls = payload.llm_api_calls
    llm_input_tokens = payload.llm_input_tokens
    llm_output_tokens = payload.llm_output_tokens
    storage_gb = payload.storage_gb
    infrastructure_units = payload.infrastructure_units

    usage_cost = TenantBillingService.usage_cost(
        minutes=minutes,
        stt_minutes=stt_minutes,
        telephony_minutes=telephony_minutes,
        tts_characters=tts_characters,
        llm_api_calls=llm_api_calls,
        llm_input_tokens=llm_input_tokens,
        llm_output_tokens=llm_output_tokens,
        storage_gb=storage_gb,
        infrastructure_units=infrastructure_units,
    )

    event = TenantUsageEvent(
        organization_id=organization_id,
        tenant_id=tenant_res.tenant_id,
        event_type=payload.event_type,
        minutes=minutes,
        stt_minutes=stt_minutes,
        telephony_minutes=telephony_minutes,
        tts_characters=tts_characters,
        llm_api_calls=llm_api_calls,
        llm_input_tokens=llm_input_tokens,
        llm_output_tokens=llm_output_tokens,
        storage_gb=storage_gb,
        infrastructure_units=infrastructure_units,
        cost_usd=usage_cost,
        metadata_=payload.metadata or {},
    )
    db.add(event)

    tenant_res.usage_minutes = (tenant_res.usage_minutes or 0) + minutes
    tenant_res.total_cost_usd = Decimal(str(tenant_res.total_cost_usd or 0)) + usage_cost

    db.add(
        SuperAdminAuditLog(
            superadmin_id=current_user.id,
            action="tenant_usage_event_recorded",
            risk_level="low",
            target_type="organization",
            target_id=str(organization_id),
            metadata_={
                "tenant_id": tenant_res.tenant_id,
                "event_type": event.event_type,
                "minutes": minutes,
                "cost_usd": float(usage_cost),
            },
        )
    )
    await db.commit()
    await db.refresh(event)

    return {
        "event_id": str(event.id),
        "organization_id": str(organization_id),
        "tenant_id": tenant_res.tenant_id,
        "minutes": event.minutes,
        "cost_usd": float(event.cost_usd),
        "cost_breakdown": TenantBillingService.event_cost_breakdown(payload.model_dump()),
        "usage_totals": {
            "usage_minutes": tenant_res.usage_minutes,
            "total_cost_usd": float(tenant_res.total_cost_usd or 0),
        },
    }


@router.get("/{organization_id}/usage-events")
async def list_usage_events(
    organization_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(["founder", "engineering", "billing", "readonly"]))
):
    tenant_res_query = await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
    tenant_res = tenant_res_query.scalars().first()
    if not tenant_res:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")

    rows = await db.execute(
        select(TenantUsageEvent)
        .where(TenantUsageEvent.organization_id == organization_id)
        .order_by(TenantUsageEvent.created_at.desc())
    )
    events = rows.scalars().all()

    return {
        "organization_id": str(organization_id),
        "tenant_id": tenant_res.tenant_id,
        "cost_breakdown": _tenant_cost_breakdown(tenant_res),
        "usage_minutes": tenant_res.usage_minutes or 0,
        "events": [
            {
                "id": str(event.id),
                "event_type": event.event_type,
                "minutes": event.minutes,
                "stt_minutes": float(event.stt_minutes or 0),
                "telephony_minutes": float(event.telephony_minutes or 0),
                "tts_characters": event.tts_characters,
                "llm_api_calls": event.llm_api_calls,
                "llm_input_tokens": event.llm_input_tokens,
                "llm_output_tokens": event.llm_output_tokens,
                "storage_gb": float(event.storage_gb or 0),
                "infrastructure_units": event.infrastructure_units,
                "cost_usd": float(event.cost_usd or 0),
                "metadata": event.metadata_ or {},
                "created_at": event.created_at,
            }
            for event in events
        ],
    }


@router.post("/{organization_id}/usage/recalculate")
async def recalculate_usage_totals(
    organization_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(["founder", "engineering", "billing"]))
):
    tenant_res_query = await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
    tenant_res = tenant_res_query.scalars().first()
    if not tenant_res:
        raise HTTPException(status_code=404, detail="Tenant resources not found for organization")

    rows = await db.execute(select(TenantUsageEvent).where(TenantUsageEvent.organization_id == organization_id))
    events = rows.scalars().all()

    total_minutes = sum(event.minutes or 0 for event in events)
    usage_cost = sum(Decimal(str(event.cost_usd or 0)) for event in events)
    provisioned_cost = TenantBillingService.monthly_provisioned_cost(tenant_res)

    tenant_res.usage_minutes = total_minutes
    tenant_res.total_cost_usd = provisioned_cost + usage_cost
    await db.commit()

    return {
        "organization_id": str(organization_id),
        "tenant_id": tenant_res.tenant_id,
        "usage_minutes": tenant_res.usage_minutes,
        "provisioned_monthly_cost_usd": float(provisioned_cost),
        "usage_cost_usd": float(usage_cost),
        "total_cost_usd": float(tenant_res.total_cost_usd or 0),
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
    Qdrant collection, Redis namespace, and Twilio provisioning.
    Only accessible by founder and engineering roles.
    """
    # Validate region
    region = org_in.region if org_in.region else "centralindia"
    if region not in ALLOWED_REGIONS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid region '{region}'. Allowed: {', '.join(ALLOWED_REGIONS)}"
        )
    admin_email = validate_work_email(org_in.admin_email)
    admin_domain = extract_email_domain(admin_email)

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
            admin_email=admin_email,
            admin_name=org_in.admin_name,
            email_domain=admin_domain,
            region=region,
            environment=org_in.environment,
            call_type=org_in.call_type,
            language=org_in.language,
            plan_type=org_in.plan_type,
            stores_pii=org_in.stores_pii,
            record_calls=org_in.record_calls,
            create_resource_group=org_in.create_resource_group,
            twilio_auto_provision=org_in.twilio_auto_provision,
            industry=org_in.industry,
            country_code=org_in.country_code
        )
        db.add(org)
        await db.commit()
        await db.refresh(org)
    else:
        if org.admin_email and normalize_email(org.admin_email) != admin_email:
            raise HTTPException(
                status_code=409,
                detail="Organization already exists with a different admin work email",
            )
        org.admin_email = admin_email
        org.admin_name = org_in.admin_name
        org.email_domain = admin_domain
        db.add(org)

    await _ensure_organization_admin_user(db, org)
    await db.commit()

    # Trigger Azure Tenant Provisioning
    try:
        result = await AzureTenantProvisioningService.provision(
            organization_id=org.id,
            organization_name=org.name,
            environment=org.environment,
            region=region,
            industry=org.industry,
            country_code=org.country_code,
            language=org.language,
            twilio_auto_provision=org.twilio_auto_provision,
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
    admin_email = validate_work_email(org_in.admin_email)
    admin_domain = extract_email_domain(admin_email)

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
            admin_email=admin_email,
            admin_name=org_in.admin_name,
            email_domain=admin_domain,
            region=region,
            environment=org_in.environment,
            call_type=org_in.call_type,
            language=org_in.language,
            plan_type=org_in.plan_type,
            stores_pii=org_in.stores_pii,
            record_calls=org_in.record_calls,
            create_resource_group=org_in.create_resource_group,
            twilio_auto_provision=org_in.twilio_auto_provision,
            industry=org_in.industry,
            country_code=org_in.country_code
        )
        db.add(org)
        await db.commit()
        await db.refresh(org)
    else:
        if org.admin_email and normalize_email(org.admin_email) != admin_email:
            raise HTTPException(
                status_code=409,
                detail="Organization already exists with a different admin work email",
            )
        org.admin_email = admin_email
        org.admin_name = org_in.admin_name
        org.email_domain = admin_domain
        db.add(org)

    await _ensure_organization_admin_user(db, org)
    await db.commit()

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
                    language=org.language,
                    twilio_auto_provision=org.twilio_auto_provision,
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
        "usage_minutes": tenant_res.usage_minutes or 0,
        "cost_breakdown": _tenant_cost_breakdown(tenant_res),
        "azure": {
            "resource_group": tenant_res.azure_resource_group_name,
            "region": tenant_res.azure_region,
        },
        "resources": {
            "qdrant_collection": tenant_res.qdrant_collection_name,
            "qdrant_url_ref": tenant_res.qdrant_url_ref,
            "redis_namespace": tenant_res.redis_namespace,
            "redis_host": tenant_res.redis_host,
            "redis_mode": (tenant_res.provider_status or {}).get("redis_mode", "shared"),
            "blob_prefix": tenant_res.blob_prefix,
            "key_vault": tenant_res.key_vault_name,
            "twilio_status": tenant_res.twilio_status,
            "twilio_provider": (tenant_res.provider_status or {}).get("twilio_provider"),
            "twilio_subaccount_id": (tenant_res.provider_status or {}).get("twilio_subaccount_id"),
            "twilio_error": (tenant_res.provider_status or {}).get("twilio_error"),
            "stt_provider": (tenant_res.provider_status or {}).get("stt_provider"),
            "stt_model": (tenant_res.provider_status or {}).get("stt_model"),
            "stt_endpoint": (tenant_res.provider_status or {}).get("stt_endpoint"),
            "stt_status": (tenant_res.provider_status or {}).get("stt_status"),
            "tts_provider": (tenant_res.provider_status or {}).get("tts_provider"),
            "tts_model": (tenant_res.provider_status or {}).get("tts_model"),
            "tts_status": (tenant_res.provider_status or {}).get("tts_status"),
            "tts_voice": (tenant_res.provider_status or {}).get("tts_voice"),
            "llm_provider": (tenant_res.provider_status or {}).get("llm_provider"),
            "llm_model": (tenant_res.provider_status or {}).get("llm_model"),
            "llm_status": (tenant_res.provider_status or {}).get("llm_status"),
            "llm_api_key_ref": (tenant_res.provider_status or {}).get("llm_api_key_ref"),
            "llm_api_key_stored": (tenant_res.provider_status or {}).get("llm_api_key_stored"),
            "llm_error": (tenant_res.provider_status or {}).get("llm_error"),
            "llm_system_prompt": (tenant_res.provider_status or {}).get("llm_system_prompt"),
        },
        "steps": tenant_res.provisioning_steps or [],
        "next_steps": [
            "Connect client database",
            "Connect CRM",
            "Upload knowledge documents to blob storage",
            "Assign or connect Twilio number",
        ],
    }


@router.post("/{organization_id}/reprovision-llm-key", status_code=status.HTTP_200_OK)
async def reprovision_llm_key(
    organization_id: UUID,
    db: AsyncSession = Depends(get_db),
    current_user: SuperAdminUser = Depends(RequireRole(["founder", "engineering"])),
):
    """
    Reads the Azure OpenAI API key from the existing resource group and writes it
    to Key Vault under the correct secret name. Use this when provisioning succeeded
    but Key Vault storage was skipped (e.g. AZURE_SHARED_KEY_VAULT_NAME was unset).
    """
    org_res = await db.execute(select(Organization).where(Organization.id == organization_id))
    org = org_res.scalars().first()
    if not org:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Organization not found")

    tr_res = await db.execute(select(TenantResources).where(TenantResources.organization_id == organization_id))
    tenant_res = tr_res.scalars().first()
    if not tenant_res:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tenant resources not found — run provisioning first")

    provider = tenant_res.provider_status or {}
    account_name = provider.get("llm_account")
    rg_name = tenant_res.azure_resource_group_name
    secret_name = provider.get("llm_api_key_ref")

    if not account_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="llm_account not recorded in provider_status — cannot locate Azure OpenAI resource")
    if not rg_name:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="azure_resource_group_name is not set on tenant resources")
    if not settings.AZURE_SUBSCRIPTION_ID:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="AZURE_SUBSCRIPTION_ID is not configured")
    if not settings.AZURE_SHARED_KEY_VAULT_NAME:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="AZURE_SHARED_KEY_VAULT_NAME is not configured")

    if not secret_name:
        secret_name = AzureKeyVaultService._secret_name(tenant_res.tenant_id, "llm-api-key")

    try:
        from azure.mgmt.cognitiveservices import CognitiveServicesManagementClient
        credential = AzureAuth.get_credential()
        cog_client = CognitiveServicesManagementClient(credential, settings.AZURE_SUBSCRIPTION_ID)
        keys = cog_client.accounts.list_keys(
            resource_group_name=rg_name,
            account_name=account_name,
        )
        llm_api_key = getattr(keys, "key1", None) or getattr(keys, "primary_key", None)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to fetch API key from Azure OpenAI resource '{account_name}': {exc}",
        )

    if not llm_api_key:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Azure OpenAI resource '{account_name}' returned no API key")

    await AzureKeyVaultService.set_secret_value(
        secret_name,
        llm_api_key,
        tenant_res.tenant_id,
        "llm_api_key",
    )

    updated_status = dict(provider)
    updated_status["llm_api_key_ref"] = secret_name
    updated_status["llm_api_key_stored"] = True
    updated_status.pop("llm_error", None)
    tenant_res.provider_status = updated_status
    db.add(tenant_res)
    await db.commit()

    return {
        "status": "ok",
        "secret_name": secret_name,
        "vault": settings.AZURE_SHARED_KEY_VAULT_NAME,
        "account_name": account_name,
        "resource_group": rg_name,
    }
