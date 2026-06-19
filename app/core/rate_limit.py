"""Shared SlowAPI Limiter so routers can attach @limiter.limit() decorators."""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address

from app.core.config import settings


def _composite_key(request) -> str:
    """Rate-limit key.

    SlowAPI's key extractor runs before Pydantic parses the request body, so
    we can't reach into JSON to compose IP + email. Falls back to the client
    IP, which is sufficient for the documented brute-force-protection use
    cases (login, OTP verify, password reset) where the attacker controls
    only one side of the bucket. Per-email rate-limiting should be done in
    the route body, not here.
    """
    return get_remote_address(request)


# Redis-backed storage so brute-force buckets (login / OTP / password-reset)
# are shared across all Container Apps replicas. In-memory storage would give
# each replica its own bucket, multiplying the effective limit by the replica
# count. Redis is already a hard dependency (sessions, LLM budget, scheduler
# locks), so this adds no new infrastructure.
#
# Resilience is critical here: SlowAPIMiddleware applies the default limit to
# EVERY request, so if the Redis store is unreachable the check would raise and
# slowapi's middleware turns that into a 500 on every request — including
# /health/live, which would fail the liveness probe and crash-loop the pod.
#   - in_memory_fallback_enabled: on a storage error slowapi flips to an
#     in-memory limiter (degraded to per-replica) instead of erroring, so the
#     API keeps serving and limits still apply during a Redis blip.
#   - swallow_errors: belt-and-suspenders — allow the request if even the
#     fallback path errors.
# When Redis is healthy, buckets are shared across replicas (the point).
limiter = Limiter(
    key_func=_composite_key,
    default_limits=["200/minute"],
    storage_uri=settings.REDIS_URL,
    in_memory_fallback_enabled=True,
    swallow_errors=True,
)
