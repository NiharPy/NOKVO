"""SmsService (Plivo Messaging) — the brochure+location channel while WhatsApp is off.

No-op discipline (disabled / no sender / no text), per-tenant sender resolution
(never a shared global in prod), plain ``{src,dst,text}`` body, and best-effort
on error. The end-of-call SMS behaviour is covered in test_whatsapp_mode.py.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.core.config import settings
from app.services.sms_service import SmsService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _tenant(plivo: dict | None = None):
    return SimpleNamespace(provider_status={"plivo": plivo or {}})


def test_sms_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_SMS_ENABLED", False)
    res = _run(SmsService.send_sms(
        tenant_res=_tenant({"sms_number": "+91111"}), to_number="+91999", text="hi",
    ))
    assert res["skipped"] and res["reason"] == "sms_disabled"


def test_sms_skips_when_no_sender(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_SMS_ENABLED", True)
    monkeypatch.setattr(settings, "PLIVO_SMS_FROM", "")
    res = _run(SmsService.send_sms(tenant_res=_tenant({}), to_number="+91999", text="hi"))
    assert res["skipped"] and res["reason"] == "no_sms_sender"


def test_sms_skips_when_no_text(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_SMS_ENABLED", True)
    res = _run(SmsService.send_sms(
        tenant_res=_tenant({"sms_number": "+91111"}), to_number="+91999", text="  ",
    ))
    assert res["skipped"] and res["reason"] == "missing_recipient_or_text"


def test_sms_sends_plain_body_with_tenant_sender(monkeypatch):
    from app.services.plivo_service import PlivoService
    monkeypatch.setattr(settings, "PLIVO_SMS_ENABLED", True)
    monkeypatch.setattr(settings, "PLIVO_SMS_FROM", "+91GLOBAL")

    captured: dict = {}

    async def fake_request(method, url, *, auth, json_body=None):
        captured.update(method=method, url=url, body=json_body)
        return {"message_uuid": ["UUID1"]}

    monkeypatch.setattr(PlivoService, "_request", fake_request)
    monkeypatch.setattr(PlivoService, "_master_auth", staticmethod(lambda: ("MASTER", "TOK")))

    res = _run(SmsService.send_sms(
        tenant_res=_tenant({"sms_number": "+91TENANT"}), to_number="+91999", text="hello",
    ))
    assert res["ok"] is True
    assert res["sender"] == "+91TENANT"                     # tenant sender beats global
    assert captured["method"] == "POST"
    assert captured["url"].endswith("/Message/")
    assert captured["body"] == {"src": "+91TENANT", "dst": "+91999", "text": "hello"}
    assert "type" not in captured["body"]                   # plain SMS, not whatsapp/template


def test_sms_uses_global_fallback_when_no_tenant_sender(monkeypatch):
    from app.services.plivo_service import PlivoService
    monkeypatch.setattr(settings, "PLIVO_SMS_ENABLED", True)
    monkeypatch.setattr(settings, "PLIVO_SMS_FROM", "+91GLOBAL")

    async def fake_request(method, url, *, auth, json_body=None):
        return {"message_uuid": "U"}
    monkeypatch.setattr(PlivoService, "_request", fake_request)
    monkeypatch.setattr(PlivoService, "_master_auth", staticmethod(lambda: ("M", "T")))

    res = _run(SmsService.send_sms(tenant_res=_tenant({}), to_number="+91999", text="hi"))
    assert res["ok"] and res["sender"] == "+91GLOBAL"


def test_sms_best_effort_on_error(monkeypatch):
    from app.services.plivo_service import PlivoError, PlivoService
    monkeypatch.setattr(settings, "PLIVO_SMS_ENABLED", True)

    async def boom(*a, **k):
        raise PlivoError("plivo down")
    monkeypatch.setattr(PlivoService, "_request", boom)
    monkeypatch.setattr(PlivoService, "_master_auth", staticmethod(lambda: ("M", "T")))

    res = _run(SmsService.send_sms(
        tenant_res=_tenant({"sms_number": "+91X"}), to_number="+91999", text="hi",
    ))
    assert res["ok"] is False and "error" in res            # never raises out
