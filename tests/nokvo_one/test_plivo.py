"""Plivo telephony: subaccount/application/number provisioning logic, inbound
answer XML, and the phone-link (call-forwarding) response shape. The Plivo REST
layer is mocked — no network."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.plivo_service import PlivoService, PlivoError


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def plivo_creds(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_AUTH_ID", "MASTER")
    monkeypatch.setattr(settings, "PLIVO_AUTH_TOKEN", "secret")


def _mock_request(monkeypatch, handler):
    async def fake(method, url, *, auth, json_body=None):
        return handler(method, url, json_body)
    monkeypatch.setattr(PlivoService, "_request", staticmethod(fake))


def test_create_subaccount_and_application(plivo_creds, monkeypatch):
    calls = []

    def handler(method, url, body):
        calls.append((method, url, body))
        if "/Subaccount/" in url:
            return {"auth_id": "SUBxxx", "auth_token": "subtok"}
        if "/Application/" in url:
            return {"app_id": "APP123"}
        return {}

    _mock_request(monkeypatch, handler)
    sub = _run(PlivoService.create_subaccount("acme"))
    assert sub == {"auth_id": "SUBxxx", "auth_token": "subtok"}
    app_id = _run(PlivoService.create_application(app_name="nokvo-acme", answer_url="https://x/api/.../plivo/voice/L1"))
    assert app_id == "APP123"
    app_call = next(c for c in calls if "/Application/" in c[1])
    assert app_call[2]["answer_url"].endswith("/plivo/voice/L1")
    assert app_call[2]["answer_method"] == "POST"


def test_rent_number_assigns_to_app_and_subaccount(plivo_creds, monkeypatch):
    calls = []

    def handler(method, url, body):
        calls.append((method, url, body))
        if "PhoneNumber/?" in url:
            return {"objects": [{"number": "911140000000"}]}
        return {}

    _mock_request(monkeypatch, handler)
    res = _run(PlivoService.rent_number(country="IN", app_id="APP123", sub_auth_id="SUBxxx"))
    assert res == {"number": "911140000000", "status": "active"}
    # The DID was bound to the app + moved to the subaccount.
    assign = next(c for c in calls if c[1].endswith("/Number/911140000000/"))
    assert assign[2]["app_id"] == "APP123" and assign[2]["subaccount"] == "SUBxxx"


def test_rent_number_raises_when_no_india_inventory(plivo_creds, monkeypatch):
    # India KYC/regulatory → no instantly-rentable DID. Caller treats this as pending.
    _mock_request(monkeypatch, lambda m, u, b: {"objects": []} if "PhoneNumber/?" in u else {})
    with pytest.raises(PlivoError):
        _run(PlivoService.rent_number(country="IN"))


def test_missing_master_creds_raises():
    # No monkeypatch → creds empty in test settings.
    with pytest.raises(PlivoError):
        _run(PlivoService.create_subaccount("acme"))


def test_phone_link_response_shows_forwarding_did():
    tr = SimpleNamespace(
        provider_status={
            "plivo": {
                "number": "911140000000",
                "number_status": "active",
                "application_id": "APP123",
                "forward_from_number": "919876543210",
                "answer_url": "https://x/plivo/voice/L1",
            },
            "agent_phone_link": {"link_id": "L1", "status": "linked"},
        },
        twilio_phone_number=None,
    )
    r = PlivoService.phone_link_response(tr)
    assert r["provider"] == "plivo"
    assert r["plivo_number"] == "911140000000"      # tenant forwards their number HERE
    assert r["number_status"] == "active"
    assert r["forward_from_number"] == "919876543210"
    assert r["link_id"] == "L1"


def test_pending_verification_when_no_number():
    tr = SimpleNamespace(provider_status={"plivo": {"application_id": "APP", "number": None}}, twilio_phone_number=None)
    r = PlivoService.phone_link_response(tr)
    assert r["number_status"] == "pending_verification"


def test_inbound_answer_xml_is_bidirectional_stream():
    from app.api.nokvo_one_voice import _plivo_stream_xml
    xml = _plivo_stream_xml("wss://host/api/nokvo-one/agents/plivo/media/L1")
    assert "<Stream" in xml and 'bidirectional="true"' in xml
    assert "wss://host/api/nokvo-one/agents/plivo/media/L1</Stream>" in xml
    assert "audio/x-l16" in xml
