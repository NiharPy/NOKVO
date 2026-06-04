"""End-to-end-ish provider behaviour under burst.

These tests don't hit real Sarvam / Azure endpoints — they substitute a
controllable fake transport so the retry / backoff / fallback paths can
be exercised deterministically. The goal is to prove that:

* Sarvam STT honours ``Retry-After`` on 429 and eventually succeeds when
  the upstream stops throttling.
* Sarvam TTS falls back to a no-prosody body when the server rejects
  the prosody params.
* Azure OpenAI streaming retries on 429 and surfaces the rate-limit
  fallback when the upstream keeps throttling.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace
from typing import Any

import httpx
import pytest

from app.core.config import settings
from app.services.sarvam_voice_service import SarvamVoiceService
from app.services.nokvo_one_voice_pipeline import (
    AzureGroundedLLM,
    NokvoOneAgentRateLimited,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# ── Sarvam STT 429 burst ────────────────────────────────────────────────────


def test_sarvam_api_key_prefers_env_override(monkeypatch):
    """A freshly rotated platform key in .env must override stale tenant
    Key Vault refs, otherwise local testing can keep using an old exhausted
    Sarvam key after backend restart."""

    async def fail_if_called(*args, **kwargs):
        raise AssertionError("tenant secret ref should not be read when SARVAM_API_KEY is set")

    monkeypatch.setattr(settings, "SARVAM_API_KEY", "env-sarvam-key")
    monkeypatch.setattr(
        "app.services.sarvam_voice_service.AzureKeyVaultService.get_secret_value",
        fail_if_called,
    )

    tenant_res = SimpleNamespace(
        provider_status={
            "stt_api_key_secret_ref": "old-stt-secret",
            "tts_api_key_secret_ref": "old-tts-secret",
        }
    )

    assert _run(SarvamVoiceService.api_key(tenant_res, "stt")) == "env-sarvam-key"


class _ControlledResponse:
    """Tiny stand-in for ``httpx.Response`` that the Sarvam helper reads."""

    def __init__(self, status_code: int, headers: dict[str, str] | None = None, body: str = ""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = body

    def json(self) -> dict[str, Any]:
        try:
            return json.loads(self.text)
        except Exception:
            return {}


class _BurstClient:
    """Pretends to be ``httpx.AsyncClient`` for the duration of one test.

    Returns the configured sequence of responses, recording each call so
    the test can assert on retry counts."""

    def __init__(self, responses: list[_ControlledResponse]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    async def post(self, url: str, *, headers=None, data=None, files=None, json=None, timeout=None):  # noqa: A002
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers or {}),
                "data": dict(data or {}),
                "files_keys": sorted((files or {}).keys()),
                "json": dict(json or {}) if json else None,
            }
        )
        if self._responses:
            return self._responses.pop(0)
        return _ControlledResponse(500, body="exhausted")


def test_sarvam_stt_retries_until_success(monkeypatch):
    """Three consecutive 429s followed by a clean 200 — the helper
    should ride out the throttle and return the success body."""

    responses = [
        _ControlledResponse(429, headers={"retry-after": "0"}, body="rate limited"),
        _ControlledResponse(429, headers={"retry-after": "0"}, body="rate limited"),
        _ControlledResponse(429, headers={"retry-after": "0"}, body="rate limited"),
        _ControlledResponse(200, body=json.dumps({"transcript": "hello world", "language_code": "en-IN"})),
    ]
    client = _BurstClient(responses)

    async def _no_sleep(_):  # zero-out the backoff so the test is fast
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(SarvamVoiceService, "http_client", classmethod(lambda cls: client))
    monkeypatch.setattr(SarvamVoiceService, "api_key", staticmethod(lambda *a, **kw: _ready("test-key")))

    tenant_res = SimpleNamespace(provider_status={})
    result = _run(
        SarvamVoiceService.transcribe_rest(
            tenant_res,
            b"audio-bytes",
            filename="utt.wav",
            content_type="audio/wav",
        )
    )
    assert result["transcript"] == "hello world"
    # Four calls: three retries + one success.
    assert len(client.calls) == 4


def test_sarvam_stt_gives_up_after_max_attempts(monkeypatch):
    """Six straight 429s — the helper should bail rather than spin
    forever, and the caller should see a RuntimeError."""

    responses = [
        _ControlledResponse(429, headers={"retry-after": "0"}, body=f"limited {i}")
        for i in range(8)
    ]
    client = _BurstClient(responses)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(SarvamVoiceService, "http_client", classmethod(lambda cls: client))
    monkeypatch.setattr(SarvamVoiceService, "api_key", staticmethod(lambda *a, **kw: _ready("test-key")))

    tenant_res = SimpleNamespace(provider_status={})
    with pytest.raises(RuntimeError) as exc:
        _run(
            SarvamVoiceService.transcribe_rest(
                tenant_res,
                b"audio-bytes",
                filename="utt.wav",
                content_type="audio/wav",
            )
        )
    assert "429" in str(exc.value)
    # Helper has max_attempts=6 by default — capped, not infinite.
    assert len(client.calls) == 6


# ── Sarvam TTS prosody fallback ─────────────────────────────────────────────


def test_sarvam_tts_retries_without_prosody_on_400(monkeypatch):
    """When the server rejects pace/pitch/loudness params with a 400,
    the second attempt must strip them and succeed."""

    responses = [
        _ControlledResponse(400, body="unknown field 'pace'"),
        _ControlledResponse(200, body=json.dumps({"audios": ["AAA"], "request_id": "r-1"})),
    ]
    client = _BurstClient(responses)
    monkeypatch.setattr(SarvamVoiceService, "http_client", classmethod(lambda cls: client))
    monkeypatch.setattr(SarvamVoiceService, "api_key", staticmethod(lambda *a, **kw: _ready("test-key")))

    tenant_res = SimpleNamespace(provider_status={"sarvam_tts_model": "bulbul:v3"})
    result = _run(
        SarvamVoiceService.synthesize(
            tenant_res,
            "Hello there",
            language="en",
            pace=1.2,
            pitch=0.1,
            loudness=1.0,
        )
    )
    assert result["audios"] == ["AAA"]
    assert len(client.calls) == 2
    # First call had prosody body fields, second one did not.
    assert "pace" in client.calls[0]["json"]
    assert "pace" not in client.calls[1]["json"]


# ── Azure OpenAI 429 stream ─────────────────────────────────────────────────


class _FakeStreamResponse:
    """Async context manager mimicking ``httpx.AsyncClient.stream``."""

    def __init__(self, status_code: int, *, retry_after: str = "", body: str = "", events: list[str] | None = None):
        self.status_code = status_code
        self.headers = {"retry-after": retry_after} if retry_after else {}
        self._body = body
        self._events = events or []

    async def aread(self):
        return self._body.encode()

    async def aiter_lines(self):
        for line in self._events:
            yield line

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeAzureClient:
    def __init__(self, responses: list[_FakeStreamResponse]):
        self._responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def stream(self, method, url, *, headers=None, json=None):  # noqa: A002
        self.calls.append({"method": method, "url": url, "json": json})
        if not self._responses:
            return _FakeStreamResponse(500, body="exhausted")
        return self._responses.pop(0)


def test_azure_llm_stream_eventually_succeeds_after_429s(monkeypatch):
    """Two 429s, then a clean SSE stream — the helper should yield
    every delta from the successful pass."""

    deltas = [
        f"data: {json.dumps({'choices': [{'delta': {'content': 'Hello '}}]})}",
        f"data: {json.dumps({'choices': [{'delta': {'content': 'there.'}}]})}",
        "data: [DONE]",
    ]
    responses = [
        _FakeStreamResponse(429, retry_after="0", body="limited"),
        _FakeStreamResponse(429, retry_after="0", body="limited"),
        _FakeStreamResponse(200, events=deltas),
    ]
    client = _FakeAzureClient(responses)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(AzureGroundedLLM, "http", classmethod(lambda cls: client))
    monkeypatch.setattr(AzureGroundedLLM, "api_key", staticmethod(lambda *a, **kw: _ready("test-key")))

    tenant_res = SimpleNamespace(
        provider_status={
            "llm_endpoint": "https://test.openai.azure.com",
            "llm_deployment": "gpt-test",
        }
    )

    async def _consume():
        tokens: list[str] = []
        async for token in AzureGroundedLLM.stream(
            tenant_res,
            [{"role": "user", "content": "hi"}],
            max_tokens=64,
        ):
            tokens.append(token)
        return tokens

    tokens = _run(_consume())
    assert tokens == ["Hello ", "there."]
    assert len(client.calls) == 3


def test_azure_llm_stream_raises_after_max_429s(monkeypatch):
    """Four straight 429s — caller must see the dedicated
    NokvoOneAgentRateLimited exception so the pipeline can fall back to
    the 'busy, try again' reply instead of a generic refusal."""

    responses = [
        _FakeStreamResponse(429, retry_after="0", body=f"limited {i}") for i in range(6)
    ]
    client = _FakeAzureClient(responses)

    async def _no_sleep(_):
        return None

    monkeypatch.setattr(asyncio, "sleep", _no_sleep)
    monkeypatch.setattr(AzureGroundedLLM, "http", classmethod(lambda cls: client))
    monkeypatch.setattr(AzureGroundedLLM, "api_key", staticmethod(lambda *a, **kw: _ready("test-key")))

    tenant_res = SimpleNamespace(
        provider_status={
            "llm_endpoint": "https://test.openai.azure.com",
            "llm_deployment": "gpt-test",
        }
    )

    async def _consume():
        async for _ in AzureGroundedLLM.stream(
            tenant_res,
            [{"role": "user", "content": "hi"}],
            max_tokens=64,
        ):
            pass

    with pytest.raises(NokvoOneAgentRateLimited):
        _run(_consume())
    # The implementation tries up to 4 times by default.
    assert len(client.calls) == 4


# ── Helpers ─────────────────────────────────────────────────────────────────


async def _ready(value):
    return value
