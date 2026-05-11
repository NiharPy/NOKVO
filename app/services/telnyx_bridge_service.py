"""Telnyx Media Stream ↔ Voice Agent bridge.

Telnyx's bidirectional streaming protocol mirrors Twilio's:
  - JSON events: connected, start, media (MULAW 8 kHz base64), stop
  - Outbound audio: media event with base64 MULAW payload

Audio conversion reuses the numpy codec from twilio_bridge_service.
"""
from __future__ import annotations

from fastapi import WebSocket

from app.core.config import settings
from app.services.twilio_bridge_service import TwilioWebSocketAdapter


class TelnyxWebSocketAdapter(TwilioWebSocketAdapter):
    """Telnyx uses the same media-stream event schema as Twilio — no changes needed."""


class TelnyxBridgeService:
    @staticmethod
    async def run_session(
        websocket: WebSocket,
        tenant_res,
        *,
        db=None,
        campaign_chunks: list[dict] | None = None,
    ) -> None:
        adapter = TelnyxWebSocketAdapter(websocket)
        if settings.AGENT_VOICE_BACKEND == "azure_realtime":
            from app.services.agent_realtime_voice_service import AgentRealtimeVoiceService
            await AgentRealtimeVoiceService.run_session(adapter, tenant_res, db=db)
        else:
            from app.services.agent_voice_stream_service import AgentVoiceStreamService
            await AgentVoiceStreamService.run_session(
                adapter, tenant_res, db=db, campaign_chunks=campaign_chunks
            )
