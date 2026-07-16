"""Per-deployment chat-parameter profiles for the LLM pool.

The gpt-5/o reasoning family 400-rejects `max_tokens` and non-default
`temperature`, and returns EMPTY content without `reasoning_effort`; classic
deployments (gpt-4.1-nano summarizer) accept temperature and reject
`reasoning_effort`. Profiles are seeded from the deployment name and corrected
at runtime from Azure's unsupported-parameter 400s.
"""
from __future__ import annotations

import asyncio
import json

import pytest

import app.services.llm_pool as lp
from app.services.llm_pool import (
    LLMPool,
    LLMPoolClient,
    PoolMember,
    adapt_profile_from_error,
    build_chat_body,
    param_profile,
)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture(autouse=True)
def _fresh_profiles():
    lp._PARAM_PROFILES.clear()
    yield
    lp._PARAM_PROFILES.clear()


def _member(deployment="gpt-5-mini", key_id="m1"):
    return PoolMember(key_id=key_id, endpoint="https://x.example.com", api_key="k", deployment=deployment, tpm=1000)


_MSGS = [{"role": "user", "content": "hi"}]


def test_reasoning_deployment_body():
    body = build_chat_body(_member("gpt-5-mini"), _MSGS, max_tokens=120, temperature=0.0)
    assert "max_tokens" not in body
    assert body["max_completion_tokens"] == 512  # floored so reasoning can't starve the reply
    assert "temperature" not in body
    assert body["reasoning_effort"]  # minimal by default


def test_classic_deployment_body_keeps_temperature():
    body = build_chat_body(_member("Summarizer-1", key_id="nano1"), _MSGS, max_tokens=120, temperature=0.2)
    assert body["max_completion_tokens"] == 120
    assert body["temperature"] == 0.2
    assert "reasoning_effort" not in body


class _FakeResp:
    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


def test_adapt_profile_learns_from_400s():
    # A reasoning deployment hiding behind an arbitrary name: seeded classic,
    # corrected by the temperature rejection.
    m = _member("mystery-deploy", key_id="myst")
    prof = param_profile(m)
    assert prof["supports_temperature"] and not prof["reasoning_effort"]
    assert adapt_profile_from_error(m, _FakeResp({"error": {"param": "temperature", "message": "unsupported"}}))
    prof = param_profile(m)
    assert not prof["supports_temperature"] and prof["reasoning_effort"]
    # A classic deployment that rejects reasoning_effort flips it off.
    assert adapt_profile_from_error(m, _FakeResp({"error": {"param": "reasoning_effort", "message": "unsupported"}}))
    assert not param_profile(m)["reasoning_effort"]
    # Unrelated 400s change nothing.
    assert not adapt_profile_from_error(m, _FakeResp({"error": {"param": "messages", "message": "bad"}}))


def test_chat_negotiates_params_and_caches_profile(monkeypatch):
    member = _member("mystery-deploy", key_id="neg1")
    monkeypatch.setattr(LLMPool, "members", classmethod(lambda cls, pool="mini": [member]))

    async def fake_reserve(estimate, **kwargs):
        return member

    async def fake_reconcile(m, est, actual, **kwargs):
        return None

    monkeypatch.setattr(LLMPool, "reserve", staticmethod(fake_reserve))
    monkeypatch.setattr(LLMPool, "reconcile", staticmethod(fake_reconcile))

    sent_bodies = []

    class _HttpResp:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

        def raise_for_status(self):
            if self.status_code >= 400:
                import httpx

                raise httpx.HTTPStatusError("boom", request=None, response=self)  # type: ignore[arg-type]

    class _FakeHttp:
        async def post(self, url, headers=None, json=None):
            sent_bodies.append(json)
            if "temperature" in json:
                return _HttpResp(400, {"error": {"param": "temperature", "message": "unsupported"}})
            return _HttpResp(200, {"choices": [{"message": {"content": "PONG"}}], "usage": {"total_tokens": 10}})

    monkeypatch.setattr(LLMPoolClient, "http", classmethod(lambda cls: _FakeHttp()))

    out = _run(LLMPoolClient.chat(_MSGS, max_tokens=50, temperature=0.0))
    assert out == "PONG"
    # First body carried temperature (seeded classic for the unknown name);
    # the 400 flipped the profile and the retry succeeded without it.
    assert "temperature" in sent_bodies[0]
    assert "temperature" not in sent_bodies[1] and sent_bodies[1]["reasoning_effort"]

    # The learned profile is cached: the next call adapts on the FIRST request.
    sent_bodies.clear()
    out2 = _run(LLMPoolClient.chat(_MSGS, max_tokens=50, temperature=0.0))
    assert out2 == "PONG"
    assert len(sent_bodies) == 1 and "temperature" not in sent_bodies[0]
