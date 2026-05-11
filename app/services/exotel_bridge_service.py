"""Exotel Media Stream ↔ Voice Agent bridge.

Exotel's ExoStream bidirectional streaming protocol is compatible with
Twilio's media stream format:
  - JSON events: connected, start, media (MULAW 8 kHz base64), stop
  - Outbound audio: media event with base64 MULAW payload

Audio conversion reuses the numpy codec from twilio_bridge_service.
"""
from __future__ import annotations

from fastapi import WebSocket

from app.core.config import settings
from app.services.twilio_bridge_service import TwilioWebSocketAdapter


import base64
import json
import logging

_log = logging.getLogger("exotel_ws")

# Write a debug dump to /tmp so we can inspect it
import os as _os
_DUMP = "/Users/harish/Desktop/NOKVO/exotel_debug.log"


def _dump(msg: str) -> None:
    with open(_DUMP, "a") as _f:
        _f.write(msg + "\n")


class ExotelWebSocketAdapter(TwilioWebSocketAdapter):
    """Exotel ExoStream adapter."""

    def __init__(self, ws, *, language: str = "en") -> None:
        super().__init__(ws)
        # Pre-set so send_json works even before Exotel sends a start event
        self._stream_sid = "exotel-stream"
        self._pending_config: dict | None = {"type": "config", "language": language}

    async def accept(self) -> None:
        # Send an initial "connected" handshake — Exotel may require this before streaming
        try:
            await self._ws.send_text(json.dumps({
                "event": "connected",
                "protocol": "Call",
                "version": "1.0",
            }))
            _dump("SENT connected handshake")
        except Exception as e:
            _dump(f"HANDSHAKE SEND FAILED: {e}")

    async def send_json(self, data: dict) -> None:
        if data.get("type") != "tts_audio":
            return
        audio_b64 = data.get("audio_base64", "")
        if not audio_b64:
            return
        from app.services.twilio_bridge_service import _parse_tts_audio, _resample
        import base64 as _b64
        audio_bytes = _b64.b64decode(audio_b64)
        samples, rate = _parse_tts_audio(audio_bytes)
        # _resample returns int16; Exotel Voicebot expects linear16 PCM at 8 kHz
        pcm8k = _resample(samples, rate, 8000)
        pcm_b64 = _b64.b64encode(pcm8k.tobytes()).decode()
        try:
            await self._ws.send_text(json.dumps({
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": pcm_b64},
            }))
            _dump(f"SENT audio chunk sid={self._stream_sid} len={len(pcm_b64)}")
        except Exception as e:
            _dump(f"SEND AUDIO FAILED: {e}")

    async def receive(self) -> dict:
        if self._pending_config is not None:
            cfg = self._pending_config
            self._pending_config = None
            return {"type": "websocket.receive", "text": json.dumps(cfg), "bytes": None}
        _dump("receive() entered — waiting for Exotel message")
        while True:
            message = await self._ws.receive()
            if message.get("type") == "websocket.disconnect":
                return message

            raw = message.get("text") or ""
            if not raw:
                raw_bytes = message.get("bytes")
                _dump(f"BINARY frame len={len(raw_bytes or b'')}")
                if raw_bytes:
                    import numpy as np
                    from app.services.twilio_bridge_service import _resample
                    pcm8k = np.frombuffer(raw_bytes, dtype=np.int16)
                    pcm = _resample(pcm8k, 8000, self._input_rate)
                    return {"type": "websocket.receive", "bytes": pcm.tobytes(), "text": None}
                continue

            try:
                payload = json.loads(raw)
            except Exception:
                _dump(f"NON-JSON: {raw[:200]}")
                continue

            event = payload.get("event")
            _dump(f"EVENT={event} keys={list(payload.keys())} raw={raw[:300]}")

            if event == "start":
                start_data = payload.get("start") or {}
                self._stream_sid = (
                    payload.get("streamSid") or payload.get("stream_sid") or
                    start_data.get("streamSid") or start_data.get("stream_sid") or
                    payload.get("callSid") or payload.get("call_sid") or
                    self._stream_sid
                )
                _log.warning("[EXOTEL-WS] stream started sid=%s media_format=%s", self._stream_sid, start_data.get("media_format"))
                _dump(f"START media_format={start_data.get('media_format')} full_start={json.dumps(start_data)}")

            elif event == "media":
                media = payload.get("media") or {}
                track = media.get("track")
                if track == "outbound":
                    continue
                pcm_b64 = media.get("payload", "")
                if not pcm_b64:
                    continue
                # Exotel Voicebot sends linear16 PCM (int16 LE) at 8 kHz
                import numpy as np
                from app.services.twilio_bridge_service import _resample
                raw = base64.b64decode(pcm_b64)
                # Pass int16 directly — _resample handles int16 input correctly
                pcm8k = np.frombuffer(raw, dtype=np.int16)
                pcm = _resample(pcm8k, 8000, self._input_rate)
                return {"type": "websocket.receive", "bytes": pcm.tobytes(), "text": None}

            elif event == "stop":
                return {"type": "websocket.disconnect", "code": 1000}


class ExotelBridgeService:
    @staticmethod
    async def run_session(
        websocket: WebSocket,
        tenant_res,
        *,
        db=None,
        language: str = "en",
    ) -> None:
        adapter = ExotelWebSocketAdapter(websocket, language=language)
        if settings.AGENT_VOICE_BACKEND == "azure_realtime":
            from app.services.agent_realtime_voice_service import AgentRealtimeVoiceService
            await AgentRealtimeVoiceService.run_session(adapter, tenant_res, db=db, language=language)
        else:
            from app.services.agent_voice_stream_service import AgentVoiceStreamService
            await AgentVoiceStreamService.run_session(adapter, tenant_res, db=db)
