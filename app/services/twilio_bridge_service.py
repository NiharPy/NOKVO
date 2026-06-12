from __future__ import annotations

import base64
import io
import json
import wave

import numpy as np
from fastapi import WebSocket

from app.core.config import settings
from app.models.tenant_resources import TenantResources

# ---------------------------------------------------------------------------
# G.711 µ-law codec (numpy, no audioop dependency)
# ---------------------------------------------------------------------------

def _ulaw_decode(data: bytes) -> np.ndarray:
    """G.711 µ-law bytes → int16 PCM samples."""
    u = np.frombuffer(data, dtype=np.uint8).astype(np.int32)
    u = (~u) & 0xFF
    sign = u & 0x80
    exp = (u >> 4) & 0x07
    mant = u & 0x0F
    mag = (((mant << 1) | 0x21) << exp) - 0x21
    return np.where(sign, -mag, mag).astype(np.int16)


def _ulaw_encode(samples: np.ndarray) -> bytes:
    """int16 PCM samples → G.711 µ-law bytes."""
    s = np.clip(samples.astype(np.int32), -32768, 32767)
    sign = np.where(s < 0, 0x80, 0).astype(np.int32)
    s = np.abs(s)
    s = np.minimum(s, 0x1FFF) + 0x21
    exp = np.zeros(len(s), dtype=np.int32)
    for thr, e in [(0x1000, 7), (0x0800, 6), (0x0400, 5), (0x0200, 4),
                   (0x0100, 3), (0x0080, 2), (0x0040, 1)]:
        exp = np.where(s >= thr, e, exp)
    mant = ((s >> (exp + 1)) & 0x0F).astype(np.int32)
    return ((~(sign | (exp << 4) | mant)) & 0xFF).astype(np.uint8).tobytes()


