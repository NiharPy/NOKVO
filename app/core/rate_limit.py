"""Shared SlowAPI Limiter so routers can attach @limiter.limit() decorators."""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address


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


limiter = Limiter(key_func=_composite_key, default_limits=["200/minute"])
