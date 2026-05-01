import uuid
from unittest.mock import AsyncMock, patch

import pyotp
import pytest
import pytest_asyncio
from sqlalchemy import delete, select

from app.core.security import create_access_token, get_password_hash
from app.db import session as db_session
from app.models.organization import Organization
from app.models.organization_user import OrganizationUser
from app.models.tenant_resources import TenantResources
from app.models.session import SuperAdminSession
from app.models.user import SuperAdminUser
from app.services.crm_integration_service import CRMIntegrationService, CRMScanResult
from app.services.database_integration_service import DatabaseScanResult
from app.services.erp_integration_service import ERPScanResult
from app.services.shipping_integration_service import ShippingScanResult


pytestmark = pytest.mark.asyncio

FOUNDER_EMAIL = "org_auth_founder@nokvo.ai"
PASSWORD = "TestPassword123!"


async def test_zoho_hydrate_refreshes_saved_access_token_before_reuse():
    request_calls = []

    async def fake_request_json(method, url, headers, payload=None, allow_404=False, form_payload=None):
        request_calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "payload": payload,
                "allow_404": allow_404,
                "form_payload": form_payload,
            }
        )
        return {"access_token": "fresh-access-token", "api_domain": "https://www.zohoapis.in"}

    with patch.object(CRMIntegrationService, "_request_json", new=AsyncMock(side_effect=fake_request_json)):
        credentials = await CRMIntegrationService._hydrate_zoho_credentials(
            {
                "access_token": "expired-access-token",
                "refresh_token": "stored-refresh-token",
                "api_domain": "https://www.zohoapis.in",
            }
        )

    assert credentials["access_token"] == "fresh-access-token"
    assert credentials["refresh_token"] == "stored-refresh-token"
    assert credentials["api_domain"] == "https://www.zohoapis.in"
    assert request_calls[0]["form_payload"]["grant_type"] == "refresh_token"
    assert request_calls[0]["form_payload"]["refresh_token"] == "stored-refresh-token"


async def ensure_founder_user() -> SuperAdminUser:
    async with db_session.AsyncSessionLocal() as db:
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == FOUNDER_EMAIL))
        user = res.scalars().first()
        if user:
            return user

        user = SuperAdminUser(
            id=uuid.uuid4(),
            email=FOUNDER_EMAIL,
            password_hash=get_password_hash(PASSWORD),
            full_name="Org Auth Founder",
            role="founder",
            status="active",
            mfa_required=False,
        )
        db.add(user)
        await db.commit()
        await db.refresh(user)
        return user


async def cleanup_org(name: str) -> None:
    async with db_session.AsyncSessionLocal() as db:
        res = await db.execute(select(Organization).where(Organization.name == name))
        org = res.scalars().first()
        if org:
            await db.execute(delete(TenantResources).where(TenantResources.organization_id == org.id))
            await db.delete(org)
            await db.commit()


def make_founder_token(user_id: uuid.UUID, role: str) -> str:
    return create_access_token(
        data={"sub": str(user_id), "role": role, "mfa_completed": True, "principal_type": "superadmin"},
    )


def org_payload(name: str, admin_email: str = "admin@orgauthco.com") -> dict:
    return {
        "organization_name": name,
        "admin_email": admin_email,
        "admin_name": "Org Auth Admin",
        "environment": "production",
        "region": "centralindia",
        "call_type": "inbound",
        "language": "en-IN",
        "plan_type": "pilot",
        "stores_pii": True,
        "record_calls": True,
        "create_resource_group": True,
        "twilio_auto_provision": False,
    }


async def provision_org(client, founder: SuperAdminUser, name: str, admin_email: str = "admin@orgauthco.com"):
    token = make_founder_token(founder.id, founder.role)

    async def _mock_provision(**kwargs):
        return {
            "status": "success",
            "tenant_id": "tenant-org-auth",
            "organization_id": str(kwargs["organization_id"]),
            "resources": {},
            "steps": [],
            "next_steps": [],
        }

    with patch(
        "app.services.azure_tenant_provisioning_service.AzureTenantProvisioningService.provision",
        new_callable=AsyncMock,
        side_effect=_mock_provision,
    ):
        return await client.post(
            "/superadmin/tenants/provision",
            json=org_payload(name, admin_email=admin_email),
            headers={"Authorization": f"Bearer {token}"},
        )


