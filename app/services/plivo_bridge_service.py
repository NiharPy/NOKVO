"""Plivo Audio Streaming ↔ Voice Agent bridge.

Plivo's bidirectional ``<Stream>`` is Twilio-like:
  - JSON events: start, media (base64 L16 PCM @ 8 kHz), stop
  - Playback back to Plivo uses a ``playAudio`` event (NOT ``media``) with
    ``{contentType, sampleRate, payload}`` — the one real divergence from Exotel.
  - No "connected" handshake is required (unlike Exotel).

Audio conversion reuses the numpy codec from twilio_bridge_service.
"""
from __future__ import annotations

import base64
import json
import logging

from fastapi import WebSocket

from app.core.config import settings
from app.services.twilio_bridge_service import TwilioWebSocketAdapter, _extract_start_call_context

_log = logging.getLogger("plivo_ws")


class PlivoWebSocketAdapter(TwilioWebSocketAdapter):
    """Plivo Audio Streaming adapter."""

    def __init__(self, ws, *, language: str = "en") -> None:
        super().__init__(ws)
        self._stream_sid = "plivo-stream"
        self._pending_config: dict | None = {"type": "config", "language": language}

    async def accept(self) -> None:
        try:
            await self._ws.accept()
        except RuntimeError:
            pass

    async def send_json(self, data: dict) -> None:
        # Playback: Plivo expects an explicit `playAudio` event with L16 8 kHz PCM.
        if data.get("type") != "tts_audio":
            return
        audio_b64 = data.get("audio_base64", "")
        if not audio_b64:
            return
        from app.services.twilio_bridge_service import _parse_tts_audio, _resample
        audio_bytes = base64.b64decode(audio_b64)
        samples, rate = _parse_tts_audio(audio_bytes)
        rate_out = settings.PLIVO_DEFAULT_SAMPLE_RATE or 8000
        pcm = _resample(samples, rate, rate_out)
        pcm_b64 = base64.b64encode(pcm.tobytes()).decode()
        try:
            await self._ws.send_text(json.dumps({
                "event": "playAudio",
                "media": {
                    "contentType": "audio/x-l16",
                    "sampleRate": rate_out,
                    "payload": pcm_b64,
                },
            }))
        except Exception as exc:  # noqa: BLE001 — best-effort playback
            _log.debug("[PLIVO-WS] playAudio send failed: %s", exc)

    async def clear_playback(self) -> None:
        """Stop buffered playback on barge-in (Plivo `clearAudio`)."""
        try:
            await self._ws.send_text(json.dumps({"event": "clearAudio"}))
        except Exception:  # noqa: BLE001
            pass

    async def receive(self) -> dict:
        if self._pending_config is not None:
            cfg = self._pending_config
            self._pending_config = None
            return {"type": "websocket.receive", "text": json.dumps(cfg), "bytes": None}
        import numpy as np
        from app.services.twilio_bridge_service import _resample
        while True:
            message = await self._ws.receive()
            if message.get("type") == "websocket.disconnect":
                return message
            raw = message.get("text") or ""
            if not raw:
                raw_bytes = message.get("bytes")
                if raw_bytes:
                    pcm8k = np.frombuffer(raw_bytes, dtype=np.int16)
                    pcm = _resample(pcm8k, settings.PLIVO_DEFAULT_SAMPLE_RATE or 8000, self._input_rate)
                    return {"type": "websocket.receive", "bytes": pcm.tobytes(), "text": None}
                continue
            try:
                payload = json.loads(raw)
            except Exception:
                continue
            event = payload.get("event")
            if event == "start":
                start_data = payload.get("start") or {}
                self._stream_sid = (
                    payload.get("streamId") or payload.get("stream_id")
                    or start_data.get("streamId") or start_data.get("callId")
                    or payload.get("callId") or self._stream_sid
                )
                self.call_context = _extract_start_call_context(payload)
                _log.warning("[PLIVO-WS] stream started sid=%s", self._stream_sid)
            elif event == "media":
                media = payload.get("media") or {}
                if media.get("track") == "outbound":
                    continue
                pcm_b64 = media.get("payload", "")
                if not pcm_b64:
                    continue
                raw_pcm = base64.b64decode(pcm_b64)
                pcm8k = np.frombuffer(raw_pcm, dtype=np.int16)
                pcm = _resample(pcm8k, settings.PLIVO_DEFAULT_SAMPLE_RATE or 8000, self._input_rate)
                return {"type": "websocket.receive", "bytes": pcm.tobytes(), "text": None}
            elif event in ("stop", "closed"):
                return {"type": "websocket.disconnect", "code": 1000}


class PlivoBridgeService:
    @staticmethod
    async def run_session(websocket: WebSocket, tenant_res, *, db=None, language: str = "en") -> None:
        adapter = PlivoWebSocketAdapter(websocket, language=language)
        # Nokvo One tenants always run the Sarvam STT → pooled LLM → Sarvam TTS pipeline.
        if (tenant_res.provider_status or {}).get("product_tier") == "nokvo_one":
            from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
            await NokvoOneVoiceStreamService.run_session(adapter, tenant_res, db=db, language=language)
            return
        if settings.AGENT_VOICE_BACKEND == "azure_realtime":
            from app.services.agent_realtime_voice_service import AgentRealtimeVoiceService
            await AgentRealtimeVoiceService.run_session(adapter, tenant_res, db=db, language=language)
        elif settings.AGENT_VOICE_BACKEND == "sarvam_pipeline":
            from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
            await NokvoOneVoiceStreamService.run_session(adapter, tenant_res, db=db, language=language)
        else:
            from app.services.agent_voice_stream_service import AgentVoiceStreamService
            await AgentVoiceStreamService.run_session(adapter, tenant_res, db=db)
