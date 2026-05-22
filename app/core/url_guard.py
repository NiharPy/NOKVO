"""URL/SSRF guard for outbound integration HTTP calls.

Tenant admins configure CRM/ERP/shipping base URLs that we then dispatch HTTP
requests to. Without validation, a malicious or compromised admin (or a tenant
takeover) can point the configured URL at internal services — most dangerously
the cloud metadata endpoint (169.254.169.254) which exposes managed-identity
tokens. We resolve the hostname, refuse private/loopback/link-local addresses,
and require http(s).
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit


class UnsafeURLError(ValueError):
    """Raised when a URL targets a disallowed host or scheme."""


_ALLOWED_SCHEMES = {"http", "https"}


def _addr_is_private(addr: str) -> bool:
    try:
        ip = ipaddress.ip_address(addr)
    except ValueError:
        return False
    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def assert_safe_url(url: str) -> None:
    """Raise UnsafeURLError if `url` should not be fetched from this server.

    Checks:
      * scheme is http or https
      * host is present
      * if host is an IP literal, it is not RFC1918/loopback/link-local/etc.
      * if host is a name, DNS resolves to no disallowed addresses
    """
    if not url or not isinstance(url, str):
        raise UnsafeURLError("URL is empty")
    parts = urlsplit(url)
    if parts.scheme not in _ALLOWED_SCHEMES:
        raise UnsafeURLError(f"URL scheme '{parts.scheme}' is not allowed")
    host = parts.hostname
    if not host:
        raise UnsafeURLError("URL has no host")
    # Block obvious metadata-server hostnames even if DNS won't resolve them.
    if host.lower() in {"metadata.google.internal", "metadata", "localhost"}:
        raise UnsafeURLError(f"URL host '{host}' is not allowed")

    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, None)
        except socket.gaierror as exc:
            raise UnsafeURLError(f"URL host '{host}' did not resolve") from exc
        addresses = [info[4][0] for info in infos]

    for addr in addresses:
        if _addr_is_private(addr):
            raise UnsafeURLError(f"URL resolves to disallowed address {addr}")
