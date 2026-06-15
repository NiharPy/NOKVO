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
    # The mode now promises an SMS/text (WhatsApp delivery is disabled).
    assert "text" in inb_fsm.mode_block_for_prompt(inb_fsm.AGENT_MODE_WHATSAPP).lower()


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


# ── end-of-call brochure + location auto-send (inbound real-estate) ──────────


def test_end_of_call_texts_brochure_and_location_links(monkeypatch):
    """At teardown, ONE SMS with the brochure + location links goes to the
    caller's own number (the ANI), and the sms_sent flag is recorded."""
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
    from app.services.sms_service import SmsService
    import app.services.real_estate_project_service as rps

    proj = _project(
        brochure={"template": "broc_tpl"},
        location={"template": "loc_tpl", "maps_url": "https://maps/x"},
    )

    async def fake_load(db, org_id):
        return [proj]
    monkeypatch.setattr(rps, "load_active_projects", fake_load)

    sent: list[dict] = []

    async def fake_send(db, org_id, *, to_number, text, sender_override=None):
        sent.append({"to": to_number, "text": text})
        return {"ok": True}
    monkeypatch.setattr(SmsService, "send_for_org", fake_send)

    flags: dict = {}

    async def fake_merge(tenant_res, call_id, patch, **kw):
        flags.update(patch)
        return patch
    monkeypatch.setattr(
        "app.services.agent_session_store.AgentSessionStore.merge_state", fake_merge
    )

    state = {"caller_phone": "+919999999999", "memory": {}}
    _run(NokvoOneVoicePipeline._send_brochure_and_location_sms(
        db=None, org_id="org-1", tenant_res=SimpleNamespace(), call_id="call-1", state=state,
    ))

    assert len(sent) == 1                                   # ONE SMS, both links
    assert sent[0]["to"] == "+919999999999"                 # ANI is the recipient
    assert "https://x/brochure.pdf" in sent[0]["text"]      # brochure link
    assert "https://maps/x" in sent[0]["text"]              # location link
    assert "Skyline Heights" in sent[0]["text"]
    assert flags == {"sms_sent": True}


def test_end_of_call_skips_when_already_sent(monkeypatch):
    """Idempotent: if sms_sent is set, nothing is re-sent."""
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
    from app.services.sms_service import SmsService

    called: list[int] = []

    async def fake_send(*a, **k):
        called.append(1)
        return {"ok": True}
    monkeypatch.setattr(SmsService, "send_for_org", fake_send)

    state = {"caller_phone": "+919999999999", "memory": {}, "sms_sent": True}
    _run(NokvoOneVoicePipeline._send_brochure_and_location_sms(
        db=None, org_id="o", tenant_res=SimpleNamespace(), call_id="c", state=state,
    ))
    assert called == []


def test_end_of_call_skips_when_no_caller_phone(monkeypatch):
    """No ANI → nothing to send."""
    from app.services.nokvo_one_voice_pipeline import NokvoOneVoicePipeline
    from app.services.sms_service import SmsService

    called: list[int] = []

    async def fake_send(*a, **k):
        called.append(1)
        return {"ok": True}
    monkeypatch.setattr(SmsService, "send_for_org", fake_send)

    _run(NokvoOneVoicePipeline._send_brochure_and_location_sms(
        db=None, org_id="o", tenant_res=SimpleNamespace(), call_id="c", state={"memory": {}},
    ))
    assert called == []


# ── inbound site-visit slot-fill is OFF (the interrogation fix) ──────────────


def test_inbound_real_estate_does_not_start_site_visit_flow():
    """Inbound real-estate visit intent must NOT start the slot-fill that used
    to interrogate for name/phone — the call stays conversational (QUERY)."""
    from app.services.tool_flow_policy import evaluate_tool_flow_policy
    from app.services.tool_flow_questions import ensure_tool_flow_questions

    provider_status, _ = ensure_tool_flow_questions({}, "real_estate")
    result = evaluate_tool_flow_policy(
        "I want to visit the property",
        business_type="real_estate",
        provider_status=provider_status,
        history=[],
        state={"call_surface": "voice_inbound"},
        language="en",
    )
    assert result is None


def test_outbound_real_estate_still_starts_site_visit_flow():
    """Outbound campaigns whose objective is a site visit must STILL book live."""
    from app.services.tool_flow_policy import evaluate_tool_flow_policy
    from app.services.tool_flow_questions import ensure_tool_flow_questions

    provider_status, _ = ensure_tool_flow_questions({}, "real_estate")
    result = evaluate_tool_flow_policy(
        "I want to visit the property",
        business_type="real_estate",
        provider_status=provider_status,
        history=[],
        state={"call_surface": "voice_outbound"},
        language="en",
        allowed_flow_keys=["real_estate_site_visit"],
    )
    assert result is not None
    assert result.get("flow_key") == "real_estate_site_visit"


# ── prompt guardrails (timing + disambiguation) ─────────────────────────────


def test_query_block_offers_to_text_after_hangup_and_disambiguates():
    block = inb_fsm.mode_block_for_prompt(inb_fsm.AGENT_MODE_QUERY).lower()
    assert "text" in block               # SMS delivery (WhatsApp disabled)
    assert "hang up" in block            # post-disconnect timing (Flaw 1)
    assert "which" in block              # ask which project first (Flaw 3)
    assert "never ask for a phone" in block  # no interrogation


