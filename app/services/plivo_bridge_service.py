"""Plivo Audio Streaming ↔ Voice Agent bridge.

Plivo's bidirectional ``<Stream>`` is Twilio-like:
  - JSON events: start, media (base64 L16 PCM @ 8 kHz), stop
  - Playback back to Plivo uses a ``playAudio`` event (NOT ``media``) with
    ``{contentType, sampleRate, payload}`` — the one real divergence from Exotel.
  - No "connected" handshake is required (unlike Exotel).

Audio conversion reuses the numpy codec from twilio_bridge_service.
"""
from __future__ import annotations

import asyncio
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
        # Rate Plivo actually streams at — read from the `start` event's media
        # format; assume the configured default until then.
        self._source_rate = int(settings.PLIVO_DEFAULT_SAMPLE_RATE or 8000)
        # Encoding Plivo actually streams. Our answer XML requests
        # audio/x-l16, so L16 is the default; µ-law only when the start
        # event's media format explicitly says so. Decoding L16 as µ-law
        # turns caller audio into pure noise — never assume µ-law.
        self._source_encoding = "l16"
        # Inbound conditioner (AGC + DC removal) → cleaner audio for STT.
        self._enhancer = None
        if settings.VOICE_STT_AGC_ENABLED:
            from app.services.agent_robustness import AudioEnhancer
            self._enhancer = AudioEnhancer(target_dbfs=settings.VOICE_STT_AGC_TARGET_DBFS)
        # RNNoise speech denoise — BEFORE the AGC so gain never amplifies the
        # noise floor the denoiser removes. Best-effort: unavailable lib →
        # None and audio passes straight to the enhancer.
        self._denoiser = None
        if settings.VOICE_STT_DENOISE_ENABLED:
            try:
                from app.services.audio_denoise import SpeechDenoiser
                denoiser = SpeechDenoiser()
                self._denoiser = denoiser if denoiser.available else None
            except Exception:
                self._denoiser = None
        self._audio_chunks = 0  # for periodic level logging

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
                    # DSP (resample + RNNoise + AGC) is CPU-bound; run it off the
                    # event loop so it can't add latency jitter to other live
                    # calls sharing this worker. Frames for THIS call stay serial
                    # (we await before the next receive), so per-call RNNoise/AGC
                    # state is never touched concurrently.
                    processed = await asyncio.to_thread(self._process_inbound, raw_bytes)
                    return {"type": "websocket.receive", "bytes": processed, "text": None}
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
                # Drive the sample rate from what Plivo ACTUALLY streams (its key
                # varies across versions) instead of assuming the configured
                # default — feeding 8 kHz audio to a 16 kHz-labelled STT garbles
                # everything. Log it so the real rate is finally observable.
                detected, detected_encoding = self._detect_stream_format(payload, start_data)
                if detected:
                    self._source_rate = detected
                if detected_encoding:
                    self._source_encoding = detected_encoding
                _log.warning(
                    "[PLIVO-WS] NOKVO-AUDIO: stream rate=%s Hz enc=%s → STT %s Hz (agc=%s)",
                    self._source_rate, self._source_encoding, self._input_rate,
                    "on" if self._enhancer is not None else "off",
                )
                if detected and detected != int(settings.PLIVO_DEFAULT_SAMPLE_RATE or 0):
                    # Forwarded-call diagnostic: the carrier negotiated a
                    # different rate than we asked for. Resampling handles it,
                    # but it's the first thing to check on a garbled call.
                    _log.error(
                        "[PLIVO-WS] NOKVO-AUDIO: stream rate mismatch plivo=%s configured=%s",
                        detected, settings.PLIVO_DEFAULT_SAMPLE_RATE,
                    )
                ctx = _extract_start_call_context(payload)
                # Preserve the ANI pre-seeded from the media URL (?caller=…) when the
                # start event doesn't carry a caller number — don't lose it.
                prev = getattr(self, "call_context", None) or {}
                if not ctx.get("from_phone") and prev.get("from_phone"):
                    ctx["from_phone"] = prev["from_phone"]
                self.call_context = ctx
                _log.warning("[PLIVO-WS] stream started sid=%s from=%s", self._stream_sid, ctx.get("from_phone"))
            elif event == "media":
                media = payload.get("media") or {}
                if media.get("track") != "inbound":
                    continue
                pcm_b64 = media.get("payload", "")
                if not pcm_b64:
                    continue
                raw_pcm = base64.b64decode(pcm_b64)
                # Offload CPU-bound DSP to a worker thread (see note above).
                processed = await asyncio.to_thread(self._process_inbound, raw_pcm)
                return {"type": "websocket.receive", "bytes": processed, "text": None}
            elif event in ("stop", "closed"):
                return {"type": "websocket.disconnect", "code": 1000}

    @staticmethod
    def _detect_stream_format(payload: dict, start_data: dict) -> tuple[int | None, str | None]:
        """Best-effort read of Plivo's true stream (sample_rate, encoding).

        Plivo's keys have varied across versions (mediaFormat.sampleRate /
        media_format.rate / a top-level sampleRate; encoding under
        encoding / contentType / content_type), so probe a few. Returns
        (None, None) components when nothing parseable. Encoding is
        normalized to "l16" or "mulaw"."""
        media_format = (
            start_data.get("mediaFormat")
            or start_data.get("media_format")
            or payload.get("mediaFormat")
            or {}
        )
        rate: int | None = None
        for candidate in (
            media_format.get("sampleRate") if isinstance(media_format, dict) else None,
            media_format.get("sample_rate") if isinstance(media_format, dict) else None,
            media_format.get("rate") if isinstance(media_format, dict) else None,
            start_data.get("sampleRate"),
            start_data.get("sample_rate"),
        ):
            try:
                parsed = int(candidate)
            except (TypeError, ValueError):
                continue
            if 4000 <= parsed <= 48000:
                rate = parsed
                break

        encoding: str | None = None
        enc_raw = ""
        if isinstance(media_format, dict):
            enc_raw = str(
                media_format.get("encoding")
                or media_format.get("contentType")
                or media_format.get("content_type")
                or ""
            ).lower()
        if not enc_raw:
            enc_raw = str(start_data.get("contentType") or start_data.get("content_type") or "").lower()
        if "mulaw" in enc_raw or "ulaw" in enc_raw or "x-mulaw" in enc_raw:
            encoding = "mulaw"
        elif "l16" in enc_raw or "linear" in enc_raw or "pcm" in enc_raw:
            encoding = "l16"
        return rate, encoding

    def _process_inbound(self, raw_pcm: bytes) -> bytes:
        """Decode caller audio per the stream's actual encoding, resample from
        Plivo's true rate to the STT rate, then condition it (AGC + DC
        removal) so STT resolves quiet/uneven speech. Inbound only — never
        touches the agent's TTS playback."""
        import numpy as np
        from app.services.twilio_bridge_service import _resample, _ulaw_decode

        if self._source_encoding == "mulaw":
            samples = _ulaw_decode(raw_pcm)
        else:
            # L16 — what our answer XML requests. Trim a trailing odd byte so
            # a torn frame never crashes the int16 view.
            usable = len(raw_pcm) - (len(raw_pcm) % 2)
            samples = np.frombuffer(raw_pcm[:usable], dtype=np.int16)
        pcm = _resample(samples, self._source_rate, self._input_rate)
        out = pcm.tobytes()
        # Order matters: denoise BEFORE AGC — otherwise the gain stage
        # amplifies the very noise floor RNNoise would have removed, and the
        # enhancer's RMS tracking sees noise instead of speech level.
        if self._denoiser is not None:
            out = self._denoiser.process(out, rate=self._input_rate)
        if self._enhancer is not None:
            out = self._enhancer.process(out)
            # Periodic level log (~every 2 s at typical chunk sizes) so a quiet
            # line / clipping is visible instead of guessed.
            self._audio_chunks += 1
            if self._audio_chunks % 100 == 0:
                _log.info(
                    "[PLIVO-WS] NOKVO-AUDIO: rms=%.4f gain=%.2f denoise=%s (chunk %d)",
                    self._enhancer.last_rms, self._enhancer.gain,
                    f"{self._denoiser.last_speech_prob:.2f}" if self._denoiser is not None else "off",
                    self._audio_chunks,
                )
        return out

    def close_audio(self) -> None:
        """Release per-call DSP state (RNNoise). Idempotent."""
        if self._denoiser is not None:
            try:
                self._denoiser.close()
            except Exception:
                pass
            self._denoiser = None


