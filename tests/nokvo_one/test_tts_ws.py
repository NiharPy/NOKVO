"""Sarvam WebSocket TTS — the real streaming path.

synthesize_streaming_ws opens a per-sentence WS, sends config→text→flush, and
yields audio frames as they synthesize (same chunk shape as the REST/HTTP paths,
plus connect_ms on the first). stream_sentence_tts prefers it when
SARVAM_TTS_WS_ENABLED and falls back to REST on any stall/error. Barge-in closes
the socket. Raw linear16 frames decode through the bridge's _parse_tts_audio.

Unit tests — no network; the WS is faked.
"""
from __future__ import annotations

import asyncio
import base64
import json
from types import SimpleNamespace

import numpy as np
from websockets.exceptions import ConnectionClosedOK

from app.core.config import settings
from app.services.sarvam_voice_service import SarvamVoiceService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


class _FakeServerWS:
    """A fake Sarvam WS: records sent messages, serves queued frames via recv(),
    then either blocks (mimics Sarvam holding the session open — idle-gap EOU) or
    raises ConnectionClosedOK (server-initiated close)."""

    def __init__(self, frames: list[str], *, block_at_end: bool = False) -> None:
        self._frames = list(frames)
        self._block_at_end = block_at_end
        self.sent: list[str] = []
        self.closed = False

    async def send(self, s: str) -> None:
        self.sent.append(s)

    async def recv(self) -> str:
        if self._frames:
            await asyncio.sleep(0)
            return self._frames.pop(0)
        if self._block_at_end:
            await asyncio.Event().wait()  # hold the session open (real Sarvam behavior)
        raise ConnectionClosedOK(None, None)

    async def close(self) -> None:
        self.closed = True