def test_whatsapp_block_defers_delivery_to_after_call():
    block = inb_fsm.mode_block_for_prompt(inb_fsm.AGENT_MODE_WHATSAPP).lower()
    assert "when the call ends" in block or "hang up" in block
    assert "already sent" in block       # forbids claiming an immediate send


# ── connect-your-WhatsApp-number flow (PlivoService) ────────────────────────


class _FakeDB:
    def add(self, obj):  # sync, like SQLAlchemy's Session.add
        pass

    async def commit(self):
        pass

    async def refresh(self, obj):
        pass


def test_connect_whatsapp_number_stores_sender_and_reports_ready(monkeypatch):
    from app.services.plivo_service import PlivoService
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)

    tr = SimpleNamespace(provider_status={"plivo": {"number": "918031321315"}})
    res = _run(PlivoService.connect_whatsapp_number(
        tr, _FakeDB(), whatsapp_number=" +91 80 3132 1315 ",
    ))
    # Normalised + stored on the tenant's own plivo config.
    assert res["whatsapp_number"] == "+918031321315"
    assert res["connected"] is True
    assert res["ready_to_send"] is True            # connected AND feature enabled
    assert tr.provider_status["plivo"]["whatsapp_number"] == "+918031321315"
    assert tr.provider_status["plivo"]["whatsapp_status"] == "connected"
    # Voice config is untouched.
    assert tr.provider_status["plivo"]["number"] == "918031321315"


def test_connect_not_ready_when_feature_disabled(monkeypatch):
    from app.services.plivo_service import PlivoService
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", False)

    tr = SimpleNamespace(provider_status={})
    res = _run(PlivoService.connect_whatsapp_number(
        tr, _FakeDB(), whatsapp_number="918031321315",
    ))
    assert res["connected"] is True
    assert res["feature_enabled"] is False
    assert res["ready_to_send"] is False           # master switch still off


def test_connect_rejects_invalid_number():
    from app.services.plivo_service import PlivoError, PlivoService

    tr = SimpleNamespace(provider_status={})
    with pytest.raises(PlivoError):
        _run(PlivoService.connect_whatsapp_number(tr, _FakeDB(), whatsapp_number="abc"))


def test_disconnect_whatsapp_number_clears_sender(monkeypatch):
    from app.services.plivo_service import PlivoService
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)

    tr = SimpleNamespace(provider_status={"plivo": {
        "whatsapp_number": "918031321315", "number": "918031321315",
    }})
    res = _run(PlivoService.disconnect_whatsapp_number(tr, _FakeDB()))
    assert res["connected"] is False
    assert res["ready_to_send"] is False
    assert "whatsapp_number" not in tr.provider_status["plivo"]
    assert tr.provider_status["plivo"]["number"] == "918031321315"  # voice intact


def test_whatsapp_link_response_not_connected(monkeypatch):
    from app.services.plivo_service import PlivoService
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)
    res = PlivoService.whatsapp_link_response(SimpleNamespace(provider_status={}))
    assert res["connected"] is False
    assert res["whatsapp_number"] is None
    assert res["ready_to_send"] is False           # nothing to send from


# ── per-PROJECT WhatsApp sender (overrides the tenant number) ────────────────


def test_resolvers_surface_project_sender_number():
    p = _project(
        sender_number="+91PROJECT",
        brochure={"template": "b"},
        location={"template": "l", "maps_url": "https://maps/x"},
    )
    assert project_whatsapp_brochure(p)["sender"] == "+91PROJECT"
    assert project_whatsapp_location(p)["sender"] == "+91PROJECT"
    # No per-project number configured → None (falls back to the tenant sender).
    assert project_whatsapp_brochure(_project(brochure={"template": "b"}))["sender"] is None


def _capture_src(monkeypatch):
    """Patch the Plivo REST + auth so a send is captured, not performed."""
    captured: dict = {}

    async def fake_request(method, url, *, auth, json_body=None):
        captured["body"] = json_body
        return {"message_uuid": "m1"}

    monkeypatch.setattr("app.services.plivo_service.PlivoService._request", fake_request)
    monkeypatch.setattr("app.services.plivo_service.PlivoService._master_auth", lambda: ("aid", "atok"))
    return captured


def test_send_template_prefers_project_sender_override(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)
    captured = _capture_src(monkeypatch)
    res = _run(WhatsAppService.send_template(
        tenant_res=_tenant({"whatsapp_number": "+91TENANT"}),
        to_number="+919999999999",
        template_name="project_brochure",
        sender_override="+91PROJECT",
    ))
    assert res["ok"] is True
    assert captured["body"]["src"] == "+91PROJECT"   # the project's number wins


def test_send_template_falls_back_to_tenant_when_no_override(monkeypatch):
    monkeypatch.setattr(settings, "PLIVO_WHATSAPP_ENABLED", True)
    captured = _capture_src(monkeypatch)
    res = _run(WhatsAppService.send_template(
        tenant_res=_tenant({"whatsapp_number": "+91TENANT"}),
        to_number="+919999999999",
        template_name="project_brochure",
    ))
    assert res["ok"] is True
    assert captured["body"]["src"] == "+91TENANT"    # tenant sender when no project override
