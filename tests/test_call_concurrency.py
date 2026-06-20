"""Per-tenant concurrent-call cap (app/services/call_concurrency.py).

Redis-gated: skips cleanly when Redis is unreachable so it never blocks the
suite in a Redis-less environment.
"""
import uuid

import pytest

from app.services import call_concurrency as cc
from app.services.agent_session_store import AgentSessionStore


@pytest.fixture(autouse=True)
def _reset_redis_singleton():
    # pytest-asyncio gives each test a fresh event loop; the redis.asyncio
    # singleton caches connections bound to the loop they were created on, so
    # reset it per test. Production runs a single long-lived loop, so the
    # singleton is correct there.
    AgentSessionStore._client = None
    yield
    AgentSessionStore._client = None


async def _redis_up() -> bool:
    try:
        await AgentSessionStore.client().ping()
        return True
    except Exception:
        return False


@pytest.mark.asyncio
async def test_cap_enforced_and_released(monkeypatch):
    if not await _redis_up():
        pytest.skip("Redis not reachable")

    # Small cap so the test is fast and independent of the configured default.
    # ``acquire`` defaults to the inbound+follow-up pool.
    monkeypatch.setattr(
        cc.settings, "NOKVO_MAX_CONCURRENT_INBOUND_FOLLOWUP_PER_TENANT", 3, raising=False
    )
    tenant = f"test-{uuid.uuid4().hex[:8]}"

    try:
        tokens = [await cc.acquire(tenant) for _ in range(3)]
        assert all(tokens), "first 3 acquires within cap should all succeed"
        assert await cc.active_count(tenant) == 3

        # 4th over the cap is rejected.
        assert await cc.acquire(tenant) is None
        assert await cc.active_count(tenant) == 3

        # Releasing one frees exactly one slot.
        await cc.release(tenant, tokens[0])
        assert await cc.active_count(tenant) == 2
        freed = await cc.acquire(tenant)
        assert freed is not None
        tokens = tokens[1:] + [freed]
    finally:
        for tok in tokens:
            await cc.release(tenant, tok)
        # Key self-cleans; assert we left no slots behind.
        assert await cc.active_count(tenant) == 0


@pytest.mark.asyncio
async def test_pools_are_independent(monkeypatch):
    """Outbound (campaign) and inbound+follow-up are separate budgets — filling
    one must not consume the other."""
    if not await _redis_up():
        pytest.skip("Redis not reachable")

    monkeypatch.setattr(cc.settings, "NOKVO_MAX_CONCURRENT_OUTBOUND_PER_TENANT", 2, raising=False)
    monkeypatch.setattr(cc.settings, "NOKVO_MAX_CONCURRENT_INBOUND_FOLLOWUP_PER_TENANT", 2, raising=False)
    tenant = f"test-{uuid.uuid4().hex[:8]}"
    out, inb = [], []
    try:
        out = [await cc.acquire(tenant, pool=cc.POOL_OUTBOUND) for _ in range(2)]
        assert all(out) and await cc.acquire(tenant, pool=cc.POOL_OUTBOUND) is None
        # inbound pool is untouched by a full outbound pool.
        assert await cc.active_count(tenant, pool=cc.POOL_INBOUND_FOLLOWUP) == 0
        inb = [await cc.acquire(tenant, pool=cc.POOL_INBOUND_FOLLOWUP) for _ in range(2)]
        assert all(inb) and await cc.acquire(tenant, pool=cc.POOL_INBOUND_FOLLOWUP) is None
    finally:
        for tok in out:
            await cc.release(tenant, tok, pool=cc.POOL_OUTBOUND)
        for tok in inb:
            await cc.release(tenant, tok, pool=cc.POOL_INBOUND_FOLLOWUP)
        assert await cc.active_count(tenant, pool=cc.POOL_OUTBOUND) == 0
        assert await cc.active_count(tenant, pool=cc.POOL_INBOUND_FOLLOWUP) == 0


@pytest.mark.asyncio
async def test_release_is_idempotent_and_safe(monkeypatch):
    if not await _redis_up():
        pytest.skip("Redis not reachable")

    tenant = f"test-{uuid.uuid4().hex[:8]}"
    # Releasing an unknown / None token must never go negative or raise.
    await cc.release(tenant, None)
    await cc.release(tenant, "never-acquired")
    assert await cc.active_count(tenant) == 0
