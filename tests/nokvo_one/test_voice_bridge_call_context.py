from __future__ import annotations

from types import SimpleNamespace

from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
from app.services.twilio_bridge_service import _extract_start_call_context


def test_extract_start_call_context_from_provider_start_frame():
    payload = {
        "event": "start",
        "callSid": "call-123",
        "start": {
            "streamSid": "stream-123",
            "customParameters": {
                "from_phone": "+91 75696 72503",
                "to_phone": "+91 80000 00000",
            },
        },
    }

    assert _extract_start_call_context(payload) == {
        "from_phone": "+91 75696 72503",
        "to_phone": "+91 80000 00000",
        "provider_call_id": "call-123",
    }


def test_campaign_context_merges_adapter_call_details_without_overwriting():
    adapter = SimpleNamespace(
        call_context={
            "from_phone": "+91 75696 72503",
            "to_phone": "+91 80000 00000",
            "provider_call_id": "call-123",
        }
    )

    merged = NokvoOneVoiceStreamService._campaign_context_with_adapter_call_details(
        {"from_phone": "+91 90000 00000", "campaign_id": "campaign-1"},
        adapter,
    )

    assert merged["from_phone"] == "+91 90000 00000"
    assert merged["to_phone"] == "+91 80000 00000"
    assert merged["provider_call_id"] == "call-123"
    assert merged["campaign_id"] == "campaign-1"
