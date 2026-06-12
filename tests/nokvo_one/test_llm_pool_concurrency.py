"""Concurrency test for the LLM-pool reservation Lua under real contention.

The whole point of ``_RESERVE_LUA`` is that selection+decrement is **atomic** on
the Redis server, so N concurrent workers can never oversubscribe a box. The
in-repo ``_FakeRedis`` ports the Lua into single-threaded Python, so it can't
actually exercise contention — only a real Redis, where the script runs
atomically server-side, proves the invariant. Hence this test connects to
``settings.REDIS_URL`` and **skips cleanly** (never fails) when Redis is absent,
so CI without Redis stays green.

Invariant under test: fire 30 concurrent ``reserve(est=100)`` against a single
box with ``tpm=1000``. Exactly ``1000 // 100 = 10`` must succeed; the other 20
must get ``None``; and the box's remaining budget read back must be **exactly
0**. ``== 0`` is the correct assertion — a negative value would mean the Lua
over-reserved (the bug we're guarding against) and a positive value would mean
it under-counted; ``>= 0`` would mask the over-reservation case.
"""
from __future__ import annotations

import asyncio

import pytest

from app.services import llm_pool as llm_pool_module
from app.services.llm_pool import LLMPool, PoolMember

# A far-future, fixed wall-clock so every reserve lands in the SAME 60s window
# (no minute-boundary straddle) and the window key is deterministic for cleanup.
_FIXED_NOW = 4_102_444_800.0  # 2100-01-01T00:00:00Z
_KEY_ID = "concurrency-test-box"
_POOL = "mini"
_TPM = 1000
_EST = 100
_CONCURRENCY = 30
_EXPECTED_SUCCESSES = _TPM // _EST  # 10


_SKIP = object()  # sentinel returned when Redis is unreachable


async def _run_atomicity_check():
    """Everything runs in ONE event loop: redis-py asyncio connections bind to
    the running loop, so a client pinged in one ``asyncio.run`` can't be reused
    in another. We build the client fresh here and drive ping + reserves on it.
    Returns ``_SKIP`` when Redis is unavailable."""
    LLMPool._redis = None  # force a fresh client bound to THIS loop
    client = LLMPool.client()
    try:
        await client.ping()
    except Exception:
        return _SKIP

    window_key = LLMPool._window_key(_KEY_ID, pool=_POOL, now=_FIXED_NOW)
    await client.delete(window_key)  # start from a clean window
    try:
        results = await asyncio.gather(
            *(LLMPool.reserve(_EST, pool=_POOL, sticky=False) for _ in range(_CONCURRENCY))
        )
        successes = [r for r in results if r is not None]
        failures = [r for r in results if r is None]
        remaining = int(await client.get(window_key))
        return successes, failures, remaining
    finally:
        await client.delete(window_key)
        await client.aclose()
        LLMPool._redis = None


def test_reserve_lua_is_atomic_under_concurrency(monkeypatch):
    # Pin time so all reserves share one 60s window → deterministic window key.
    monkeypatch.setattr(llm_pool_module.time, "time", lambda: _FIXED_NOW)

    # Inject a single isolated box (tpm=1000) straight into the member cache so
    # the test is independent of whatever pool JSON the env happens to carry.
    box = PoolMember(
        key_id=_KEY_ID,
        endpoint="https://test.invalid/openai/deployments/x/chat/completions",
        api_key="test-key",
        deployment="x",
        tpm=_TPM,
    )
    saved_cache = LLMPool._members_cache
    LLMPool._members_cache = {_POOL: [box]}
    try:
        outcome = asyncio.run(_run_atomicity_check())
    finally:
        LLMPool._members_cache = saved_cache
        LLMPool.reset_cache()

    if outcome is _SKIP:
        pytest.skip("Real Redis not reachable at REDIS_URL — skipping atomicity test")

    successes, failures, remaining = outcome

    # Exactly tpm//est reservations win; the rest are turned away. This is the
    # atomicity guarantee — never more than 10 even under 30-way contention.
    assert len(successes) == _EXPECTED_SUCCESSES, (
        f"expected exactly {_EXPECTED_SUCCESSES} successful reserves, got {len(successes)}"
    )
    assert len(failures) == _CONCURRENCY - _EXPECTED_SUCCESSES
    assert all(r.key_id == _KEY_ID for r in successes)

    # 10 × 100 fully consumes tpm=1000 → remaining must be EXACTLY 0.
    assert remaining == 0, (
        f"remaining budget must be exactly 0 after full consumption; got {remaining} "
        f"({'over-reserved (Lua bug)' if remaining < 0 else 'under-counted'})"
    )
