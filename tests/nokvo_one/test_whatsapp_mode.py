"""WhatsApp feature: per-tenant sender, brochure whatsapp_mode, location-on-booking.

Covers the no-op-when-disabled discipline, the per-tenant sender resolution
(never a shared global), the brochure-intent detector + the whatsapp_mode FSM
trigger (inbound AND outbound), and the project WhatsApp config resolvers.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.whatsapp_service import WhatsAppService
from app.services.real_estate_project_service import (
    project_whatsapp_brochure,
    project_whatsapp_location,
)
from app.services.tool_flow_policy import brochure_intent_active, detect_brochure_request
from app.services import real_estate_agent_fsm as inb_fsm
from app.services import real_estate_outbound_agent_fsm as outb_fsm


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _tenant(plivo: dict | None = None):
    return SimpleNamespace(provider_status={"plivo": plivo or {}})


# ── sender / no-op discipline ───────────────────────────────────────────────


def test_send_template_skips_when_disabled(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", False)
    res = _run(WhatsAppService.send_template(
        tenant_res=_tenant({"whatsapp_number": "+910000000000"}),
        to_number="+919999999999", template_name="t",
    ))
    assert res == {"ok": False, "skipped": True, "reason": "whatsapp_disabled"}


def test_send_template_skips_when_no_tenant_sender(monkeypatch):
    # Enabled, but the tenant has no WABA number AND there's no global fallback →
    # skip. It must NEVER borrow another tenant's / a shared sender.
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_FROM", "")
    res = _run(WhatsAppService.send_template(
        tenant_res=_tenant({}), to_number="+919999999999", template_name="t",
    ))
    assert res["skipped"] is True and res["reason"] == "no_whatsapp_sender"


def test_sender_prefers_tenant_number_over_global(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_FROM", "+91GLOBAL")
    # Tenant's own WABA number wins.
    assert WhatsAppService._resolve_sender({"whatsapp_number": "+91TENANT"}) == "+91TENANT"
    # Only falls back to the global when the tenant has none.
    assert WhatsAppService._resolve_sender({}) == "+91GLOBAL"


def test_components_build_header_and_body():
    comps = WhatsAppService._build_components(["Skyline", "https://maps/x"], "https://x/b.pdf")
    assert comps[0]["type"] == "header"
    assert comps[0]["parameters"][0]["document"]["link"] == "https://x/b.pdf"
    assert comps[1]["type"] == "body"
    assert [p["text"] for p in comps[1]["parameters"]] == ["Skyline", "https://maps/x"]


# ── project resolvers ───────────────────────────────────────────────────────


def _project(**wa):
    return SimpleNamespace(
        name="Skyline Heights", location="Gachibowli",
        brochure_url="https://x/brochure.pdf", whatsapp=wa,
    )


def test_location_resolver_configured_and_unconfigured():
    p = _project(location={"template": "loc_tpl", "maps_url": "https://maps/x"})
    cfg = project_whatsapp_location(p)
    assert cfg["template"] == "loc_tpl"
    assert cfg["body_params"] == ["Skyline Heights", "https://maps/x"]
    # No template → None (sends nothing).
    assert project_whatsapp_location(_project()) is None


def test_brochure_resolver_uses_brochure_url_as_media():
    cfg = project_whatsapp_brochure(_project(brochure={"template": "broc_tpl"}))
    assert cfg["template"] == "broc_tpl"
    assert cfg["body_params"] == ["Skyline Heights"]
    assert cfg["media_url"] == "https://x/brochure.pdf"
    assert project_whatsapp_brochure(_project()) is None


# ── brochure-intent detector ────────────────────────────────────────────────


@pytest.mark.parametrize("text", [
    "send me the brochure",
    "can you whatsapp me the details",
    "share the floor plan on whatsapp",
    "brochure bhejo",
])
def test_detector_matches_brochure_requests(text):
    assert detect_brochure_request(text) is True


@pytest.mark.parametrize("text", [
    "what's the price?",
    "I'm looking for a 2 BHK",
    "is the property ready?",
    "",
])
def test_detector_ignores_generic_enquiry(text):
    assert detect_brochure_request(text) is False


def test_brochure_intent_is_sticky_across_followup_turns():
    # The caller asks for the brochure, then just says "yeah" / reads a number.
    # whatsapp_mode must persist so the agent doesn't drop back into lead capture
    # mid-exchange (the exact bug seen on the live call).
    hist = [{"role": "user", "content": "can you send me the brochure"},
            {"role": "assistant", "content": "sure, sending it now"}]
    assert brochure_intent_active("Yeah", hist) is True
    # No history → a bare "yeah" is not a brochure request.
    assert brochure_intent_active("Yeah", []) is False
    # Self-expires after the lookback window so the agent doesn't get stuck.
    stale = [{"role": "user", "content": "brochure"}] + [
        {"role": "user", "content": x} for x in ("a", "b", "c", "d")
    ]
    assert brochure_intent_active("ok", stale) is False


def test_whatsapp_block_forbids_name_email_and_lead():
    for block in (inb_fsm.mode_block_for_prompt(inb_fsm.AGENT_MODE_WHATSAPP),
                  outb_fsm.mode_block_for_prompt(outb_fsm.AGENT_MODE_WHATSAPP, [])):
        low = block.lower()
        assert "do not ask for their name" in low
        assert "email" in low
        assert "lead" in low  # explicitly says not to create a lead


# ── whatsapp_mode FSM trigger (inbound + outbound) ──────────────────────────


def test_inbound_fsm_enters_whatsapp_mode_on_intent():
    st = {"tool_flow": {"whatsapp_intent": {"kind": "brochure"}}}
    assert inb_fsm.current_mode(st) == inb_fsm.AGENT_MODE_WHATSAPP
    assert "WHATSAPP" in inb_fsm.mode_block_for_prompt(inb_fsm.AGENT_MODE_WHATSAPP)


def test_outbound_fsm_enters_whatsapp_mode_on_intent():
    st = {"tool_flow": {"whatsapp_intent": {"kind": "brochure"}}}
    assert outb_fsm.current_mode(st, ["site_visit"]) == outb_fsm.AGENT_MODE_WHATSAPP
    assert "WHATSAPP" in outb_fsm.mode_block_for_prompt(outb_fsm.AGENT_MODE_WHATSAPP, [])


def test_active_booking_flow_is_not_interrupted_by_brochure_intent():
    # Mid-booking, a brochure request must NOT abandon the site-visit flow.
    st = {"tool_flow": {
        "active": True, "flow_key": "real_estate_site_visit",
        "whatsapp_intent": {"kind": "brochure"},
    }}
    assert inb_fsm.current_mode(st) == inb_fsm.AGENT_MODE_SITE_VISIT
    assert outb_fsm.current_mode(st, ["site_visit"]) == outb_fsm.AGENT_MODE_SITE_VISIT
