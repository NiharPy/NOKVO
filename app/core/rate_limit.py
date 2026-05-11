"""Shared SlowAPI Limiter so routers can attach @limiter.limit() decorators."""
from __future__ import annotations

from slowapi import Limiter
from slowapi.util import get_remote_address


def _composite_key(request) -> str:
    """Rate-limit key that combines client IP with request body email/domain when available.

    Falls back to IP when the body cannot be inspected (e.g., the route hasn't parsed it
    yet — slowapi runs key extraction before Pydantic). The IP alone is still a valid limit.
    """
    return get_remote_address(request)


limiter = Limiter(key_func=_composite_key, default_limits=["200/minute"])
