import pytest
import pytest_asyncio
import uuid
from unittest.mock import patch, AsyncMock, MagicMock
from httpx import ASGITransport, AsyncClient

from app.main import app as fastapi_app
from app.db import session as db_session
from app.models.user import SuperAdminUser
from app.models.session import SuperAdminSession
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.core.security import get_password_hash, create_access_token
from app.services.qdrant_service import QdrantService
from sqlalchemy import select, delete
import datetime


# ── Helpers ──────────────────────────────────────────────────────────────

FOUNDER_EMAIL = "test_founder_provision@nokvo.ai"
ENGINEERING_EMAIL = "test_eng_provision@nokvo.ai"
SUPPORT_EMAIL = "test_support_provision@nokvo.ai"
PASSWORD = "TestPassword123!"


async def create_test_user(email, role):
    async with db_session.AsyncSessionLocal() as db:
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == email))
        user = res.scalars().first()
        if user:
            return user
        user = SuperAdminUser(
            id=uuid.uuid4(),
            email=email,
            password_hash=get_password_hash(PASSWORD),
            full_name=f"Test {role}",
            role=role,
            status="active",
            mfa_required=False,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


def make_token(user_id, role, mfa=True):
    return create_access_token(
        data={"sub": str(user_id), "role": role, "mfa_completed": mfa},
        expires_delta=datetime.timedelta(minutes=30),
    )


# ── Fixtures ─────────────────────────────────────────────────────────────

@pytest_asyncio.fixture(scope="module")
async def client():
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture(scope="module")
async def founder():
    return await create_test_user(FOUNDER_EMAIL, "founder")


@pytest_asyncio.fixture(scope="module")
async def engineer():
    return await create_test_user(ENGINEERING_EMAIL, "engineering")


@pytest_asyncio.fixture(scope="module")
async def support():
    return await create_test_user(SUPPORT_EMAIL, "support")


async def cleanup_org(name):
    async with db_session.AsyncSessionLocal() as db:
        res = await db.execute(select(Organization).where(Organization.name == name))
        org = res.scalars().first()
        if org:
            await db.execute(delete(TenantResources).where(TenantResources.organization_id == org.id))
            await db.delete(org)
            await db.commit()


def enterprise_payload(org_name: str, region: str = "centralindia", environment: str = "production") -> dict:
    return {
        "organization_name": org_name,
        "admin_email": "admin@acme.com",
        "admin_name": "Rahul Sharma",
        "environment": environment,
        "region": region,
        "call_type": "inbound",
        "language": "en-IN",
        "plan_type": "pilot",
        "stores_pii": True,
        "record_calls": True,
        "create_resource_group": True,
        "twilio_auto_provision": False,
    }


# ── MOCK PATCHES ─────────────────────────────────────────────────────────

MOCK_PATCHES = [
    "app.services.azure_resource_group_service.AzureResourceGroupService.provision_resource_group",
    "app.services.azure_keyvault_service.AzureKeyVaultService.provision_secret_refs",
    "app.services.azure_blob_service.AzureBlobService.provision_blob_storage",
    "app.services.qdrant_service.QdrantService.provision_collection",
    "app.services.redis_tenant_service.RedisTenantService.provision_redis",
    "app.services.twilio_service.TwilioService.provision_subaccount",
    "app.services.soniox_stt_service.SonioxSTTService.provision_stt",
    "app.services.soniox_tts_service.SonioxTTSService.provision_tts",
]


def get_mocked_returns(tenant_id_prefix="mock"):
    return [
        f"rg-nokvo-test-production",
        {
            "llm_api_key": f"tenant-{tenant_id_prefix}-llm-api-key",
            "stt_api_key": f"tenant-{tenant_id_prefix}-stt-api-key",
        },
        {
            "storage_account_name": "mockaccount",
            "container_name": "mockcontainer",
            "blob_prefix": f"tenants/{tenant_id_prefix}/",
        },
        f"tenant_{tenant_id_prefix}_knowledge",
        f"tenant:{tenant_id_prefix}",
        {"twilio_provider": "twilio", "subaccount_id": None, "phone_number_status": "pending", "subaccount_status": "skipped"},
        {"stt_provider": "soniox", "stt_status": "provisioned", "stt_model": "stt-rt-v4", "stt_endpoint": "wss://stt-rt.soniox.com/transcribe-websocket"},
        {"tts_provider": "soniox", "tts_status": "provisioned", "tts_model": "tts-rt-v1-preview", "tts_voice": "Adrian"},
    ]


# ── TESTS ────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_founder_can_provision(client, founder):
    """Test that a founder role can successfully provision a tenant."""
    org_name = "ProvTest Founder Org"
    await cleanup_org(org_name)

    token = make_token(founder.id, founder.role)
    
    with patch(MOCK_PATCHES[0], new_callable=AsyncMock, return_value="rg-nokvo-provtest-production") as m_rg, \
         patch(MOCK_PATCHES[1], new_callable=AsyncMock, return_value={"llm": "ref"}) as m_kv, \
         patch(MOCK_PATCHES[2], new_callable=AsyncMock, return_value={"storage_account_name": "sa", "container_name": "c", "blob_prefix": "p/"}) as m_blob, \
         patch(MOCK_PATCHES[3], new_callable=AsyncMock, return_value="tenant_x_knowledge") as m_qdrant, \
         patch(MOCK_PATCHES[4], new_callable=AsyncMock, return_value="tenant:x") as m_redis, \
         patch(MOCK_PATCHES[5], new_callable=AsyncMock, return_value={"phone_number_status": "pending"}) as m_twilio, \
         patch(MOCK_PATCHES[6], new_callable=AsyncMock, return_value={"stt_provider": "soniox", "stt_status": "provisioned", "stt_model": "stt-rt-v4", "stt_endpoint": "wss://stt-rt.soniox.com/transcribe-websocket"}) as m_stt, \
         patch(MOCK_PATCHES[7], new_callable=AsyncMock, return_value={"tts_provider": "soniox", "tts_status": "provisioned", "tts_model": "tts-rt-v1-preview", "tts_voice": "Adrian"}) as m_tts:

        resp = await client.post(
            "/superadmin/tenants/provision",
            json=enterprise_payload(org_name),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] in ["success", "partial"]
    assert data["tenant_id"]
    assert data["organization_profile"]["admin_email"] == "admin@acme.com"
    assert data["organization_profile"]["email_domain"] == "acme.com"
    assert "qdrant_url_ref" in data["resources"]
    assert data["resources"]["stt_provider"] == "soniox"
    assert data["resources"]["tts_provider"] == "soniox"
    assert "steps" in data
    assert "next_steps" in data

    async with db_session.AsyncSessionLocal() as db:
        org_res = await db.execute(select(Organization).where(Organization.name == org_name))
        org = org_res.scalars().first()
        member_res = await db.execute(
            select(OrganizationUser).where(
                OrganizationUser.organization_id == org.id,
                OrganizationUser.email == "admin@acme.com",
            )
        )
        member = member_res.scalars().first()
        assert member is not None
        assert member.role == "admin"

    await cleanup_org(org_name)


@pytest.mark.asyncio
async def test_engineering_can_provision(client, engineer):
    """Test that an engineering role can successfully provision a tenant."""
    org_name = "ProvTest Eng Org"
    await cleanup_org(org_name)

    token = make_token(engineer.id, engineer.role)

    with patch(MOCK_PATCHES[0], new_callable=AsyncMock, return_value="rg") as m1, \
         patch(MOCK_PATCHES[1], new_callable=AsyncMock, return_value={}) as m2, \
         patch(MOCK_PATCHES[2], new_callable=AsyncMock, return_value={"storage_account_name": "s", "container_name": "c", "blob_prefix": "p/"}) as m3, \
         patch(MOCK_PATCHES[3], new_callable=AsyncMock, return_value="col") as m4, \
         patch(MOCK_PATCHES[4], new_callable=AsyncMock, return_value="ns") as m5, \
         patch(MOCK_PATCHES[5], new_callable=AsyncMock, return_value={"phone_number_status": "pending"}) as m6, \
         patch(MOCK_PATCHES[6], new_callable=AsyncMock, return_value={"stt_provider": "soniox", "stt_status": "provisioned"}) as m7, \
         patch(MOCK_PATCHES[7], new_callable=AsyncMock, return_value={"tts_provider": "soniox", "tts_status": "provisioned"}) as m8:

        resp = await client.post(
            "/superadmin/tenants/provision",
            json=enterprise_payload(org_name),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201
    await cleanup_org(org_name)


@pytest.mark.asyncio
async def test_support_role_rejected(client, support):
    """Test that support role is blocked from provisioning tenants."""
    token = make_token(support.id, support.role)

    resp = await client.post(
        "/superadmin/tenants/provision",
        json=enterprise_payload("Should Fail Org"),
        headers={"Authorization": f"Bearer {token}"},
    )

    assert resp.status_code == 403
    assert "not permitted" in resp.json()["detail"].lower() or "role" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_unauthenticated_rejected(client):
    """Test that unauthenticated requests are rejected."""
    resp = await client.post(
        "/superadmin/tenants/provision",
        json=enterprise_payload("No Auth Org"),
    )
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_invalid_region_rejected(client, founder):
    """Test that an invalid region is rejected."""
    token = make_token(founder.id, founder.role)
    resp = await client.post(
        "/superadmin/tenants/provision",
        json=enterprise_payload("Bad Region Org", region="mars-west1"),
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "Invalid region" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_duplicate_provisioning_returns_existing(client, founder):
    """Test idempotency: calling twice returns existing tenant resources."""
    org_name = "Idempotent Org"
    await cleanup_org(org_name)

    token = make_token(founder.id, founder.role)

    with patch(MOCK_PATCHES[0], new_callable=AsyncMock, return_value="rg") as m1, \
         patch(MOCK_PATCHES[1], new_callable=AsyncMock, return_value={}) as m2, \
         patch(MOCK_PATCHES[2], new_callable=AsyncMock, return_value={"storage_account_name": "s", "container_name": "c", "blob_prefix": "p/"}) as m3, \
         patch(MOCK_PATCHES[3], new_callable=AsyncMock, return_value="col") as m4, \
         patch(MOCK_PATCHES[4], new_callable=AsyncMock, return_value="ns") as m5, \
         patch(MOCK_PATCHES[5], new_callable=AsyncMock, return_value={"phone_number_status": "pending"}) as m6, \
         patch(MOCK_PATCHES[6], new_callable=AsyncMock, return_value={"stt_provider": "soniox", "stt_status": "provisioned"}) as m7, \
         patch(MOCK_PATCHES[7], new_callable=AsyncMock, return_value={"tts_provider": "soniox", "tts_status": "provisioned"}) as m8:

        resp1 = await client.post(
            "/superadmin/tenants/provision",
            json=enterprise_payload(org_name),
            headers={"Authorization": f"Bearer {token}"},
        )
        resp2 = await client.post(
            "/superadmin/tenants/provision",
            json=enterprise_payload(org_name),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp1.status_code == 201
    assert resp2.status_code == 201
    assert resp1.json()["tenant_id"] == resp2.json()["tenant_id"]

    await cleanup_org(org_name)


@pytest.mark.asyncio
async def test_partial_failure_stored(client, founder):
    """Test that a partial failure is stored when an external service fails."""
    org_name = "Partial Fail Org"
    await cleanup_org(org_name)

    token = make_token(founder.id, founder.role)

    with patch(MOCK_PATCHES[0], new_callable=AsyncMock, return_value="rg") as m1, \
         patch(MOCK_PATCHES[1], new_callable=AsyncMock, return_value={}) as m2, \
         patch(MOCK_PATCHES[2], new_callable=AsyncMock, side_effect=RuntimeError("Blob down")) as m3, \
         patch(MOCK_PATCHES[3], new_callable=AsyncMock, return_value="col") as m4, \
         patch(MOCK_PATCHES[4], new_callable=AsyncMock, return_value="ns") as m5, \
         patch(MOCK_PATCHES[5], new_callable=AsyncMock, return_value={"phone_number_status": "pending"}) as m6, \
         patch(MOCK_PATCHES[6], new_callable=AsyncMock, return_value={"stt_provider": "soniox", "stt_status": "provisioned"}) as m7, \
         patch(MOCK_PATCHES[7], new_callable=AsyncMock, return_value={"tts_provider": "soniox", "tts_status": "provisioned"}) as m8:

        resp = await client.post(
            "/superadmin/tenants/provision",
            json=enterprise_payload(org_name),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201
    data = resp.json()
    assert data["status"] == "partial"
    blob_step = next(s for s in data["steps"] if s["name"] == "blob_prefixes")
    assert blob_step["status"] == "failed"

    await cleanup_org(org_name)


@pytest.mark.asyncio
async def test_secret_refs_not_exposed(client, founder):
    """Test that no actual secret values appear in the response."""
    org_name = "Secret Check Org"
    await cleanup_org(org_name)

    token = make_token(founder.id, founder.role)

    with patch(MOCK_PATCHES[0], new_callable=AsyncMock, return_value="rg") as m1, \
         patch(MOCK_PATCHES[1], new_callable=AsyncMock, return_value={"llm": "tenant-x-llm-api-key"}) as m2, \
         patch(MOCK_PATCHES[2], new_callable=AsyncMock, return_value={"storage_account_name": "s", "container_name": "c", "blob_prefix": "p/"}) as m3, \
         patch(MOCK_PATCHES[3], new_callable=AsyncMock, return_value="col") as m4, \
         patch(MOCK_PATCHES[4], new_callable=AsyncMock, return_value="ns") as m5, \
         patch(MOCK_PATCHES[5], new_callable=AsyncMock, return_value={"phone_number_status": "pending"}) as m6, \
         patch(MOCK_PATCHES[6], new_callable=AsyncMock, return_value={"stt_provider": "soniox", "stt_status": "provisioned"}) as m7, \
         patch(MOCK_PATCHES[7], new_callable=AsyncMock, return_value={"tts_provider": "soniox", "tts_status": "provisioned"}) as m8:

        resp = await client.post(
            "/superadmin/tenants/provision",
            json=enterprise_payload(org_name),
            headers={"Authorization": f"Bearer {token}"},
        )

    data = resp.json()
    resp_str = str(data)
    # No real secret values should be present - only references
    assert "PLACEHOLDER" not in resp_str
    assert "sk-" not in resp_str

    await cleanup_org(org_name)


@pytest.mark.asyncio
async def test_resource_group_step_called(client, founder):
    """Test that the resource group service is invoked during provisioning."""
    org_name = "RG Step Org"
    await cleanup_org(org_name)

    token = make_token(founder.id, founder.role)

    with patch(MOCK_PATCHES[0], new_callable=AsyncMock, return_value="rg-nokvo-rg-step-production") as m_rg, \
         patch(MOCK_PATCHES[1], new_callable=AsyncMock, return_value={}) as m2, \
         patch(MOCK_PATCHES[2], new_callable=AsyncMock, return_value={"storage_account_name": "s", "container_name": "c", "blob_prefix": "p/"}) as m3, \
         patch(MOCK_PATCHES[3], new_callable=AsyncMock, return_value="col") as m4, \
         patch(MOCK_PATCHES[4], new_callable=AsyncMock, return_value="ns") as m5, \
         patch(MOCK_PATCHES[5], new_callable=AsyncMock, return_value={"phone_number_status": "pending"}) as m6, \
         patch(MOCK_PATCHES[6], new_callable=AsyncMock, return_value={"stt_provider": "soniox", "stt_status": "provisioned"}) as m7, \
         patch(MOCK_PATCHES[7], new_callable=AsyncMock, return_value={"tts_provider": "soniox", "tts_status": "provisioned"}) as m8:

        resp = await client.post(
            "/superadmin/tenants/provision",
            json=enterprise_payload(org_name),
            headers={"Authorization": f"Bearer {token}"},
        )

    assert resp.status_code == 201
    m_rg.assert_called_once()

    await cleanup_org(org_name)


# NOTE: per-tenant LLM provisioning was removed — the chat/voice LLM is served by
# the shared gpt-5-mini pool, so there is no provision_ai_resource to test.


def test_qdrant_collection_name_is_client_scoped():
    collection_name = QdrantService.collection_name_for_tenant("Client-ABC/123")
    assert collection_name == "tenant_client-abc_123_knowledge"


def test_qdrant_cluster_ref_comes_from_settings():
    assert QdrantService.cluster_ref()
