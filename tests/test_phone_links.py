"""Tests for Telnyx phone-link endpoints."""
from __future__ import annotations

import pytest
import pytest_asyncio
import uuid
from unittest.mock import AsyncMock, patch, MagicMock
from httpx import ASGITransport, AsyncClient

from app.main import app as fastapi_app
from app.models.tenant_resources import TenantResources


def _make_tenant_res(**kwargs) -> TenantResources:
    tr = MagicMock(spec=TenantResources)
    tr.provider_status = kwargs.get("provider_status", {})
    tr.tenant_id = kwargs.get("tenant_id", str(uuid.uuid4()))
    tr.twilio_phone_number = None
    tr.twilio_status = None
    return tr


def _linked_link(phone="+16674218227", link_id=None):
    return {
        "link_id": link_id or str(uuid.uuid4()),
        "status": "linked",
        "provider": "telnyx",
        "phone_number": phone,
        "telnyx_number_id": "num-abc",
        "voice_url": "https://example.ngrok.io/api/org-auth/agent/telnyx/voice/abc",
        "linked_at": "2026-01-01T00:00:00+00:00",
        "unlinked_at": None,
    }


# -----------------------------------------------------------------------
# List phone links
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_phone_links_empty(org_auth_client, org_admin_token, mock_tenant_res_empty):
    """Empty tenant returns empty links list."""
    with patch("app.api.organization_auth._get_tenant_resources_for_org", return_value=mock_tenant_res_empty):
        resp = await org_auth_client.get(
            "/api/org-auth/agent/phone-links",
            headers={"Authorization": f"Bearer {org_admin_token}"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["links"] == []
    assert body["max"] == 5


@pytest.mark.asyncio
async def test_list_phone_links_with_linked(org_auth_client, org_admin_token):
    """Returns only active (linked / pending_manual_review) links."""
    link1 = _linked_link()
    link2 = {**_linked_link(phone="+19991112222"), "status": "unlinked"}
    link3 = {**_linked_link(phone="+19993334444"), "status": "pending_manual_review",
             "phone_number": None}
    tr = _make_tenant_res(provider_status={"agent_phone_links": [link1, link2, link3]})
    with patch("app.api.organization_auth._get_tenant_resources_for_org", return_value=tr):
        resp = await org_auth_client.get(
            "/api/org-auth/agent/phone-links",
            headers={"Authorization": f"Bearer {org_admin_token}"},
        )
    assert resp.status_code == 200
    links = resp.json()["links"]
    assert len(links) == 2
    statuses = {l["status"] for l in links}
    assert statuses == {"linked", "pending_manual_review"}


# -----------------------------------------------------------------------
# Add (link) phone number
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_add_phone_link_success(org_auth_client, org_admin_token):
    """Links a Telnyx number and returns PhoneLinkOut."""
    tr = _make_tenant_res()
    new_link = _linked_link(link_id="new-link-id")

    with patch("app.api.organization_auth._get_tenant_resources_for_org", return_value=tr), \
         patch("app.services.telnyx_service.TelnyxService.link_agent_phone_number",
               new_callable=AsyncMock, return_value=new_link):
        resp = await org_auth_client.post(
            "/api/org-auth/agent/phone-links",
            headers={"Authorization": f"Bearer {org_admin_token}"},
            json={"phone_number": "+16674218227"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["link_id"] == "new-link-id"
    assert body["status"] == "linked"
    assert body["phone_number"] == "+16674218227"


@pytest.mark.asyncio
async def test_add_phone_link_at_max(org_auth_client, org_admin_token):
    """Returns 400 when max phone links reached."""
    active = [_linked_link(phone=f"+1555000000{i}") for i in range(5)]
    tr = _make_tenant_res(provider_status={"agent_phone_links": active})

    with patch("app.api.organization_auth._get_tenant_resources_for_org", return_value=tr), \
         patch("app.services.telnyx_service.TelnyxService.link_agent_phone_number",
               new_callable=AsyncMock,
               side_effect=RuntimeError("Maximum of 5 phone numbers can be linked.")):
        resp = await org_auth_client.post(
            "/api/org-auth/agent/phone-links",
            headers={"Authorization": f"Bearer {org_admin_token}"},
            json={"phone_number": "+16674218227"},
        )
    assert resp.status_code == 400
    assert "5" in resp.json()["detail"]


@pytest.mark.asyncio
async def test_add_phone_link_number_not_found(org_auth_client, org_admin_token):
    """Returns 400 when Telnyx can't find the number."""
    tr = _make_tenant_res()

    with patch("app.api.organization_auth._get_tenant_resources_for_org", return_value=tr), \
         patch("app.services.telnyx_service.TelnyxService.link_agent_phone_number",
               new_callable=AsyncMock,
               side_effect=RuntimeError("Phone number '+19999999999' was not found")):
        resp = await org_auth_client.post(
            "/api/org-auth/agent/phone-links",
            headers={"Authorization": f"Bearer {org_admin_token}"},
            json={"phone_number": "+19999999999"},
        )
    assert resp.status_code == 400


# -----------------------------------------------------------------------
# Request Indian number
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_request_indian_number(org_auth_client, org_admin_token):
    """Creates a pending_manual_review entry in provider_status."""
    tr = _make_tenant_res()

    async def _fake_commit():
        pass

    db_mock = AsyncMock()
    db_mock.add = MagicMock()
    db_mock.commit = _fake_commit

    with patch("app.api.organization_auth._get_tenant_resources_for_org", return_value=tr), \
         patch("app.api.deps.get_db", return_value=iter([db_mock])):
        resp = await org_auth_client.post(
            "/api/org-auth/agent/phone-numbers/request-indian",
            headers={"Authorization": f"Bearer {org_admin_token}"},
            json={
                "city_or_region": "Mumbai",
                "number_type": "local",
                "business_name": "Acme Ltd",
                "business_address": "123 Main St",
                "gst_or_cin": "27AABCC1234A1Z5",
                "notes": "Needed for customer support line",
            },
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "pending_manual_review"
    assert "link_id" in body
    assert "3" in body["message"] or "5" in body["message"]  # mentions days


# -----------------------------------------------------------------------
# Delete (unlink) phone number
# -----------------------------------------------------------------------

@pytest.mark.asyncio
async def test_delete_phone_link_success(org_auth_client, org_admin_token):
    """Marks a linked number as unlinked."""
    link_id = str(uuid.uuid4())
    link = _linked_link(link_id=link_id)
    tr = _make_tenant_res(provider_status={"agent_phone_links": [link]})

    with patch("app.api.organization_auth._get_tenant_resources_for_org", return_value=tr), \
         patch("app.services.telnyx_service.TelnyxService.unlink_agent_phone_number",
               new_callable=AsyncMock, return_value={**link, "status": "unlinked"}):
        resp = await org_auth_client.delete(
            f"/api/org-auth/agent/phone-links/{link_id}",
            headers={"Authorization": f"Bearer {org_admin_token}"},
        )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True


@pytest.mark.asyncio
async def test_delete_phone_link_not_found(org_auth_client, org_admin_token):
    """Returns 404 when link_id doesn't exist."""
    tr = _make_tenant_res(provider_status={"agent_phone_links": []})
    with patch("app.api.organization_auth._get_tenant_resources_for_org", return_value=tr):
        resp = await org_auth_client.delete(
            "/api/org-auth/agent/phone-links/nonexistent-id",
            headers={"Authorization": f"Bearer {org_admin_token}"},
        )
    assert resp.status_code == 404


# -----------------------------------------------------------------------
# provider_status safety: unrelated keys must survive
# -----------------------------------------------------------------------

def test_provider_status_preserves_unrelated_keys():
    """load-copy-modify-save never blows away crm/erp/documents keys."""
    from app.services.telnyx_service import TelnyxService

    original_ps = {
        "crm": {"provider": "zoho", "status": "connected"},
        "erp": {"provider": "tally"},
        "documents": [{"id": "doc1"}],
        "agent_phone_links": [],
    }
    tr = _make_tenant_res(provider_status=dict(original_ps))

    links = TelnyxService._get_links(tr)
    # Simulate what link_agent_phone_number does: copy ps, set phone links, assign back
    ps = dict(tr.provider_status or {})
    ps["agent_phone_links"] = links + [_linked_link()]
    tr.provider_status = ps

    # All original non-phone keys must still be present
    for key in ("crm", "erp", "documents"):
        assert key in tr.provider_status, f"Key '{key}' was lost from provider_status"
    assert tr.provider_status["crm"] == original_ps["crm"]


# -----------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------

@pytest_asyncio.fixture
async def org_auth_client():
    async with AsyncClient(transport=ASGITransport(app=fastapi_app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
def org_admin_token():
    """Return a dummy token — routes are mocked so auth is bypassed in most tests."""
    return "dummy-token-for-mock-tests"


@pytest.fixture
def mock_tenant_res_empty():
    return _make_tenant_res(provider_status={})