class _FakeClientWS:
    """The outbound websocket stream_sentence_tts writes audio to."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _tenant():
    return SimpleNamespace(provider_status={})


# ── _parse_tts_ws_message: tolerant to frame shapes ─────────────────────────

def test_parse_ws_message_shapes():
    p = SarvamVoiceService._parse_tts_ws_message
    assert p(json.dumps({"type": "audio", "data": {"audio": "AAA"}}))["audios"] == ["AAA"]
    assert p(json.dumps({"type": "audio", "audios": ["BBB", "CCC"]}))["audios"] == ["BBB", "CCC"]
    assert p(json.dumps({"audio": "DDD"}))["audios"] == ["DDD"]           # flat, no envelope
    assert p(json.dumps({"type": "done"}))["done"] is True
    assert p(json.dumps({"type": "flushed"}))["done"] is True
    assert p(json.dumps({"error": "boom"}))["error"] == "boom"
    assert p("not json") is None                                          # malformed skipped
    assert p(json.dumps([1, 2, 3])) is None                              # non-dict skipped
    # a data-wrapped audio isn't double-counted with a top-level one
    dedup = p(json.dumps({"audio": "X", "data": {"audio": "X"}}))
    assert dedup["audios"] == ["X"]


# ── synthesize_streaming_ws: sends config/text/flush, yields decoded chunks ──

def test_ws_generator_sends_protocol_and_yields_chunks(monkeypatch):
    frames = [
        json.dumps({"type": "audio", "data": {"audio": _b64(b"\x01\x00\x02\x00")}}),
        json.dumps({"type": "audio", "audios": [_b64(b"\x03\x00\x04\x00")]}),
        json.dumps({"type": "done"}),
    ]
    fake_ws = _FakeServerWS(frames)

    async def fake_connect(tenant_res, **kw):
        return fake_ws

    monkeypatch.setattr(SarvamVoiceService, "connect_tts_ws", staticmethod(fake_connect))

    async def drive():
        out = []
        async for chunk in SarvamVoiceService.synthesize_streaming_ws(_tenant(), "Hello there.", language="en"):
            out.append(chunk)
        return out

    chunks = _run(drive())
    assert [c["audio_base64"] for c in chunks] == [_b64(b"\x01\x00\x02\x00"), _b64(b"\x03\x00\x04\x00")]
    assert all(c["audio_format"] == settings.SARVAM_TTS_WS_OUTPUT_CODEC for c in chunks)
    assert all(c["sample_rate"] == settings.SARVAM_TTS_SAMPLE_RATE for c in chunks)
    assert "connect_ms" in chunks[0] and "connect_ms" not in chunks[1]   # only the first
    assert fake_ws.closed                                                # finally closed the socket
    # config → text → flush, in order
    kinds = [json.loads(m).get("type") for m in fake_ws.sent]
    assert kinds == ["config", "text", "flush"]
    cfg = json.loads(fake_ws.sent[0])["data"]
    assert cfg["output_audio_codec"] == settings.SARVAM_TTS_WS_OUTPUT_CODEC
    assert cfg["speech_sample_rate"] == settings.SARVAM_TTS_SAMPLE_RATE
    assert cfg["target_language_code"] and cfg["speaker"] and cfg["model"]
    text_msg = json.loads(fake_ws.sent[1])
    assert text_msg["data"]["text"]


def test_ws_generator_raises_on_error_frame(monkeypatch):
    fake_ws = _FakeServerWS([json.dumps({"error": "quota exceeded"})])

    async def fake_connect(tenant_res, **kw):
        return fake_ws

    monkeypatch.setattr(SarvamVoiceService, "connect_tts_ws", staticmethod(fake_connect))

    async def drive():
        async for _ in SarvamVoiceService.synthesize_streaming_ws(_tenant(), "hi", language="en"):
            pass

    try:
        _run(drive())
        assert False, "expected RuntimeError on error frame"
    except RuntimeError as exc:
        assert "quota exceeded" in str(exc)
    assert fake_ws.closed  # still closed via finally so a REST fallback is clean


# ── EOU completion: idle gap (Sarvam holds the session open, no marker) ─────

def test_ws_generator_completes_on_idle_gap(monkeypatch):
    monkeypatch.setattr(settings, "SARVAM_TTS_WS_IDLE_EOU_MS", 30)
    # Two audio frames, then the server holds the socket open (no done, no close).
    fake_ws = _FakeServerWS(
        [json.dumps({"type": "audio", "data": {"audio": _b64(b"\x01\x00")}}),
         json.dumps({"type": "audio", "data": {"audio": _b64(b"\x02\x00")}})],
        block_at_end=True,
    )

    async def fake_connect(tenant_res, **kw):
        return fake_ws

    monkeypatch.setattr(SarvamVoiceService, "connect_tts_ws", staticmethod(fake_connect))

    async def drive():
        out = []
        async for chunk in SarvamVoiceService.synthesize_streaming_ws(_tenant(), "hi", language="en"):
            out.append(chunk)
        return out

    chunks = _run(drive())
    assert [c["audio_base64"] for c in chunks] == [_b64(b"\x01\x00"), _b64(b"\x02\x00")]
    assert fake_ws.closed  # idle gap ended the utterance and closed the socket


def test_ws_generator_completes_on_server_close(monkeypatch):
    # Frames then a server-initiated close (ConnectionClosedOK) — clean end.
    fake_ws = _FakeServerWS([json.dumps({"type": "audio", "audios": [_b64(b"\x07\x00")]})])

    async def fake_connect(tenant_res, **kw):
        return fake_ws

    monkeypatch.setattr(SarvamVoiceService, "connect_tts_ws", staticmethod(fake_connect))

    async def drive():
        return [c async for c in SarvamVoiceService.synthesize_streaming_ws(_tenant(), "hi", language="en")]

    chunks = _run(drive())
    assert [c["audio_base64"] for c in chunks] == [_b64(b"\x07\x00")]
    assert fake_ws.closed


def test_ws_generator_close_before_audio_raises(monkeypatch):
    # Server closes with no audio → raise so stream_sentence_tts falls back to REST.
    fake_ws = _FakeServerWS([])  # no frames → recv raises ConnectionClosedOK immediately

    async def fake_connect(tenant_res, **kw):
        return fake_ws

    monkeypatch.setattr(SarvamVoiceService, "connect_tts_ws", staticmethod(fake_connect))

    async def drive():
        async for _ in SarvamVoiceService.synthesize_streaming_ws(_tenant(), "hi", language="en"):
            pass

    try:
        _run(drive())
        assert False, "expected RuntimeError when closed before any audio"
    except RuntimeError:
        pass
    assert fake_ws.closed


# ── Barge-in: aclose() closes the socket mid-stream ─────────────────────────

def test_ws_generator_aclose_closes_socket(monkeypatch):
    # One audio frame then the server holds the socket open — mimics a barge-in
    # while the utterance is still streaming.
    fake_ws = _FakeServerWS([json.dumps({"audio": _b64(b"\x01\x00")})], block_at_end=True)

    async def fake_connect(tenant_res, **kw):
        return fake_ws

    monkeypatch.setattr(SarvamVoiceService, "connect_tts_ws", staticmethod(fake_connect))

    async def drive():
        gen = SarvamVoiceService.synthesize_streaming_ws(_tenant(), "hi", language="en")
        first = await gen.__anext__()
        await gen.aclose()  # barge-in: consumer cancels the stream
        return first

    first = _run(drive())
    assert first["audio_base64"] == _b64(b"\x01\x00")
    assert fake_ws.closed


# ── Watchdog: a stalled WS falls back to REST via stream_sentence_tts ────────

def test_ws_first_audio_deadline_falls_back_to_rest(monkeypatch):
    monkeypatch.setattr(settings, "SARVAM_TTS_WS_ENABLED", True)
    monkeypatch.setattr(settings, "VOICE_TTS_STREAM_FIRST_AUDIO_DEADLINE_MS", 40)

    async def slow_ws(*args, **kwargs):
        await asyncio.sleep(0.4)  # first audio never arrives within the deadline
        yield {"audio_base64": "LATE", "sample_rate": settings.SARVAM_TTS_SAMPLE_RATE, "audio_format": "linear16"}

    rest_calls = {"n": 0}

    async def fake_rest(*args, **kwargs):
        rest_calls["n"] += 1
        return {"audios": ["RESTAUDIO"], "audio_format": "wav", "sample_rate": 8000}

    monkeypatch.setattr(SarvamVoiceService, "synthesize_streaming_ws", staticmethod(slow_ws))
    monkeypatch.setattr(SarvamVoiceService, "synthesize", staticmethod(fake_rest))

    ws = _FakeClientWS()
    _run(SarvamVoiceService.stream_sentence_tts(ws, _tenant(), "Fallback please.", language="en"))

    assert rest_calls["n"] == 1
    rest_audio = [m for m in ws.sent if m.get("type") == "tts_audio" and m.get("audio_base64") == "RESTAUDIO"]
    assert rest_audio, "REST fallback audio should have been emitted"


def test_ws_provider_tag_on_first_audio(monkeypatch):
    monkeypatch.setattr(settings, "SARVAM_TTS_WS_ENABLED", True)

    async def ok_ws(*args, **kwargs):
        yield {"audio_base64": "A", "sample_rate": settings.SARVAM_TTS_SAMPLE_RATE,
               "audio_format": "linear16", "connect_ms": 42}

    monkeypatch.setattr(SarvamVoiceService, "synthesize_streaming_ws", staticmethod(ok_ws))

    ws = _FakeClientWS()
    _run(SarvamVoiceService.stream_sentence_tts(ws, _tenant(), "Stream it.", language="en"))

    first = [m for m in ws.sent if m.get("type") == "tts_first_audio"]
    assert first and first[0]["provider"] == "sarvam_ws"
    assert first[0]["connect_ms"] == 42 and first[0]["streaming"] is True


# ── Codec: raw linear16 frames decode through the bridge with no change ──────

def test_linear16_frames_decode_through_bridge(monkeypatch):
    from app.services.twilio_bridge_service import _parse_tts_audio

    monkeypatch.setattr(settings, "AGENT_VOICE_BACKEND", "sarvam_pipeline")
    pcm = np.array([0, 1000, -1000, 32000, -32000], dtype=np.int16)
    raw = pcm.tobytes()  # headerless PCM16 — exactly what a WS linear16 frame carries
    samples, rate = _parse_tts_audio(raw)
    assert rate == settings.SARVAM_TTS_SAMPLE_RATE          # fallback rate == requested rate
    assert np.array_equal(samples, pcm)                     # bytes survive the round trip
