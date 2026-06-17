"""Per-sentence TTS first-audio watchdog (NOKVO One voice path).

A degraded Sarvam streaming endpoint must not stall the turn for seconds: if no
first audio arrives within VOICE_TTS_STREAM_FIRST_AUDIO_DEADLINE_MS, abandon
streaming and fall back to REST so the caller still hears the sentence promptly.
Regression for the ~8s mid-answer gap seen in a live call.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

from app.core.config import settings
from app.services.sarvam_voice_service import SarvamVoiceService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeWS:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send_json(self, payload: dict) -> None:
        self.sent.append(payload)


def _audio_frames(ws: _FakeWS, value: str) -> list[dict]:
    return [m for m in ws.sent if m.get("type") == "tts_audio" and m.get("audio_base64") == value]


def test_streaming_first_audio_deadline_falls_back_to_rest(monkeypatch, caplog):
    monkeypatch.setattr(settings, "SARVAM_TTS_STREAMING_ENABLED", True)  # watchdog only runs when streaming is on
    monkeypatch.setattr(settings, "VOICE_TTS_STREAM_FIRST_AUDIO_DEADLINE_MS", 50)

    async def slow_stream(*args, **kwargs):
        # Degraded stream: first audio never arrives within the 50ms deadline.
        await asyncio.sleep(0.4)
        yield {"audio_base64": "LATE", "sample_rate": 8000, "audio_format": "wav"}

    rest_calls = {"n": 0}

    async def fake_rest(*args, **kwargs):
        rest_calls["n"] += 1
        return {"audios": ["RESTAUDIO"], "audio_format": "wav", "sample_rate": 8000}

    monkeypatch.setattr(SarvamVoiceService, "synthesize_streaming", staticmethod(slow_stream))
    monkeypatch.setattr(SarvamVoiceService, "synthesize", staticmethod(fake_rest))

    ws = _FakeWS()
    tenant = SimpleNamespace(provider_status={})

    with caplog.at_level(logging.INFO, logger="app.services.sarvam_voice_service"):
        out = _run(
            SarvamVoiceService.stream_sentence_tts(
                ws, tenant, "Skyline Heights is in Tukkuguda.", language="en"
            )
        )

    # Deadline tripped → REST fallback fired exactly once, audio still delivered.
    assert rest_calls["n"] == 1
    assert _audio_frames(ws, "RESTAUDIO")
    assert out["first_audio_ms"] is not None
    # Observability: a latency line was emitted, flagging the fallback + REST mode.
    line = next(r.getMessage() for r in caplog.records if "NOKVO-TTS-LATENCY" in r.getMessage())
    assert "fell_back=True" in line
    assert "mode=rest" in line


def test_streaming_fast_path_no_fallback(monkeypatch, caplog):
    monkeypatch.setattr(settings, "SARVAM_TTS_STREAMING_ENABLED", True)
    monkeypatch.setattr(settings, "VOICE_TTS_STREAM_FIRST_AUDIO_DEADLINE_MS", 500)

    async def fast_stream(*args, **kwargs):
        yield {"audio_base64": "FRAME1", "sample_rate": 8000, "audio_format": "wav"}
        yield {"audio_base64": "FRAME2", "sample_rate": 8000, "audio_format": "wav"}

    rest_calls = {"n": 0}

    async def fake_rest(*args, **kwargs):
        rest_calls["n"] += 1
        return {"audios": ["X"], "audio_format": "wav", "sample_rate": 8000}

    monkeypatch.setattr(SarvamVoiceService, "synthesize_streaming", staticmethod(fast_stream))
    monkeypatch.setattr(SarvamVoiceService, "synthesize", staticmethod(fake_rest))

    ws = _FakeWS()
    tenant = SimpleNamespace(provider_status={})

    with caplog.at_level(logging.INFO, logger="app.services.sarvam_voice_service"):
        out = _run(SarvamVoiceService.stream_sentence_tts(ws, tenant, "Hi there.", language="en"))

    # Streaming produced audio → no REST fallback, both frames delivered.
    assert rest_calls["n"] == 0
    assert len(_audio_frames(ws, "FRAME1")) == 1
    assert len(_audio_frames(ws, "FRAME2")) == 1
    assert out["first_audio_ms"] is not None
    line = next(r.getMessage() for r in caplog.records if "NOKVO-TTS-LATENCY" in r.getMessage())
    assert "fell_back=False" in line
    assert "mode=stream" in line


def test_streaming_disabled_goes_straight_to_rest(monkeypatch, caplog):
    # Default: the dead HTTP /stream endpoint is gated off, so REST is the
    # PRIMARY path — synthesize_streaming must not even be called (no ~2s of
    # wasted streaming latency), and it's not a "fallback".
    monkeypatch.setattr(settings, "SARVAM_TTS_STREAMING_ENABLED", False)

    stream_calls = {"n": 0}

    async def boom_stream(*args, **kwargs):
        stream_calls["n"] += 1
        yield {"audio_base64": "SHOULD_NOT_RUN", "sample_rate": 8000, "audio_format": "wav"}

    rest_calls = {"n": 0}

    async def fake_rest(*args, **kwargs):
        rest_calls["n"] += 1
        return {"audios": ["RESTAUDIO"], "audio_format": "wav", "sample_rate": 8000}

    monkeypatch.setattr(SarvamVoiceService, "synthesize_streaming", staticmethod(boom_stream))
    monkeypatch.setattr(SarvamVoiceService, "synthesize", staticmethod(fake_rest))

    ws = _FakeWS()
    tenant = SimpleNamespace(provider_status={})

    with caplog.at_level(logging.INFO, logger="app.services.sarvam_voice_service"):
        _run(SarvamVoiceService.stream_sentence_tts(ws, tenant, "Hello there.", language="en"))

    assert stream_calls["n"] == 0  # streaming endpoint never touched
    assert rest_calls["n"] == 1
    assert _audio_frames(ws, "RESTAUDIO")
    line = next(r.getMessage() for r in caplog.records if "NOKVO-TTS-LATENCY" in r.getMessage())
    assert "mode=rest" in line
    assert "fell_back=False" in line  # primary REST, not a degraded-stream fallback