class PlivoBridgeService:
    @staticmethod
    async def run_session(
        websocket: WebSocket, tenant_res, *, db=None, language: str = "en", caller_phone: str | None = None
    ) -> None:
        adapter = PlivoWebSocketAdapter(websocket, language=language)
        # Seed the caller's number (ANI) from the call signaling so the agent can
        # auto-fill the phone slot (no spoken-digit capture). _merge_call_context
        # folds call_context.from_phone into campaign_context downstream.
        if caller_phone:
            ctx = dict(getattr(adapter, "call_context", None) or {})
            ctx.setdefault("from_phone", caller_phone)
            adapter.call_context = ctx
        # Nokvo One tenants always run the Sarvam STT → pooled LLM → Sarvam TTS
        # pipeline — REGARDLESS of the global AGENT_VOICE_BACKEND. The adapter's
        # input rate was seeded from that global (e.g. azure_realtime → 24 kHz),
        # so we MUST realign it to the rate Sarvam STT is actually told
        # (SARVAM_STT_SAMPLE_RATE). Otherwise the bridge upsamples caller audio
        # to 24 kHz while Sarvam reads it as 16 kHz → 1.5× slow → garbled STT.
        try:
            if (tenant_res.provider_status or {}).get("product_tier") == "nokvo_one":
                from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
                adapter._input_rate = int(settings.SARVAM_STT_SAMPLE_RATE)
                await NokvoOneVoiceStreamService.run_session(adapter, tenant_res, db=db, language=language)
                return
            if settings.AGENT_VOICE_BACKEND == "azure_realtime":
                from app.services.agent_realtime_voice_service import AgentRealtimeVoiceService
                await AgentRealtimeVoiceService.run_session(adapter, tenant_res, db=db, language=language)
            elif settings.AGENT_VOICE_BACKEND == "sarvam_pipeline":
                from app.services.nokvo_one_voice_stream_service import NokvoOneVoiceStreamService
                adapter._input_rate = int(settings.SARVAM_STT_SAMPLE_RATE)
                await NokvoOneVoiceStreamService.run_session(adapter, tenant_res, db=db, language=language)
            else:
                from app.services.agent_voice_stream_service import AgentVoiceStreamService
                await AgentVoiceStreamService.run_session(adapter, tenant_res, db=db)
        finally:
            adapter.close_audio()
