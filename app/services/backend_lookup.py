"""Backend Lookup capability (P5) — fetch live data from a business's own systems
and speak it back, SAFELY. See NOKVOSDK/docs/05.

This module is the security core + orchestration. It is inert until a tenant has a
configured endpoint (``retrieval.py`` stops deflecting only when one exists), and
it is **read-only** — any write (cancel/refund) goes through the existing
confirm→ticket/escalation path, never a silent external mutation.

Everything here is pure/testable except the actual HTTP call, which is injected as
a ``fetch`` callable so the SSRF guard, templating, mapping and PII redaction can
be verified without network.
"""
from __future__ import annotations

import ipaddress
import re
import socket
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable
from urllib.parse import urlparse


@dataclass
class BackendLookupSpec:
    key: str                                    # → tool ``lookup_<key>``
    url: str
    method: str = "GET"
    auth_secret_ref: str | None = None          # e.g. "kv://tenant/<id>/shopify_token"
    auth_header: str = "Authorization"
    auth_scheme: str = "Bearer"
    request_template: dict[str, str] = field(default_factory=dict)   # param → "{{ slots.x }}"
    response_map: dict[str, str] = field(default_factory=dict)       # spoken field → dotted path
    identity_gate: list[str] = field(default_factory=list)          # slots that MUST be verified first
    speak_template: str = ""
    pii_fields: list[str] = field(default_factory=list)             # never spoken/logged
    cache_ttl_s: int = 60


# ── SSRF guard ───────────────────────────────────────────────────────────────

def _ip_blocked(ip: ipaddress._BaseAddress) -> bool:
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def is_safe_lookup_url(url: str, *, allowed_hosts: list[str] | None = None) -> bool:
    """https-only; host must resolve to PUBLIC IP(s) — no private / loopback /
    link-local / cloud-metadata — and, if an allowlist is given, be on it."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme != "https" or not p.hostname:
        return False
    host = p.hostname
    if host in ("metadata.google.internal",):
        return False
    if allowed_hosts is not None and host.lower() not in {h.lower() for h in allowed_hosts}:
        return False
    # IP literal → check directly (no DNS); hostname → resolve every A/AAAA.
    try:
        ips = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            ips = [ipaddress.ip_address(info[4][0]) for info in socket.getaddrinfo(host, p.port or 443)]
        except Exception:
            return False
    return bool(ips) and all(not _ip_blocked(ip) for ip in ips)


# ── templating / mapping / redaction (pure) ──────────────────────────────────

_SLOT_RE = re.compile(r"\{\{\s*slots\.([a-zA-Z0-9_]+)\s*\}\}")


def render_template(text: str, slots: dict[str, Any]) -> str:
    return _SLOT_RE.sub(lambda m: str((slots or {}).get(m.group(1), "")), text or "")


def render_request(template: dict[str, str], slots: dict[str, Any]) -> dict[str, str]:
    return {k: render_template(v, slots) for k, v in (template or {}).items()}


def _get_path(obj: Any, path: str) -> Any:
    cur = obj
    for part in path.split("."):
        if not part:
            continue
        name, _, rest = part.partition("[")
        if name:
            cur = cur.get(name) if isinstance(cur, dict) else None
        while rest:
            idx, _, rest = rest.partition("]")
            rest = rest.lstrip("[")
            try:
                cur = cur[int(idx)]
            except Exception:
                return None
        if cur is None:
            return None
    return cur


def map_response(obj: Any, response_map: dict[str, str]) -> dict[str, Any]:
    return {field_name: _get_path(obj, path) for field_name, path in (response_map or {}).items()}


def redact(fields: dict[str, Any], pii_fields: list[str]) -> dict[str, Any]:
    blocked = set(pii_fields or [])
    return {k: v for k, v in (fields or {}).items() if k not in blocked}


# ── orchestration ────────────────────────────────────────────────────────────

FetchFn = Callable[..., Awaitable[Any]]  # (url, method, headers, params) -> parsed JSON


async def perform_lookup(
    spec: BackendLookupSpec,
    slots: dict[str, Any],
    *,
    identity_verified: bool,
    fetch: FetchFn,
    secret_resolver: Callable[[str], Awaitable[str | None]] | None = None,
    allowed_hosts: list[str] | None = None,
) -> dict[str, Any]:
    """Run one lookup → ``{"ok": bool, "spoken": str, "fields": {...}}``.

    Guards, in order: identity gate → SSRF → fetch → map → redact PII → speak.
    Never raises; a failure returns ``ok=False`` with a graceful spoken fallback.
    """
    if spec.identity_gate and not identity_verified:
        return {"ok": False, "reason": "identity_unverified",
                "spoken": "I can help, but first I need to verify a couple of details."}

    if not is_safe_lookup_url(spec.url, allowed_hosts=allowed_hosts):
        return {"ok": False, "reason": "unsafe_url", "spoken": "I couldn't reach that system right now."}

    headers: dict[str, str] = {}
    if spec.auth_secret_ref and secret_resolver is not None:
        try:
            token = await secret_resolver(spec.auth_secret_ref)
        except Exception:
            token = None
        if token:
            headers[spec.auth_header] = f"{spec.auth_scheme} {token}".strip()

    params = render_request(spec.request_template, slots)
    try:
        raw = await fetch(spec.url, method=spec.method, headers=headers, params=params)
    except Exception:
        return {"ok": False, "reason": "fetch_failed", "spoken": "I couldn't reach that system right now."}

    fields = redact(map_response(raw, spec.response_map), spec.pii_fields)
    spoken = render_template(spec.speak_template, fields) if spec.speak_template else ""
    return {"ok": True, "fields": fields, "spoken": spoken}