def _resample(samples: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
    """Linear-interpolation resampling of int16 samples.

    When DOWNSAMPLING we first apply a short moving-average low-pass — bare
    linear interpolation has no anti-alias filter, so high-frequency content
    folds back into the band and muddies the result. Upsampling and the
    equal-rate fast path are untouched (numpy-only, no scipy)."""
    if from_rate == to_rate or len(samples) == 0:
        return samples
    work = samples.astype(np.float32)
    if from_rate > to_rate:
        win = max(2, int(round(from_rate / to_rate)))
        kernel = np.ones(win, dtype=np.float32) / win
        work = np.convolve(work, kernel, mode="same")
    n_out = max(1, int(len(samples) * to_rate / from_rate))
    x_in = np.arange(len(samples), dtype=np.float32)
    x_out = np.linspace(0, len(samples) - 1, n_out, dtype=np.float32)
    return np.interp(x_out, x_in, work).astype(np.int16)


def _parse_tts_audio(audio_bytes: bytes) -> tuple[np.ndarray, int]:
    """Extract int16 samples and sample rate from WAV or raw PCM16 bytes."""
    if audio_bytes[:4] == b"RIFF":
        try:
            with wave.open(io.BytesIO(audio_bytes)) as wf:
                rate = wf.getframerate()
                raw = wf.readframes(wf.getnframes())
                return np.frombuffer(raw, dtype=np.int16).copy(), rate
        except Exception:
            pass
    fallback_rate = (
        settings.SARVAM_TTS_SAMPLE_RATE
        if settings.AGENT_VOICE_BACKEND == "sarvam_pipeline"
        else settings.SONIOX_TTS_SAMPLE_RATE
    )
    return np.frombuffer(audio_bytes, dtype=np.int16).copy(), fallback_rate


def _first_text(*values) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _start_custom_parameters(start: dict) -> dict:
    custom = (
        start.get("customParameters")
        or start.get("custom_parameters")
        or start.get("custom_params")
        or {}
    )
    return custom if isinstance(custom, dict) else {}


def _extract_start_call_context(payload: dict) -> dict[str, str]:
    start = payload.get("start") if isinstance(payload.get("start"), dict) else {}
    custom = _start_custom_parameters(start)
    from_phone = _first_text(
        start.get("from"),
        start.get("fromNumber"),
        start.get("from_number"),
        start.get("caller"),
        start.get("callerId"),
        custom.get("from"),
        custom.get("from_phone"),
        custom.get("caller"),
        custom.get("caller_phone"),
        payload.get("from"),
    )
    to_phone = _first_text(
        start.get("to"),
        start.get("toNumber"),
        start.get("to_number"),
        custom.get("to"),
        custom.get("to_phone"),
        payload.get("to"),
    )
    call_sid = _first_text(
        payload.get("callSid"),
        payload.get("call_sid"),
        start.get("callSid"),
        start.get("call_sid"),
        start.get("callId"),
        start.get("call_id"),
        custom.get("call_sid"),
    )
    return {
        key: value
        for key, value in {
            "from_phone": from_phone,
            "to_phone": to_phone,
            "provider_call_id": call_sid,
        }.items()
        if value
    }


# ---------------------------------------------------------------------------
# WebSocket adapter: makes a Twilio Media Stream look like a browser voice WS
# ---------------------------------------------------------------------------

class TwilioWebSocketAdapter:
    """Wraps the Twilio WebSocket so AgentVoiceStreamService / AgentRealtimeVoiceService
    can call it with the same interface they use for the browser voice session."""

    # Soniox STT works at 16kHz; Azure Realtime session is configured at 24kHz.
    # We upsample to the rate the active backend expects.
    _BACKEND_INPUT_RATE: dict[str, int] = {
        "azure_realtime": 24000,
        "soniox_pipeline": 16000,
        "sarvam_pipeline": 16000,
    }

    def __init__(self, ws: WebSocket) -> None:
        self._ws = ws
        self._stream_sid = ""
        self.call_context: dict[str, str] = {}
        backend = settings.AGENT_VOICE_BACKEND
        self._input_rate = self._BACKEND_INPUT_RATE.get(backend, 16000)

    # --- interface expected by run_session() ---

    async def accept(self) -> None:
        pass  # already accepted by the endpoint

    async def receive(self) -> dict:
        while True:
            message = await self._ws.receive()
            if message.get("type") == "websocket.disconnect":
                return message
            raw = message.get("text") or ""
            if not raw:
                continue
            payload = json.loads(raw)
            event = payload.get("event")

            if event == "start":
                self._stream_sid = payload.get("streamSid", "")
                self.call_context = _extract_start_call_context(payload)

            elif event == "media":
                track = (payload.get("media") or {}).get("track")
                if track != "inbound":
                    continue
                mulaw_b64 = (payload.get("media") or {}).get("payload", "")
                if not mulaw_b64:
                    continue
                mulaw_bytes = base64.b64decode(mulaw_b64)
                pcm8k = _ulaw_decode(mulaw_bytes)
                pcm_resampled = _resample(pcm8k, 8000, self._input_rate)
                return {
                    "type": "websocket.receive",
                    "bytes": pcm_resampled.tobytes(),
                    "text": None,
                }

            elif event == "stop":
                return {"type": "websocket.disconnect", "code": 1000}

    async def send_json(self, data: dict) -> None:
        if data.get("type") != "tts_audio":
            return  # diagnostics / status events are irrelevant for a phone call
        audio_b64 = data.get("audio_base64", "")
        if not audio_b64 or not self._stream_sid:
            return
        audio_bytes = base64.b64decode(audio_b64)
        samples, rate = _parse_tts_audio(audio_bytes)
        pcm8k = _resample(samples, rate, 8000)
        mulaw_bytes = _ulaw_encode(pcm8k)
        mulaw_b64 = base64.b64encode(mulaw_bytes).decode()
        try:
            await self._ws.send_text(json.dumps({
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": mulaw_b64},
            }))
        except Exception:
            pass

    async def send_bytes(self, data: bytes) -> None:
        pass  # not used by voice services

    async def close(self, code: int = 1000) -> None:
        try:
            await self._ws.close(code=code)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Bridge service
# ---------------------------------------------------------------------------

class TwilioBridgeService:
    @staticmethod
    async def run_session(websocket: WebSocket, tenant_res: TenantResources, *, db=None) -> None:
        adapter = TwilioWebSocketAdapter(websocket)
        # Nokvo One tenants always run the Sarvam STT -> gpt-4.1-mini -> Sarvam TTS
        # pipeline, regardless of the global AGENT_VOICE_BACKEND setting.
        if (tenant_res.provider_status or {}).get("product_tier") == "nokvo_one":
            from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
            await NokvoOneVoiceStreamService.run_session(adapter, tenant_res, db=db)
            return
        if settings.AGENT_VOICE_BACKEND == "azure_realtime":
            from app.services.agent_realtime_voice_service import AgentRealtimeVoiceService
            await AgentRealtimeVoiceService.run_session(adapter, tenant_res, db=db)
        else:
            from app.services.agent_voice_stream_service import AgentVoiceStreamService
            await AgentVoiceStreamService.run_session(adapter, tenant_res, db=db)
