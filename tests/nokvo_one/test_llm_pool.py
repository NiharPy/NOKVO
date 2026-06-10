"""Phase A — the shared LLM pool (cupcake-box budgeter).

No fakeredis in the env, so a tiny in-memory async fake ports the reserve-Lua
logic; the tests exercise the real reserve/reconcile/cooldown/chat orchestration.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from app.core.config import settings
from app.services.llm_pool import LLMPool, LLMPoolClient, LLMPoolError


def _run(coro):
    return asyncio.run(coro)


class _FakeRedis:
    """Implements just what LLMPool uses: eval(reserve), get, set, incrby, expire."""

    def __init__(self):
        self.store: dict[str, int] = {}

    async def eval(self, script, numkeys, *args):
        keys = list(args[:numkeys])
        argv = list(args[numkeys:])
        est = int(argv[0]); start = int(argv[2]); caps = [int(x) for x in argv[3:]]
        n = len(keys)
        for off in range(n):
            i = (start + off) % n
            cur = self.store.get(keys[i])
            remaining = caps[i] if cur is None else int(cur)
            if remaining >= est:
                self.store[keys[i]] = remaining - est
                return i
        return -1

    async def get(self, key):
        v = self.store.get(key)
        return None if v is None else str(v)

    async def set(self, key, val, ex=None):
        self.store[key] = int(val)

    async def incrby(self, key, delta):
        self.store[key] = int(self.store.get(key, 0)) + int(delta)
        return self.store[key]

    async def expire(self, key, secs):
        return True


@pytest.fixture
def pool(monkeypatch):
    monkeypatch.setattr(settings, "AZURE_OPENAI_POOL_JSON", json.dumps([
        {"key_id": "a", "endpoint": "https://a.example", "api_key": "ka", "deployment": "gpt-5-mini", "tpm": 100},
        {"key_id": "b", "endpoint": "https://b.example", "api_key": "kb", "deployment": "gpt-5-mini", "tpm": 100},
    ]))
    # The global account is always appended as a pool box; clear it so these
    # tests isolate exactly the two JSON members.
    monkeypatch.setattr(settings, "AZURE_OPENAI_GLOBAL_ENDPOINT", "")
    monkeypatch.setattr(settings, "AZURE_OPENAI_GLOBAL_API_KEY", "")
    LLMPool.reset_cache()
    LLMPool._redis = _FakeRedis()
    yield LLMPool._redis
    LLMPool._redis = None
    LLMPool.reset_cache()


def test_reserve_decrements_skips_and_empties(pool):
    async def go():
        m1 = await LLMPool.reserve(60)
        m2 = await LLMPool.reserve(60)
        assert m1 is not None and m2 is not None
        assert {m1.key_id, m2.key_id} == {"a", "b"}     # spread across both boxes
        assert await LLMPool.reserve(60) is None         # both at 40 < 60
    _run(go())


def test_reconcile_refunds_unused_budget(pool):
    async def go():
        m = await LLMPool.reserve(80)        # one box → 20 left
        await LLMPool.reconcile(m, 80, 10)   # used 10 → refund 70 → 90 left
        assert await LLMPool.reserve(80) is not None
    _run(go())


def test_cooldown_zeroes_a_box(pool):
    async def go():
        members = LLMPool.members()
        await LLMPool.cooldown(members[0])
        m = await LLMPool.reserve(90)         # only the other box (100) can serve
        assert m.key_id != members[0].key_id
    _run(go())


def test_no_members_raises(monkeypatch):
    monkeypatch.setattr(settings, "AZURE_OPENAI_POOL_JSON", "")
    monkeypatch.setattr(settings, "AZURE_OPENAI_GLOBAL_ENDPOINT", "")
    monkeypatch.setattr(settings, "AZURE_OPENAI_GLOBAL_API_KEY", "")
    LLMPool.reset_cache()
    try:
        with pytest.raises(LLMPoolError):
            _run(LLMPool.reserve(10))
    finally:
        LLMPool.reset_cache()


# ── chat orchestration ──────────────────────────────────────────────────────


class _FakeResp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        import httpx
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("err", request=None, response=self)


class _FakeHTTP:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    async def post(self, url, headers=None, json=None):
        self.calls += 1
        return self._responses.pop(0)


def test_chat_returns_text_and_reconciles(pool, monkeypatch):
    payload = {"choices": [{"message": {"content": "  hi there  "}}], "usage": {"total_tokens": 5}}
    monkeypatch.setattr(LLMPoolClient, "http", classmethod(lambda cls: _FakeHTTP([_FakeResp(200, payload)])))
    out = _run(LLMPoolClient.chat([{"role": "user", "content": "hello"}], max_tokens=20))
    assert out == "hi there"


def test_chat_rotates_on_429(pool, monkeypatch):
    ok = {"choices": [{"message": {"content": "second key"}}], "usage": {"total_tokens": 5}}
    http = _FakeHTTP([_FakeResp(429, {}), _FakeResp(200, ok)])
    monkeypatch.setattr(LLMPoolClient, "http", classmethod(lambda cls: http))
    out = _run(LLMPoolClient.chat([{"role": "user", "content": "hello"}], max_tokens=20))
    assert out == "second key"
    assert http.calls == 2  # first 429 → cooldown + retry on the next box


# ── Phase B: the voice pipeline resolves its LLM from the pool ───────────────


def test_sticky_same_call_same_box(pool):
    from app.services.llm_pool import set_call_id

    async def go():
        set_call_id("call-XYZ")
        try:
            boxes = {(await LLMPool.reserve(10)).key_id for _ in range(5)}
            assert len(boxes) == 1  # all turns of one call → the same home box
        finally:
            set_call_id(None)
    _run(go())


def test_sticky_fails_over_when_home_capped(pool):
    from app.services.llm_pool import set_call_id, _sticky_start

    async def go():
        set_call_id("call-XYZ")
        try:
            members = LLMPool.members()
            home = members[_sticky_start(len(members), "call-XYZ")]
            await LLMPool.cooldown(home)             # drain the home box to 0
            m = await LLMPool.reserve(50)
            assert m is not None and m.key_id != home.key_id   # failed over to the other box
        finally:
            set_call_id(None)
    _run(go())


def test_nano_pool_has_separate_budget(monkeypatch):
    monkeypatch.setattr(settings, "AZURE_OPENAI_NANO_POOL_JSON", json.dumps([
        {"key_id": "n1", "endpoint": "https://n1.example", "api_key": "kn", "deployment": "gpt-4-1-nano", "tpm": 50},
    ]))
    monkeypatch.setattr(settings, "AZURE_OPENAI_NANO_ENDPOINT", "")  # no single-nano fallback box
    LLMPool.reset_cache()
    fake = _FakeRedis()
    LLMPool._redis = fake
    try:
        async def go():
            assert [m.key_id for m in LLMPool.members("nano")] == ["n1"]
            m = await LLMPool.reserve(20, pool="nano", sticky=False)
            assert m.key_id == "n1"
            # budget key is namespaced to the nano pool, not mini.
            assert any(":nano:" in k for k in fake.store)
            assert not any(":mini:" in k for k in fake.store)
        _run(go())
    finally:
        LLMPool._redis = None
        LLMPool.reset_cache()


def test_pipeline_resolves_llm_from_pool(pool):
    from app.services.nokvo_one_voice_pipeline import AzureGroundedLLM

    async def go():
        member, est = await AzureGroundedLLM._acquire_member(
            [{"role": "user", "content": "hello there"}], 120
        )
        assert member is not None and member.key_id in {"a", "b"}
        assert est > 0
        url, body = AzureGroundedLLM._member_request(member, [{"role": "user", "content": "hi"}])
        # URL is built from the POOL member's endpoint/deployment, not per-tenant.
        assert member.endpoint in url and "/chat/completions?api-version=" in url
        assert body["messages"] and "max_tokens" in body
    _run(go())
