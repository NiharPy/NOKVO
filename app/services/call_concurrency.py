"""Per-tenant concurrent-call caps, split into TWO independent pools.

  * ``outbound`` — campaign calls. Cap:
    ``settings.NOKVO_MAX_CONCURRENT_OUTBOUND_PER_TENANT`` (default 5).
  * ``inbound_followup`` — inbound calls AND follow-up calls, sharing one cap:
    ``settings.NOKVO_MAX_CONCURRENT_INBOUND_FOLLOWUP_PER_TENANT`` (default 5).

Follow-ups deliberately draw from the inbound pool (not the campaign pool), so
when a campaign's follow-up agent is turned off, no follow-up calls are placed
and the full inbound pool is available to inbound callers. Each pool is its own
Redis sorted set, so they never starve each other.

Live calls are tracked in a Redis **sorted set** per tenant — member = a unique
call token, score = the time the slot was reserved. Using a sorted set (rather
than a bare INCR/DECR counter) makes the count *self-healing*: if a replica is
hard-killed mid-call and never releases its tokens, those entries are pruned the
next time anyone reads the set, once they age past ``_MAX_CALL_SECONDS``. A bare
counter would leak that slot forever and eventually wedge the tenant at a false
"busy".

Usage:
  - Inbound: the Plivo answer webhook calls :func:`acquire` *before* bridging
    audio. ``None`` → play a busy message and never open a media stream (so the
    caller never hits dead air). A token → pass it to the media WebSocket via the
    URL; the WS releases it in its ``finally``.
  - Outbound: the media-WS endpoint calls :func:`acquire` after resolving the
    tenant and releases in ``finally``; the follow-up scheduler uses
    :func:`active_count` to avoid placing a call it can't service.

All operations **fail open** (admit the call) when Redis is unreachable — a
cache blip must never drop a paying customer's call; the worst case is briefly
exceeding the cap.
"""
from __future__ import annotations

import logging
import time
import uuid

from app.core.config import settings
from app.services.agent_session_store import AgentSessionStore

logger = logging.getLogger(__name__)

# A reserved slot older than this is assumed dead (replica killed without
# cleanup) and pruned. Comfortably longer than any real call.
_MAX_CALL_SECONDS = 60 * 60

# Atomically prune stale tokens, then admit iff under the cap.
#   KEYS[1]=zset
#   ARGV[1]=limit  ARGV[2]=now  ARGV[3]=max_age  ARGV[4]=token  ARGV[5]=ttl
_ACQUIRE_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', tonumber(ARGV[2]) - tonumber(ARGV[3]))
local n = redis.call('ZCARD', KEYS[1])
if n >= tonumber(ARGV[1]) then
  return 0
end
redis.call('ZADD', KEYS[1], ARGV[2], ARGV[4])
redis.call('EXPIRE', KEYS[1], tonumber(ARGV[5]))
return 1
"""

# Prune stale tokens, then return the live count.
#   KEYS[1]=zset  ARGV[1]=now  ARGV[2]=max_age
_COUNT_LUA = """
redis.call('ZREMRANGEBYSCORE', KEYS[1], '-inf', tonumber(ARGV[1]) - tonumber(ARGV[2]))
return redis.call('ZCARD', KEYS[1])
"""


# Concurrency pools. Outbound (campaign) calls and inbound+follow-up calls each
# get their own budget and their own Redis key.
POOL_OUTBOUND = "outbound"
POOL_INBOUND_FOLLOWUP = "inbound_followup"


def _key(tenant_id, pool: str) -> str:
    return f"nokvo:tenant:{tenant_id}:active_calls:{pool}"


def _limit(pool: str) -> int:
    try:
        if pool == POOL_OUTBOUND:
            return max(1, int(settings.NOKVO_MAX_CONCURRENT_OUTBOUND_PER_TENANT))
        return max(1, int(settings.NOKVO_MAX_CONCURRENT_INBOUND_FOLLOWUP_PER_TENANT))
    except Exception:
        return 5


async def acquire(tenant_id, *, pool: str = POOL_INBOUND_FOLLOWUP, limit: int | None = None) -> str | None:
    """Reserve a concurrency slot for ``tenant_id`` in ``pool``.

    ``limit`` is the per-tenant cap for this pool (e.g. an APEX org's plan
    concurrency). When ``None`` the global ``_limit(pool)`` setting is used — so
    non-APEX callers keep the platform default and only APEX outbound passes a
    per-plan value.

    Returns an opaque token to be passed to :func:`release` (with the SAME
    ``pool``), or ``None`` if that pool is already at its cap. Fails open
    (returns a token) on any Redis error so a cache blip never blocks calls.
    """
    eff_limit = _limit(pool) if limit is None else max(1, int(limit))
    token = uuid.uuid4().hex
    now = time.time()
    try:
        ok = await AgentSessionStore.client().eval(
            _ACQUIRE_LUA,
            1,
            _key(tenant_id, pool),
            eff_limit,
            now,
            _MAX_CALL_SECONDS,
            token,
            _MAX_CALL_SECONDS + 300,
        )
        return token if int(ok) == 1 else None
    except Exception:
        logger.warning("call_concurrency.acquire failed open for tenant=%s pool=%s", tenant_id, pool, exc_info=True)
        return token


async def release(tenant_id, token: str | None, *, pool: str = POOL_INBOUND_FOLLOWUP) -> None:
    """Release a slot previously reserved by :func:`acquire` in the same
    ``pool``. No-op if ``token`` is falsy or Redis is unreachable (the entry
    self-prunes by age)."""
    if not token:
        return
    try:
        await AgentSessionStore.client().zrem(_key(tenant_id, pool), token)
    except Exception:
        logger.debug("call_concurrency.release failed for tenant=%s pool=%s", tenant_id, pool, exc_info=True)


async def active_count(tenant_id, *, pool: str = POOL_INBOUND_FOLLOWUP) -> int:
    """Live call count for ``tenant_id`` in ``pool`` (stale tokens pruned).
    Returns 0 on Redis error so callers fail open."""
    try:
        n = await AgentSessionStore.client().eval(
            _COUNT_LUA,
            1,
            _key(tenant_id, pool),
            time.time(),
            _MAX_CALL_SECONDS,
        )
        return int(n)
    except Exception:
        return 0
