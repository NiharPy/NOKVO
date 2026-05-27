"""Nokvo Connect feature-flag tests.

Connect is a powerful surface (API key minting + a public voice/text API
backed by those keys). It must stay dormant until an operator explicitly
opts in via ``NOKVO_CONNECT_ENABLED`` in .env. These tests pin the
contract:

  1. When the flag is off, the FastAPI app does not register either
     Connect router — every Connect URL falls through to 404. Stale API
     keys minted before the flag flip cannot be used.
  2. When the flag is on, both routers register at their expected
     prefixes.
  3. The frontend-facing ``/api/nokvo-one/config`` endpoint surfaces the
     flag verbatim so the UI can hide the nav + landing pages without
     racing the backend.
"""
from __future__ import annotations

import importlib
import sys

import pytest

from app.core.config import settings


CONNECT_ADMIN_PREFIX = "/api/nokvo-one/connect"
CONNECT_PUBLIC_PREFIX = "/api/voice"


def _reload_main() -> object:
    """Reload :mod:`app.main` so the conditional ``include_router`` calls
    re-evaluate against the current ``settings.NOKVO_CONNECT_ENABLED``.
    Without the reload the router list is frozen at first import."""
    if "app.main" in sys.modules:
        return importlib.reload(sys.modules["app.main"])
    return importlib.import_module("app.main")


def _route_prefixes(app) -> set[str]:
    """Return the set of path prefixes the registered routes start with.
    Comparing prefixes (not exact paths) keeps the test stable when new
    Connect endpoints are added or renamed."""
    paths: set[str] = set()
    for route in app.routes:
        path = getattr(route, "path", "") or ""
        if path:
            paths.add(path)
    return paths


@pytest.fixture(autouse=True)
def _restore_flag():
    """Snapshot and restore the flag so test order can't bleed state into
    other suites (the ASGI app + router registration are imported by the
    rest of the test session)."""
    original = settings.NOKVO_CONNECT_ENABLED
    yield
    settings.NOKVO_CONNECT_ENABLED = original
    _reload_main()


def test_connect_routes_unregistered_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "NOKVO_CONNECT_ENABLED", False)
    main = _reload_main()
    paths = _route_prefixes(main.app)
    # Admin: API key CRUD lives under /api/nokvo-one/connect/api-keys.
    assert not any(p.startswith(f"{CONNECT_ADMIN_PREFIX}/api-keys") for p in paths)
    # Public: sessions live under /api/voice/sessions.
    assert not any(p.startswith(f"{CONNECT_PUBLIC_PREFIX}/sessions") for p in paths)


def test_connect_routes_registered_when_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "NOKVO_CONNECT_ENABLED", True)
    main = _reload_main()
    paths = _route_prefixes(main.app)
    # Both halves of Connect must show up under their conventional prefixes.
    assert any(p.startswith(f"{CONNECT_ADMIN_PREFIX}/api-keys") for p in paths)
    assert any(p.startswith(f"{CONNECT_PUBLIC_PREFIX}/sessions") for p in paths)


def test_nokvo_connect_default_is_off():
    """Brand new deployments must NOT expose Connect until the operator
    flips the env var. If this test ever flips, someone changed the
    default and should justify it in review."""
    # Re-read the class default (instance state could have been mutated
    # by an earlier test even though the fixture restores it; the class
    # default is the canonical source).
    from app.core.config import Settings
    assert Settings.model_fields["NOKVO_CONNECT_ENABLED"].default is False


def test_config_endpoint_surfaces_flag_state(monkeypatch):
    """The frontend reads ``nokvo_connect_enabled`` off /config to decide
    whether to render the nav button. The field must always be present
    and reflect the current setting in real time."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "NOKVO_CONNECT_ENABLED", False)
    main = _reload_main()
    with TestClient(main.app) as client:
        body = client.get("/api/nokvo-one/config").json()
    assert "nokvo_connect_enabled" in body
    assert body["nokvo_connect_enabled"] is False

    monkeypatch.setattr(settings, "NOKVO_CONNECT_ENABLED", True)
    main = _reload_main()
    with TestClient(main.app) as client:
        body = client.get("/api/nokvo-one/config").json()
    assert body["nokvo_connect_enabled"] is True


def test_disabled_connect_endpoints_return_404(monkeypatch):
    """End-to-end: when the flag is off, hitting a Connect URL returns
    404 (FastAPI's default for an unregistered path). This is the actual
    guarantee that a leaked API key can't be used in a deployment that
    has Connect turned off."""
    from fastapi.testclient import TestClient

    monkeypatch.setattr(settings, "NOKVO_CONNECT_ENABLED", False)
    main = _reload_main()
    with TestClient(main.app) as client:
        # Admin side — list keys.
        r1 = client.get(f"{CONNECT_ADMIN_PREFIX}/api-keys")
        # Public side — start a session.
        r2 = client.post(f"{CONNECT_PUBLIC_PREFIX}/sessions", json={})
    assert r1.status_code == 404
    assert r2.status_code == 404
