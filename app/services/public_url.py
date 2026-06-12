"""Single source of truth for the app's PUBLIC base URL.

Plivo (and any external webhook caller) must be given URLs that are reachable
from the internet. Three sources have historically disagreed:

  * ``PLIVO_WEBHOOK_BASE_URL`` — the explicit contract; what provisioning
    registers on each tenant's Plivo Application.
  * ``AGENT_PUBLIC_BASE_URL`` — used by the outbound dialer/scheduler.
  * ``request.url`` — what the app saw, which behind a TLS-terminating proxy
    or tunnel is the INTERNAL scheme/host ("http://10.0.0.5:8000"), producing
    ``ws://`` media URLs Plivo can't open → call answers, then dead air.

Every webhook/media URL must come through :func:`public_base_url` so they all
agree, in this precedence:

  1. ``PLIVO_WEBHOOK_BASE_URL`` (explicit, set at deploy time)
  2. ``AGENT_PUBLIC_BASE_URL`` (when set to something other than the
     localhost default)
  3. request-derived, honoring ``X-Forwarded-Proto`` / ``X-Forwarded-Host``
     (URL building only — never trust these for auth decisions).

Pure functions, no FastAPI dependency at import time — unit-testable.
"""
from __future__ import annotations

import logging
from typing import Any

from app.core.config import settings

logger = logging.getLogger(__name__)

_LOCALHOST_DEFAULT = "http://localhost:8000"


def _first_forwarded(value: str | None) -> str:
    """X-Forwarded-* headers may be comma-joined across proxies; the first
    entry is the client-facing one."""
    return (value or "").split(",")[0].strip()


def _request_derived(request: Any) -> str:
    """Best-effort base URL from the live request, preferring forwarded
    headers over what the socket saw."""
    headers = getattr(request, "headers", None) or {}
    proto = _first_forwarded(headers.get("x-forwarded-proto"))
    host = _first_forwarded(headers.get("x-forwarded-host"))
    url = getattr(request, "url", None)
    scheme = proto or (getattr(url, "scheme", None) or "http")
    if not host:
        hostname = getattr(url, "hostname", None) or "localhost"
        port = getattr(url, "port", None)
        host = f"{hostname}:{port}" if port else hostname
    return f"{scheme}://{host}"


def public_base_url(request: Any = None) -> str:
    """The https base URL external services should call us on (no trailing /)."""
    explicit = (settings.PLIVO_WEBHOOK_BASE_URL or "").strip().rstrip("/")
    if explicit:
        return explicit
    agent = (settings.AGENT_PUBLIC_BASE_URL or "").strip().rstrip("/")
    if agent and agent != _LOCALHOST_DEFAULT:
        return agent
    if request is not None:
        return _request_derived(request).rstrip("/")
    return agent or _LOCALHOST_DEFAULT


def ws_base_url(request: Any = None) -> str:
    """Same base with the WebSocket scheme (https→wss, http→ws)."""
    base = public_base_url(request)
    if base.startswith("https://"):
        return "wss://" + base[len("https://"):]
    if base.startswith("http://"):
        return "ws://" + base[len("http://"):]
    return base


def validate_public_base_url() -> list[str]:
    """Deploy-time sanity warnings. Returned (not raised) so startup can log
    loudly without refusing to boot — provisioning already degrades."""
    warnings: list[str] = []
    explicit = (settings.PLIVO_WEBHOOK_BASE_URL or "").strip()
    agent = (settings.AGENT_PUBLIC_BASE_URL or "").strip()
    if not explicit:
        warnings.append(
            "PLIVO_WEBHOOK_BASE_URL is not set — Plivo webhooks/media URLs will "
            "fall back to AGENT_PUBLIC_BASE_URL or the request host, which is "
            "wrong behind a proxy/tunnel. Inbound calls may not connect."
        )
    effective = explicit or agent
    if "localhost" in effective or "127.0.0.1" in effective:
        warnings.append(
            f"Public base URL '{effective}' points at localhost — Plivo cannot "
            "reach it. Set PLIVO_WEBHOOK_BASE_URL to the public https URL."
        )
    elif effective.startswith("http://"):
        warnings.append(
            f"Public base URL '{effective}' is plain http — Plivo media streams "
            "need wss (https). Use an https URL."
        )
    if explicit and agent and agent != _LOCALHOST_DEFAULT:
        if explicit.rstrip("/") != agent.rstrip("/"):
            warnings.append(
                f"PLIVO_WEBHOOK_BASE_URL ({explicit}) and AGENT_PUBLIC_BASE_URL "
                f"({agent}) disagree — inbound and outbound webhooks will use "
                "different hosts. Align them."
            )
    return warnings


__all__ = ("public_base_url", "ws_base_url", "validate_public_base_url")
