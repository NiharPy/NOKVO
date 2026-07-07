"""TTS Redis byte-cache in SarvamVoiceService.synthesize (opt-in via cache=True).

Pins: a hit returns cached audio with NO HTTP call; a miss stores; the key changes
when any byte-affecting input changes; a Redis error falls through to live synth;
caching is skipped when cache=False or TTS_CACHE_ENABLED=False. HTTP + Redis are
mocked so this is fast and offline.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.sarvam_voice_service import SarvamVoiceService
import app.services.agent_session_store as ass


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _FakeRedis:
    def __init__(self):
        self.store: dict[str, str] = {}
        self.raise_on = None  # "get" | "set" to simulate redis errors

    async def get(self, key):
        if self.raise_on == "get":
            raise RuntimeError("redis down")
        return self.store.get(key)

    async def exists(self, key):
        if self.raise_on == "get":
            raise RuntimeError("redis down")
        return 1 if key in self.store else 0

    async def setex(self, key, ttl, value):
        if self.raise_on == "set":
            raise RuntimeError("redis down")
        self.store[key] = value


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, payload):
        self.payload = payload
        self.calls = 0

    async def post(self, url, **kwargs):
        self.calls += 1
        return _FakeResp(200, self.payload)


def _patch(monkeypatch, http_payload=None):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(ass.AgentSessionStore, "_client", fake_redis, raising=False)
    monkeypatch.setattr(ass.AgentSessionStore, "client", classmethod(lambda cls: fake_redis))
    http = _FakeHTTP(http_payload or {"audios": ["QUJD"], "request_id": "r1"})
    monkeypatch.setattr(SarvamVoiceService, "http_client", staticmethod(lambda: http))

    async def fake_key(tenant_res, kind):
        return "k"

    monkeypatch.setattr(SarvamVoiceService, "api_key", staticmethod(fake_key))
    monkeypatch.setattr(settings, "TTS_CACHE_ENABLED", True)
    return fake_redis, http


def _tr():
    return SimpleNamespace(provider_status={})


def test_miss_calls_http_and_stores(monkeypatch):
    redis, http = _patch(monkeypatch)
    res = _run(SarvamVoiceService.synthesize(_tr(), "Hello there", language="en", cache=True))
    assert http.calls == 1 and res["cached"] is False and res["audios"] == ["QUJD"]
    assert len(redis.store) == 1  # stored for reuse


def test_hit_returns_cached_without_http(monkeypatch):
    redis, http = _patch(monkeypatch)
    _run(SarvamVoiceService.synthesize(_tr(), "Hello there", language="en", cache=True))  # populate
    http.calls = 0
    res = _run(SarvamVoiceService.synthesize(_tr(), "Hello there", language="en", cache=True))
    assert http.calls == 0                # served from cache, no Sarvam call
    assert res["cached"] is True and res["audios"] == ["QUJD"]


def test_cache_off_never_caches(monkeypatch):
    redis, http = _patch(monkeypatch)
    _run(SarvamVoiceService.synthesize(_tr(), "Hello", language="en", cache=False))
    assert http.calls == 1 and redis.store == {}   # cache=False → no store
    # cache=True but the global flag off → also no store
    monkeypatch.setattr(settings, "TTS_CACHE_ENABLED", False)
    _run(SarvamVoiceService.synthesize(_tr(), "Hello", language="en", cache=True))
    assert redis.store == {}


def test_key_changes_with_language_and_prosody(monkeypatch):
    redis, http = _patch(monkeypatch)
    _run(SarvamVoiceService.synthesize(_tr(), "Hi", language="en", cache=True))
    _run(SarvamVoiceService.synthesize(_tr(), "Hi", language="hi", cache=True))       # diff lang
    _run(SarvamVoiceService.synthesize(_tr(), "Hi", language="en", pace=1.5, cache=True))  # diff pace
    assert len(redis.store) == 3          # three distinct keys


def test_same_text_same_key_dedupes(monkeypatch):
    redis, http = _patch(monkeypatch)
    _run(SarvamVoiceService.synthesize(_tr(), "Same line", language="en", cache=True))
    _run(SarvamVoiceService.synthesize(_tr(), "Same line", language="en", cache=True))
    assert len(redis.store) == 1          # identical inputs → one entry


def test_redis_get_error_falls_through_to_http(monkeypatch):
    redis, http = _patch(monkeypatch)
    redis.raise_on = "get"
    res = _run(SarvamVoiceService.synthesize(_tr(), "Hello", language="en", cache=True))
    assert http.calls == 1 and res["audios"] == ["QUJD"]  # error never breaks synth


def test_empty_audios_not_cached(monkeypatch):
    redis, http = _patch(monkeypatch, http_payload={"audios": [], "request_id": "r"})
    _run(SarvamVoiceService.synthesize(_tr(), "Hello", language="en", cache=True))
    assert redis.store == {}               # nothing to serve later


# ── cache probe + streaming short-circuit ──
# stream_sentence_tts consults the local cache BEFORE picking a streaming source:
# a warm scripted line skips WS streaming and is served from Redis via the REST
# branch (synthesize's own hit). The probe must compute the SAME key synthesize
# does, or the short-circuit never fires / fires wrongly.


def test_probe_matches_synthesize_key(monkeypatch):
    redis, http = _patch(monkeypatch)
    probe = SarvamVoiceService.tts_cached_audio_available
    assert _run(probe(_tr(), "Hello there", language="en")) is False
    _run(SarvamVoiceService.synthesize(_tr(), "Hello there", language="en", cache=True))
    assert _run(probe(_tr(), "Hello there", language="en")) is True
    # Any byte-affecting difference is a different key — no false hit.
    assert _run(probe(_tr(), "Hello there", language="en", pace=1.5)) is False
    assert _run(probe(_tr(), "Hello there", language="hi")) is False


def test_probe_error_or_flag_off_is_false(monkeypatch):
    redis, http = _patch(monkeypatch)
    probe = SarvamVoiceService.tts_cached_audio_available
    _run(SarvamVoiceService.synthesize(_tr(), "Hello", language="en", cache=True))
    redis.raise_on = "get"
    assert _run(probe(_tr(), "Hello", language="en")) is False  # redis error → stream as usual
    redis.raise_on = None
    monkeypatch.setattr(settings, "TTS_CACHE_ENABLED", False)
    assert _run(probe(_tr(), "Hello", language="en")) is False


class _FakeWS:
    def __init__(self):
        self.sent = []

    async def send_json(self, obj):
        self.sent.append(obj)


def test_stream_sentence_short_circuits_streaming_on_cache_hit(monkeypatch):
    redis, http = _patch(monkeypatch)
    _run(SarvamVoiceService.synthesize(_tr(), "Scripted line", language="en", cache=True))  # warm
    http.calls = 0
    monkeypatch.setattr(settings, "SARVAM_TTS_WS_ENABLED", True)

    def _no_ws(*args, **kwargs):
        raise AssertionError("streaming must not run when the line is cached")

    monkeypatch.setattr(SarvamVoiceService, "synthesize_streaming_ws", staticmethod(_no_ws))
    ws = _FakeWS()
    _run(SarvamVoiceService.stream_sentence_tts(ws, _tr(), "Scripted line", language="en", cache=True))
    audio = [m for m in ws.sent if m.get("type") == "tts_audio"]
    assert audio and audio[0]["audio_base64"] == "QUJD"  # served from the cache
    assert http.calls == 0                               # no live Sarvam call


def test_stream_sentence_streams_on_cache_miss(monkeypatch):
    redis, http = _patch(monkeypatch)
    monkeypatch.setattr(settings, "SARVAM_TTS_WS_ENABLED", True)

    async def _ws_stream(*args, **kwargs):
        yield {"audio_base64": "V1M=", "audio_format": "wav", "sample_rate": 8000}

    monkeypatch.setattr(SarvamVoiceService, "synthesize_streaming_ws", staticmethod(_ws_stream))
    ws = _FakeWS()
    _run(SarvamVoiceService.stream_sentence_tts(ws, _tr(), "Never cached", language="en", cache=True))
    audio = [m for m in ws.sent if m.get("type") == "tts_audio"]
    assert audio and audio[0]["audio_base64"] == "V1M="  # streamed, not REST
    assert http.calls == 0
