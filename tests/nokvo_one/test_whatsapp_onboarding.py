"""Concierge WhatsApp onboarding — the client requests setup and never sees Plivo.

Covers the request → setting_up → connected state machine on the tenant, the
best-effort ops alert (sent only when configured), the operator's fulfilment
(connect flips the pending request to connected), cancel/disconnect, and the
superadmin requests-queue filter.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.plivo_service import PlivoError, PlivoService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeDB:
    def add(self, obj):  # sync, like SQLAlchemy's Session.add
        pass

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass


def _tenant(plivo: dict | None = None):
    return SimpleNamespace(
        provider_status={"plivo": plivo or {}},
        organization_id="org-1",
        tenant_id="t-1",
    )


# ── client request → setting_up ─────────────────────────────────────────────


def test_request_stores_request_and_reports_setting_up(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)
    monkeypatch.setattr(settings, "WHATSAPP_ONBOARDING_ALERT_EMAIL", "")  # no alert path

    tr = _tenant({"number": "918031321315"})  # voice already configured
    res = _run(PlivoService.request_whatsapp_setup(
        tr, _FakeDB(),
        business_name="  Skyline Realty  ",
        contact_number=" +91 7569672503 ",
        display_name="Skyline",
        requested_by="admin@skyline.com",
    ))

    ob = tr.provider_status["plivo"]["whatsapp_onboarding"]
    assert ob["step"] == "requested"
    assert ob["business_name"] == "Skyline Realty"        # trimmed
    assert ob["contact_number"] == "+917569672503"        # normalised
    assert ob["display_name"] == "Skyline"
    assert ob["requested_by"] == "admin@skyline.com"
    assert ob["requested_at"]
    assert tr.provider_status["plivo"]["whatsapp_status"] == "setting_up"
    assert tr.provider_status["plivo"]["number"] == "918031321315"  # voice intact

    # Client-facing status: setting up, not connected, nothing sends yet.
    assert res["onboarding_step"] == "setting_up"
    assert res["next_step"] == "awaiting_setup"
    assert res["connected"] is False
    assert res["ready_to_send"] is False
    assert res["onboarding"]["business_name"] == "Skyline Realty"


def test_request_validation():
    with pytest.raises(PlivoError):
        _run(PlivoService.request_whatsapp_setup(
            _tenant(), _FakeDB(), business_name="  ", contact_number="+917569672503",
        ))
    with pytest.raises(PlivoError):
        _run(PlivoService.request_whatsapp_setup(
            _tenant(), _FakeDB(), business_name="Acme", contact_number="abc",
        ))


# ── ops alert: only when configured, never fatal ────────────────────────────


def test_request_alerts_ops_when_configured(monkeypatch):
    from app.services.email_service import EmailService

    sent: list[tuple] = []

    async def fake_send(to_email, subject, text_body, html_body=None):
        sent.append((to_email, subject, text_body))

    monkeypatch.setattr(settings, "WHATSAPP_ONBOARDING_ALERT_EMAIL", "ops@nokvo.ai")
    monkeypatch.setattr(EmailService, "send", fake_send)

    _run(PlivoService.request_whatsapp_setup(
        _tenant(), _FakeDB(), business_name="Skyline Realty", contact_number="+917569672503",
    ))

    assert len(sent) == 1
    assert sent[0][0] == "ops@nokvo.ai"
    assert "Skyline Realty" in sent[0][1]          # business name in subject
    assert "+917569672503" in sent[0][2]           # number in body


def test_request_no_alert_when_unconfigured(monkeypatch):
    from app.services.email_service import EmailService

    sent: list[tuple] = []

    async def fake_send(to_email, subject, text_body, html_body=None):
        sent.append((to_email, subject))

    monkeypatch.setattr(settings, "WHATSAPP_ONBOARDING_ALERT_EMAIL", "")
    monkeypatch.setattr(EmailService, "send", fake_send)

    _run(PlivoService.request_whatsapp_setup(
        _tenant(), _FakeDB(), business_name="Acme", contact_number="+917569672503",
    ))
    assert sent == []                               # no recipient → no send


def test_request_survives_email_failure(monkeypatch):
    from app.services.email_service import EmailService

    async def boom(*a, **k):
        raise RuntimeError("smtp down")

    monkeypatch.setattr(settings, "WHATSAPP_ONBOARDING_ALERT_EMAIL", "ops@nokvo.ai")
    monkeypatch.setattr(EmailService, "send", boom)

    # A mail failure must NOT fail the client's request.
    res = _run(PlivoService.request_whatsapp_setup(
        _tenant(), _FakeDB(), business_name="Acme", contact_number="+917569672503",
    ))
    assert res["onboarding_step"] == "setting_up"


# ── operator fulfilment: connect flips pending → connected ──────────────────


def test_operator_connect_marks_request_connected(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)

    tr = _tenant()
    _run(PlivoService.request_whatsapp_setup(
        tr, _FakeDB(), business_name="Skyline Realty", contact_number="+917569672503",
    ))
    assert tr.provider_status["plivo"]["whatsapp_onboarding"]["step"] == "requested"

    # Operator records the WABA sender they onboarded in the Plivo Console.
    res = _run(PlivoService.connect_whatsapp_number(
        tr, _FakeDB(), whatsapp_number="+917569672503",
        waba_id="WABA123", phone_number_id="PN456",
    ))

    plivo = tr.provider_status["plivo"]
    assert plivo["whatsapp_onboarding"]["step"] == "connected"  # request cleared from queue
    assert plivo["whatsapp_onboarding"]["business_name"] == "Skyline Realty"  # details kept
    assert plivo["whatsapp_number"] == "+917569672503"
    assert plivo["waba_id"] == "WABA123"
    assert plivo["phone_number_id"] == "PN456"
    assert res["onboarding_step"] == "connected"
    assert res["next_step"] == "ready"
    assert res["ready_to_send"] is True


def test_disconnect_cancels_pending_request(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)

    tr = _tenant({"number": "918031321315"})
    _run(PlivoService.request_whatsapp_setup(
        tr, _FakeDB(), business_name="Acme", contact_number="+917569672503",
    ))
    res = _run(PlivoService.disconnect_whatsapp_number(tr, _FakeDB()))

    assert "whatsapp_onboarding" not in tr.provider_status["plivo"]  # back to not_requested
    assert tr.provider_status["plivo"]["number"] == "918031321315"   # voice intact
    assert res["onboarding_step"] == "not_requested"
    assert res["next_step"] == "request"


def test_status_not_requested_when_empty(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)
    res = PlivoService.whatsapp_link_response(SimpleNamespace(provider_status={}))
    assert res["onboarding_step"] == "not_requested"
    assert res["next_step"] == "request"
    assert res["onboarding"] is None


# NOTE: the SuperAdmin WhatsApp requests-queue (and its `_whatsapp_request_item`
# helper) was removed in the SuperAdmin console overhaul — the console is now
# view + per-call-cost + plan-upgrade only. The client-side request → connected
# state machine above (PlivoService) is unchanged and still covered.