async def complete_org_login(
    client,
    email: str,
    full_name: str,
    hosted_domain: str,
    organization_id: str | None = None,
):
    with patch(
        "app.api.organization_auth.GoogleOAuthService.verify_id_token",
        new=AsyncMock(return_value={
            "sub": f"google-{email}",
            "email": email,
            "email_verified": True,
            "full_name": full_name,
            "hosted_domain": hosted_domain,
        }),
    ):
        login_payload = {"id_token": f"{email}-token"}
        if organization_id:
            login_payload["organization_id"] = organization_id
        login_response = await client.post("/api/org-auth/google/login", json=login_payload)

    assert login_response.status_code == 200
    temp_token = login_response.json()["access_token"]

    setup_response = await client.post(
        "/api/org-auth/mfa/totp/setup",
        headers={"Authorization": f"Bearer {temp_token}"},
    )
    if setup_response.status_code == 200:
        secret = setup_response.json()["secret"]
    else:
        assert setup_response.status_code == 400
        assert setup_response.json()["detail"] == "TOTP already set up"
        async with db_session.AsyncSessionLocal() as db:
            result = await db.execute(select(OrganizationUser).where(OrganizationUser.email == email))
            secret = result.scalars().first().totp_secret_encrypted

    verify_response = await client.post(
        "/api/org-auth/mfa/totp/verify",
        json={"token": pyotp.TOTP(secret).now()},
        headers={"Authorization": f"Bearer {temp_token}"},
    )
    assert verify_response.status_code == 200
    return verify_response


@pytest_asyncio.fixture(scope="module", autouse=True)
async def cleanup_founder_user():
    yield
    async with db_session.AsyncSessionLocal() as db:
        res = await db.execute(select(SuperAdminUser).where(SuperAdminUser.email == FOUNDER_EMAIL))
        user = res.scalars().first()
        if user:
            await db.execute(delete(SuperAdminSession).where(SuperAdminSession.superadmin_id == user.id))
            await db.delete(user)
            await db.commit()


async def test_org_creation_rejects_personal_admin_email(client):
    founder = await ensure_founder_user()
    response = await provision_org(client, founder, "Personal Email Rejected Org", admin_email="owner@gmail.com")
    assert response.status_code == 422
    assert "work email" in str(response.json()).lower()
    await cleanup_org("Personal Email Rejected Org")


async def test_initial_admin_only_can_login_then_admin_can_add_member(client):
    founder = await ensure_founder_user()
    org_name = "OAuth Org Auth Flow"
    await cleanup_org(org_name)

    response = await provision_org(client, founder, org_name)
    assert response.status_code == 201
    organization_id = response.json()["organization_id"]

    with patch(
        "app.api.organization_auth.GoogleOAuthService.verify_id_token",
        new=AsyncMock(return_value={
            "sub": "google-admin-1",
            "email": "admin@orgauthco.com",
            "email_verified": True,
            "full_name": "Org Admin",
            "hosted_domain": "orgauthco.com",
        }),
    ):
        admin_login = await client.post(
            "/api/org-auth/google/login",
            json={"id_token": "admin-token"},
        )
    assert admin_login.status_code == 200
    assert admin_login.json()["refresh_token"] == "pending_mfa"
    admin_temp_token = admin_login.json()["access_token"]

    admin_profile_before_mfa = await client.get(
        "/api/org-auth/me",
        headers={"Authorization": f"Bearer {admin_temp_token}"},
    )
    assert admin_profile_before_mfa.status_code == 403
    assert admin_profile_before_mfa.json()["detail"] == "Organization MFA required"

    admin_setup = await client.post(
        "/api/org-auth/mfa/totp/setup",
        headers={"Authorization": f"Bearer {admin_temp_token}"},
    )
    assert admin_setup.status_code == 200
    admin_setup_data = admin_setup.json()
    assert admin_setup_data["email"] == "admin@orgauthco.com"
    assert "admin%40orgauthco.com" in admin_setup_data["uri"]

    admin_totp = pyotp.TOTP(admin_setup_data["secret"])
    admin_verify = await client.post(
        "/api/org-auth/mfa/totp/verify",
        json={"token": admin_totp.now()},
        headers={"Authorization": f"Bearer {admin_temp_token}"},
    )
    assert admin_verify.status_code == 200
    admin_token = admin_verify.json()["access_token"]
    assert admin_verify.json()["refresh_token"] != "pending_mfa"

    with patch(
        "app.api.organization_auth.GoogleOAuthService.verify_id_token",
        new=AsyncMock(return_value={
            "sub": "google-uninvited-1",
            "email": "member@orgauthco.com",
            "email_verified": True,
            "full_name": "Uninvited Member",
            "hosted_domain": "orgauthco.com",
        }),
    ):
        uninvited_login = await client.post(
            "/api/org-auth/google/login",
            json={"id_token": "member-token"},
        )
    assert uninvited_login.status_code == 403
    assert "not provisioned" in uninvited_login.json()["detail"].lower()

    member_create = await client.post(
        "/api/org-auth/members",
        json={"email": "member@orgauthco.com", "full_name": "Org Member", "role": "manager"},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert member_create.status_code == 201
    assert member_create.json()["role"] == "manager"
    assert member_create.json()["status"] == "invited"

    with patch(
        "app.api.organization_auth.GoogleOAuthService.verify_id_token",
        new=AsyncMock(return_value={
            "sub": "google-member-1",
            "email": "member@orgauthco.com",
            "email_verified": True,
            "full_name": "Org Member",
            "hosted_domain": "orgauthco.com",
        }),
    ):
        member_login = await client.post(
            "/api/org-auth/google/login",
            json={"id_token": "member-token"},
        )
    assert member_login.status_code == 200
    assert member_login.json()["refresh_token"] == "pending_mfa"

    member_temp_token = member_login.json()["access_token"]
    member_setup = await client.post(
        "/api/org-auth/mfa/totp/setup",
        headers={"Authorization": f"Bearer {member_temp_token}"},
    )
    assert member_setup.status_code == 200
    member_setup_data = member_setup.json()
    assert member_setup_data["email"] == "member@orgauthco.com"
    assert "member%40orgauthco.com" in member_setup_data["uri"]

    member_totp = pyotp.TOTP(member_setup_data["secret"])
    member_verify = await client.post(
        "/api/org-auth/mfa/totp/verify",
        json={"token": member_totp.now()},
        headers={"Authorization": f"Bearer {member_temp_token}"},
    )
    assert member_verify.status_code == 200
    assert member_verify.json()["user"]["role"] == "manager"
    assert member_verify.json()["organization"]["email_domain"] == "orgauthco.com"

    profile = await client.get(
        "/api/org-auth/me",
        headers={"Authorization": f"Bearer {member_verify.json()['access_token']}"},
    )
    assert profile.status_code == 200
    assert profile.json()["user"]["email"] == "member@orgauthco.com"

    async with db_session.AsyncSessionLocal() as db:
        result = await db.execute(
            select(OrganizationUser).where(
                OrganizationUser.organization_id == uuid.UUID(organization_id),
                OrganizationUser.email == "member@orgauthco.com",
            )
        )
        member = result.scalars().first()
        assert member is not None
        assert member.status == "active"
        assert member.email_verified is True

    await cleanup_org(org_name)


async def test_duplicate_domain_prompts_for_explicit_organization(client):
    founder = await ensure_founder_user()
    org_a = "Duplicate Domain Org A"
    org_b = "Duplicate Domain Org B"
    await cleanup_org(org_a)
    await cleanup_org(org_b)

    response_a = await provision_org(client, founder, org_a, admin_email="admin@sharedco.com")
    response_b = await provision_org(client, founder, org_b, admin_email="admin@sharedco.com")
    assert response_a.status_code == 201
    assert response_b.status_code == 201

    organization_id = response_a.json()["organization_id"]

    with patch(
        "app.api.organization_auth.GoogleOAuthService.verify_id_token",
        new=AsyncMock(return_value={
            "sub": "google-shared-admin-1",
            "email": "admin@sharedco.com",
            "email_verified": True,
            "full_name": "Shared Admin",
            "hosted_domain": "sharedco.com",
        }),
    ):
        ambiguous_login = await client.post(
            "/api/org-auth/google/login",
            json={"id_token": "shared-admin-token"},
        )
    assert ambiguous_login.status_code == 409
    detail = ambiguous_login.json()["detail"]
    assert detail["code"] == "organization_selection_required"
    assert len(detail["organizations"]) == 2
    assert {item["name"] for item in detail["organizations"]} == {org_a, org_b}

    with patch(
        "app.api.organization_auth.GoogleOAuthService.verify_id_token",
        new=AsyncMock(return_value={
            "sub": "google-shared-admin-1",
            "email": "admin@sharedco.com",
            "email_verified": True,
            "full_name": "Shared Admin",
            "hosted_domain": "sharedco.com",
        }),
    ):
        selected_login = await client.post(
            "/api/org-auth/google/login",
            json={"organization_id": organization_id, "id_token": "shared-admin-token"},
        )
    assert selected_login.status_code == 200
    assert selected_login.json()["refresh_token"] == "pending_mfa"

    setup = await client.post(
        "/api/org-auth/mfa/totp/setup",
        headers={"Authorization": f"Bearer {selected_login.json()['access_token']}"},
    )
    assert setup.status_code == 200
    code = pyotp.TOTP(setup.json()["secret"]).now()
    verified_login = await client.post(
        "/api/org-auth/mfa/totp/verify",
        json={"token": code},
        headers={"Authorization": f"Bearer {selected_login.json()['access_token']}"},
    )
    assert verified_login.status_code == 200
    assert verified_login.json()["organization"]["name"] == org_a

    await cleanup_org(org_a)
    await cleanup_org(org_b)


async def test_org_login_requires_existing_totp_for_same_registered_email_only(client):
    founder = await ensure_founder_user()
    org_name = "OAuth Org Existing TOTP"
    await cleanup_org(org_name)

    response = await provision_org(client, founder, org_name, admin_email="admin@existingtotp.com")
    assert response.status_code == 201

    with patch(
        "app.api.organization_auth.GoogleOAuthService.verify_id_token",
        new=AsyncMock(return_value={
            "sub": "google-admin-existing-1",
            "email": "admin@existingtotp.com",
            "email_verified": True,
            "full_name": "Existing TOTP Admin",
            "hosted_domain": "existingtotp.com",
        }),
    ):
        first_login = await client.post(
            "/api/org-auth/google/login",
            json={"id_token": "existing-admin-token-1"},
        )
    assert first_login.status_code == 200
    first_temp_token = first_login.json()["access_token"]

    first_setup = await client.post(
        "/api/org-auth/mfa/totp/setup",
        headers={"Authorization": f"Bearer {first_temp_token}"},
    )
    assert first_setup.status_code == 200
    shared_secret = first_setup.json()["secret"]

    first_verify = await client.post(
        "/api/org-auth/mfa/totp/verify",
        json={"token": pyotp.TOTP(shared_secret).now()},
        headers={"Authorization": f"Bearer {first_temp_token}"},
    )
    assert first_verify.status_code == 200

    with patch(
        "app.api.organization_auth.GoogleOAuthService.verify_id_token",
        new=AsyncMock(return_value={
            "sub": "google-admin-existing-2",
            "email": "admin@existingtotp.com",
            "email_verified": True,
            "full_name": "Existing TOTP Admin",
            "hosted_domain": "existingtotp.com",
        }),
    ):
        second_login = await client.post(
            "/api/org-auth/google/login",
            json={"id_token": "existing-admin-token-2"},
        )
    assert second_login.status_code == 200
    assert second_login.json()["refresh_token"] == "pending_mfa"

    second_setup = await client.post(
        "/api/org-auth/mfa/totp/setup",
        headers={"Authorization": f"Bearer {second_login.json()['access_token']}"},
    )
    assert second_setup.status_code == 400
    assert second_setup.json()["detail"] == "TOTP already set up"

    bad_verify = await client.post(
        "/api/org-auth/mfa/totp/verify",
        json={"token": "000000"},
        headers={"Authorization": f"Bearer {second_login.json()['access_token']}"},
    )
    assert bad_verify.status_code == 401

    good_verify = await client.post(
        "/api/org-auth/mfa/totp/verify",
        json={"token": pyotp.TOTP(shared_secret).now()},
        headers={"Authorization": f"Bearer {second_login.json()['access_token']}"},
    )
    assert good_verify.status_code == 200
    assert good_verify.json()["user"]["email"] == "admin@existingtotp.com"

    await cleanup_org(org_name)


async def test_admin_can_connect_and_index_database_schema(client):
    founder = await ensure_founder_user()
    org_name = "OAuth Org DB Connect"
    await cleanup_org(org_name)


async def test_admin_can_connect_crm_and_index_schema(client):
    founder = await ensure_founder_user()
    org_name = "CRM Connect Org"
    await cleanup_org(org_name)

    response = await provision_org(client, founder, org_name)
    assert response.status_code == 201
    organization_id = response.json()["organization_id"]

    async with db_session.AsyncSessionLocal() as db:
        tenant = TenantResources(
            id=uuid.uuid4(),
            organization_id=uuid.UUID(organization_id),
            tenant_id="tenant-org-auth-crm",
            qdrant_collection_name="tenant_org_auth_crm_knowledge",
            provisioning_status="success",
            provider_status={},
        )
        db.add(tenant)
        await db.commit()

    verify_response = await complete_org_login(
        client,
        email="admin@orgauthco.com",
        full_name="Org Admin",
        hosted_domain="orgauthco.com",
    )
    admin_token = verify_response.json()["access_token"]

    crm_scan_result = CRMScanResult(
        provider="zoho",
        account_name="www.zohoapis.com",
        folder_path="integrations/crm/zoho",
        modules=[
            {
                "api_name": "Leads",
                "label": "Leads",
                "fields": [{"name": "Company", "label": "Company", "type": "text", "required": True}],
            }
        ],
        actions=[
            {
                "module": "Leads",
                "name": "list_records",
                "method": "GET",
                "endpoint": "/crm/v8/Leads",
                "description": "Retrieve lead records",
            }
        ],
    )

    with patch(
        "app.api.organization_auth.CRMIntegrationService.scan_schema",
        new=AsyncMock(return_value=crm_scan_result),
    ), patch(
        "app.api.organization_auth.CRMIntegrationService.store_connection_secret",
        new=AsyncMock(return_value="tenant-zoho-secret"),
    ), patch(
        "app.api.organization_auth.CRMIntegrationService.index_schema_embeddings",
        new=AsyncMock(return_value={
            "indexed_points": 2,
            "module_count": 1,
            "action_count": 1,
            "folder_path": "integrations/crm/zoho",
            "indexed_paths": ["integrations/crm/zoho/schema/Leads", "integrations/crm/zoho/actions/Leads"],
        }),
    ):
        crm_response = await client.post(
            "/api/org-auth/crm/connect",
            json={
                "provider": "zoho",
                "api_domain": "https://www.zohoapis.com",
                "access_token": "zoho-access-token",
                "refresh_token": "zoho-refresh-token",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert crm_response.status_code == 200
    crm_payload = crm_response.json()
    assert crm_payload["provider"] == "zoho"
    assert crm_payload["status"] == "indexed"
    assert crm_payload["indexed_points"] == 2
    assert crm_payload["module_count"] == 1
    assert crm_payload["action_count"] == 1
    assert crm_payload["folder_path"] == "integrations/crm/zoho"
    assert crm_payload["modules"][0]["api_name"] == "Leads"
    assert crm_payload["actions"][0]["name"] == "list_records"

    crm_status_response = await client.get(
        "/api/org-auth/crm/status",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert crm_status_response.status_code == 200
    assert crm_status_response.json()["provider"] == "zoho"
    assert crm_status_response.json()["indexed_points"] == 2

    await cleanup_org(org_name)


async def test_admin_can_connect_tally_erp_and_index_schema(client):
    founder = await ensure_founder_user()
    org_name = "ERP Connect Org"
    await cleanup_org(org_name)

    response = await provision_org(client, founder, org_name)
    assert response.status_code == 201
    organization_id = response.json()["organization_id"]

    async with db_session.AsyncSessionLocal() as db:
        tenant = TenantResources(
            id=uuid.uuid4(),
            organization_id=uuid.UUID(organization_id),
            tenant_id="tenant-org-auth-erp",
            qdrant_collection_name="tenant_org_auth_erp_knowledge",
            provisioning_status="success",
            provider_status={},
        )
        db.add(tenant)
        await db.commit()

    verify_response = await complete_org_login(
        client,
        email="admin@orgauthco.com",
        full_name="Org Admin",
        hosted_domain="orgauthco.com",
    )
    admin_token = verify_response.json()["access_token"]

    erp_scan_result = ERPScanResult(
        provider="tally",
        account_name="Demo Company",
        folder_path="integrations/erp/tally",
        modules=[
            {
                "api_name": "ledgers",
                "label": "Ledgers",
                "object_type": "Ledger",
                "fields": [{"name": "Name", "label": "Name", "type": "text", "required": True}],
                "record_count": 1,
                "sample_records": [{"name": "Cash"}],
                "status": "available",
                "last_error": None,
            }
        ],
        actions=[
            {
                "module": "ledgers",
                "name": "create_ledger",
                "method": "IMPORT",
                "endpoint": "Import Data / All Masters / LEDGER",
                "description": "Create a ledger",
            }
        ],
    )

    with patch(
        "app.api.organization_auth.ERPIntegrationService.scan_schema",
        new=AsyncMock(return_value=erp_scan_result),
    ), patch(
        "app.api.organization_auth.ERPIntegrationService.store_connection_secret",
        new=AsyncMock(return_value="tenant-erp-secret"),
    ), patch(
        "app.api.organization_auth.ERPIntegrationService.index_schema_embeddings",
        new=AsyncMock(return_value={
            "indexed_points": 2,
            "module_count": 1,
            "action_count": 1,
            "folder_path": "integrations/erp/tally",
            "indexed_paths": ["integrations/erp/tally/schema/ledgers", "integrations/erp/tally/actions/ledgers"],
        }),
    ):
        erp_response = await client.post(
            "/api/org-auth/erp/connect",
            json={
                "provider": "tally",
                "base_url": "http://localhost:9000",
                "company_name": "Demo Company",
                "timeout_seconds": 5,
                "max_items_per_module": 10,
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert erp_response.status_code == 200
    erp_payload = erp_response.json()
    assert erp_payload["provider"] == "tally"
    assert erp_payload["status"] == "indexed"
    assert erp_payload["indexed_points"] == 2
    assert erp_payload["modules"][0]["api_name"] == "ledgers"

    erp_status_response = await client.get(
        "/api/org-auth/erp/status",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert erp_status_response.status_code == 200
    assert erp_status_response.json()["provider"] == "tally"
    assert erp_status_response.json()["indexed_points"] == 2

    await cleanup_org(org_name)


async def test_admin_can_connect_shiprocket_and_index_actions(client):
    founder = await ensure_founder_user()
    org_name = "Shiprocket Connect Org"
    await cleanup_org(org_name)

    response = await provision_org(client, founder, org_name)
    assert response.status_code == 201
    organization_id = response.json()["organization_id"]

    async with db_session.AsyncSessionLocal() as db:
        tenant = TenantResources(
            id=uuid.uuid4(),
            organization_id=uuid.UUID(organization_id),
            tenant_id="tenant-org-auth-shiprocket",
            qdrant_collection_name="tenant_org_auth_shiprocket_knowledge",
            provisioning_status="success",
            provider_status={},
        )
        db.add(tenant)
        await db.commit()

    verify_response = await complete_org_login(
        client,
        email="admin@orgauthco.com",
        full_name="Org Admin",
        hosted_domain="orgauthco.com",
    )
    admin_token = verify_response.json()["access_token"]

    shipping_scan_result = ShippingScanResult(
        provider="shiprocket",
        account_name="shiprocket-api@example.com",
        folder_path="integrations/shipping/shiprocket",
        modules=[
            {
                "api_name": "orders",
                "label": "Orders",
                "fields": ["order_id"],
                "description": "Create adhoc orders",
            }
        ],
        actions=[
            {
                "module": "orders",
                "name": "create_adhoc_order",
                "method": "POST",
                "endpoint": "/orders/create/adhoc",
                "description": "Create an order",
            }
        ],
    )

    with patch(
        "app.api.organization_auth.ShippingIntegrationService.scan_schema",
        new=AsyncMock(return_value=shipping_scan_result),
    ), patch(
        "app.api.organization_auth.ShippingIntegrationService.store_connection_secret",
        new=AsyncMock(return_value="tenant-shipping-secret"),
    ), patch(
        "app.api.organization_auth.ShippingIntegrationService.index_schema_embeddings",
        new=AsyncMock(return_value={
            "indexed_points": 2,
            "module_count": 1,
            "action_count": 1,
            "folder_path": "integrations/shipping/shiprocket",
            "indexed_paths": ["integrations/shipping/shiprocket/schema/orders"],
        }),
    ):
        shipping_response = await client.post(
            "/api/org-auth/shipping/connect",
            json={
                "provider": "shiprocket",
                "email": "shiprocket-api@example.com",
                "password": "secret",
                "base_url": "https://apiv2.shiprocket.in/v1/external",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert shipping_response.status_code == 200
    assert shipping_response.json()["provider"] == "shiprocket"
    assert shipping_response.json()["indexed_points"] == 2

    shipping_status_response = await client.get(
        "/api/org-auth/shipping/status",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert shipping_status_response.status_code == 200
    assert shipping_status_response.json()["provider"] == "shiprocket"

    await cleanup_org(org_name)


async def test_admin_can_connect_zoho_desk_and_manage_ticket(client):
    founder = await ensure_founder_user()
    org_name = "Zoho Desk Org"
    await cleanup_org(org_name)

    response = await provision_org(client, founder, org_name)
    assert response.status_code == 201
    organization_id = response.json()["organization_id"]

    async with db_session.AsyncSessionLocal() as db:
        tenant = TenantResources(
            id=uuid.uuid4(),
            organization_id=uuid.UUID(organization_id),
            tenant_id="tenant-org-auth-desk",
            qdrant_collection_name="tenant_org_auth_desk_knowledge",
            provisioning_status="success",
            provider_status={},
        )
        db.add(tenant)
        await db.commit()

    verify_response = await complete_org_login(
        client,
        email="admin@orgauthco.com",
        full_name="Org Admin",
        hosted_domain="orgauthco.com",
    )
    admin_token = verify_response.json()["access_token"]

    desk_scan_result = {
        "status": "indexed",
        "account_name": "Desk Portal",
        "org_id": "2389290",
        "indexed_points": 6,
        "module_count": 2,
        "action_count": 4,
        "folder_path": "integrations/zoho-desk",
    }

    with patch(
        "app.api.organization_auth.CRMIntegrationService.load_connection_secret",
        new=AsyncMock(return_value=("zoho", {"access_token": "token", "api_domain": "https://www.zohoapis.in"})),
    ), patch(
        "app.api.organization_auth.ZohoDeskService.connect_and_scan",
        new=AsyncMock(return_value=(
            {"access_token": "token", "api_domain": "https://www.zohoapis.in"},
            type(
                "DeskScan",
                (),
                {
                    "account_name": "Desk Portal",
                    "org_id": "2389290",
                    "modules": [{"api_name": "tickets", "label": "Tickets"}],
                    "actions": [{"module": "tickets", "name": "create_ticket"}],
                    "folder_path": "integrations/zoho-desk",
                },
            )(),
        )),
    ), patch(
        "app.api.organization_auth.ZohoDeskService.index_desk_embeddings",
        new=AsyncMock(return_value={
            "indexed_points": 6,
            "module_count": 2,
            "action_count": 4,
            "folder_path": "integrations/zoho-desk",
        }),
    ):
        connect_response = await client.post(
            "/api/org-auth/crm/zoho-desk/connect",
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert connect_response.status_code == 200
    assert connect_response.json()["org_id"] == "2389290"
    assert connect_response.json()["indexed_points"] == 6

    with patch(
        "app.api.organization_auth.CRMIntegrationService.load_connection_secret",
        new=AsyncMock(return_value=("zoho", {"access_token": "token", "api_domain": "https://www.zohoapis.in"})),
    ), patch(
        "app.api.organization_auth.ZohoDeskService.create_ticket",
        new=AsyncMock(return_value={"id": "9001", "status": "Open", "subject": "Callback requested", "departmentId": "1"}),
    ):
        ticket_response = await client.post(
            "/api/org-auth/crm/zoho-desk/tickets",
            json={"subject": "Callback requested", "department_id": "1", "description": "Customer requested callback"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert ticket_response.status_code == 200
    assert ticket_response.json()["id"] == "9001"

    with patch(
        "app.api.organization_auth.CRMIntegrationService.load_connection_secret",
        new=AsyncMock(return_value=("zoho", {"access_token": "token", "api_domain": "https://www.zohoapis.in"})),
    ), patch(
        "app.api.organization_auth.ZohoDeskService.update_ticket",
        new=AsyncMock(return_value={"id": "9001", "status": "Closed", "subject": "Callback requested", "departmentId": "1"}),
    ):
        update_response = await client.patch(
            "/api/org-auth/crm/zoho-desk/tickets/9001",
            json={"status": "Closed"},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
    assert update_response.status_code == 200
    assert update_response.json()["status"] == "Closed"

    await cleanup_org(org_name)

    response = await provision_org(client, founder, org_name, admin_email="admin@dborgco.com")
    assert response.status_code == 201

    async with db_session.AsyncSessionLocal() as db:
        tenant = TenantResources(
            organization_id=uuid.UUID(response.json()["organization_id"]),
            tenant_id="tenant-org-db-connect",
            qdrant_collection_name="tenant_org_db_connect_knowledge",
            qdrant_url_ref="https://example.qdrant.local",
            redis_namespace="tenant:org-db-connect",
            key_vault_name="nokvo-secrets-4305",
            secret_refs={},
            provider_status={},
            provisioning_status="success",
            provisioning_steps=[],
            cleanup_required=False,
            usage_minutes=0,
            total_cost_usd=0,
        )
        db.add(tenant)
        await db.commit()

    admin_verify = await complete_org_login(
        client,
        email="admin@dborgco.com",
        full_name="DB Org Admin",
        hosted_domain="dborgco.com",
    )
    admin_token = admin_verify.json()["access_token"]
    organization_id = response.json()["organization_id"]

    with patch(
        "app.api.organization_auth.DatabaseIntegrationService.scan_schema",
        new=AsyncMock(return_value=DatabaseScanResult(
            provider="postgresql",
            database_name="customerdb",
            schema=[
                {
                    "schema": "public",
                    "name": "customers",
                    "columns": [
                        {"name": "name", "type": "text", "nullable": True},
                        {"name": "email", "type": "text", "nullable": False},
                    ],
                }
            ],
        )),
    ), patch(
        "app.api.organization_auth.DatabaseIntegrationService.store_connection_secret",
        new=AsyncMock(return_value="tenant-db-secret"),
    ):
        connect_response = await client.post(
            "/api/org-auth/database/connect",
            json={
                "provider": "postgresql",
                "connection_string": "postgresql://demo:demo@localhost:5432/customerdb",
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert connect_response.status_code == 200
    assert connect_response.json()["provider"] == "postgresql"
    assert connect_response.json()["database_name"] == "customerdb"

    with patch(
        "app.api.organization_auth.DatabaseIntegrationService.load_connection_secret",
        new=AsyncMock(return_value=("postgresql", "postgresql://demo:demo@localhost:5432/customerdb")),
    ), patch(
        "app.api.organization_auth.DatabaseIntegrationService.index_selected_columns",
        new=AsyncMock(return_value={
            "indexed_points": 12,
            "tables": ["public.customers"],
            "column_value_count": 24,
            "row_limit": 50,
        }),
    ):
        index_response = await client.post(
            "/api/org-auth/database/index",
            json={
                "provider": "postgresql",
                "row_limit": 50,
                "selections": [
                    {"schema": "public", "table": "customers", "columns": ["name", "email"]},
                ],
            },
            headers={"Authorization": f"Bearer {admin_token}"},
        )

    assert index_response.status_code == 200
    assert index_response.json()["status"] == "indexed"
    assert index_response.json()["indexed_points"] == 12

    async with db_session.AsyncSessionLocal() as db:
        tenant = (
            await db.execute(select(TenantResources).where(TenantResources.organization_id == uuid.UUID(organization_id)))
        ).scalars().first()
        assert tenant is not None
        assert tenant.provider_status["db_status"] == "indexed"
        assert tenant.provider_status["db_provider"] == "postgresql"
        assert tenant.provider_status["db_indexed_points"] == 12

    await cleanup_org(org_name)
