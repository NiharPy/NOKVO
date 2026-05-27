"""Regression tests for :mod:`app.services.agent_runtime_bundle`.

The bundle caches a per-tenant snapshot of the Organization row,
business-template overrides, and policy cards across requests. The key
risk is that the cached Organization is an ORM instance loaded by a
session that has since closed — accessing it from a later turn would
trigger SQLAlchemy's ``greenlet_spawn has not been called`` error.

These tests verify that:

* ``_load_organization`` detaches the instance via ``expunge``, so
  scalar reads after the session closes do not require IO.
* ``get_bundle`` self-heals when the cached Organization becomes
  unusable (e.g. session destroyed in a way that prevents access).
* The bundle caches by version key and invalidates on tenant changes.
"""

from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services import agent_runtime_bundle as bundle_module
from app.services.agent_runtime_bundle import (
    RuntimeBundle,
    _bundle_is_usable,
    _version_key,
    get_bundle,
    invalidate,
    invalidate_all,
)


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _fake_organization(*, industry: str = "clinics", name: str = "Test Clinic") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        name=name,
        industry=industry,
    )


def _fake_tenant(provider_status: dict[str, Any] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        tenant_id=str(uuid.uuid4()),
        organization_id=uuid.uuid4(),
        provider_status=dict(provider_status or {}),
    )


class _FakeSession:
    """Minimal AsyncSession stub: returns a fixed Organization, tracks
    expunge calls, and refuses further IO after close."""

    def __init__(self, organization: SimpleNamespace | None):
        self._organization = organization
        self.expunged: list[Any] = []
        self.closed = False
        self.sync_session = SimpleNamespace(expunge=self._expunge)

    def _expunge(self, instance: Any) -> None:
        self.expunged.append(instance)

    def expunge(self, instance: Any) -> None:
        self.expunged.append(instance)

    async def execute(self, *args, **kwargs):
        if self.closed:
            raise RuntimeError("session is closed")
        org = self._organization

        class _Scalars:
            def first(self_inner):
                return org

        class _Result:
            def scalars(self_inner):
                return _Scalars()

        return _Result()


@pytest.fixture(autouse=True)
def _reset_cache():
    invalidate_all()
    yield
    invalidate_all()


# ── Detach behaviour ────────────────────────────────────────────────────────


def test_load_organization_expunges_loaded_instance():
    organization = _fake_organization()
    session = _FakeSession(organization)
    tenant = _fake_tenant()
    tenant.organization_id = organization.id

    bundle = _run(get_bundle(session, tenant))

    # The Organization was expunged so it's safe to read across
    # sessions.
    assert session.expunged == [organization]
    assert bundle.organization is organization
    assert bundle.organization_industry == "clinics"
    assert bundle.organization_name == "Test Clinic"


def test_bundle_remains_usable_after_session_close():
    """Closing the loading session must not affect the cached bundle —
    the whole point of the detach + materialise pass."""
    organization = _fake_organization(industry="real_estate", name="Acme Estates")
    session = _FakeSession(organization)
    tenant = _fake_tenant()
    tenant.organization_id = organization.id

    bundle = _run(get_bundle(session, tenant))
    session.closed = True

    # All scalar fields still work.
    assert bundle.organization_industry == "real_estate"
    assert bundle.organization_name == "Acme Estates"
    assert bundle.organization.industry == "real_estate"
    assert _bundle_is_usable(bundle) is True


# ── Cache hit / miss semantics ──────────────────────────────────────────────


def test_get_bundle_returns_same_instance_on_hit():
    organization = _fake_organization()
    session = _FakeSession(organization)
    tenant = _fake_tenant()
    tenant.organization_id = organization.id

    first = _run(get_bundle(session, tenant))
    second = _run(get_bundle(session, tenant))
    assert first is second


def test_get_bundle_cache_is_scoped_by_organization_id():
    """One tenant can own multiple Nokvo One organizations. Cached industry
    data must not bleed from a clinic account into a real-estate account."""
    tenant_id = str(uuid.uuid4())
    clinic = _fake_organization(industry="clinics", name="Eye Clinic")
    estate = _fake_organization(industry="real_estate", name="Raghava Estates")
    clinic_tenant = _fake_tenant()
    clinic_tenant.tenant_id = tenant_id
    clinic_tenant.organization_id = clinic.id
    estate_tenant = _fake_tenant()
    estate_tenant.tenant_id = tenant_id
    estate_tenant.organization_id = estate.id

    first = _run(get_bundle(_FakeSession(clinic), clinic_tenant))
    second = _run(get_bundle(_FakeSession(estate), estate_tenant))

    assert first is not second
    assert first.organization_industry == "clinics"
    assert second.organization_industry == "real_estate"


def test_version_key_changes_invalidate_cache():
    organization = _fake_organization()
    session = _FakeSession(organization)
    tenant = _fake_tenant({"agent_policy_version": "v1"})
    tenant.organization_id = organization.id

    first = _run(get_bundle(session, tenant))
    # Mutating the tenant's provider_status changes the version key, so
    # the cached bundle becomes stale and the next call rebuilds.
    tenant.provider_status["agent_policy_version"] = "v2"
    second = _run(get_bundle(session, tenant))
    assert first is not second
    assert _version_key(tenant) == second.version_key


def test_invalidate_drops_cached_bundle():
    organization = _fake_organization()
    session = _FakeSession(organization)
    tenant = _fake_tenant()
    tenant.organization_id = organization.id

    first = _run(get_bundle(session, tenant))
    invalidate(tenant.tenant_id)
    second = _run(get_bundle(session, tenant))
    assert first is not second


# ── Self-heal when the cached bundle goes bad ───────────────────────────────


def test_get_bundle_rebuilds_when_cached_instance_unreadable(monkeypatch):
    """If touching the cached Organization raises (simulating the
    greenlet_spawn failure mode), get_bundle rebuilds rather than
    propagating the error to the turn."""
    organization = _fake_organization()
    session = _FakeSession(organization)
    tenant = _fake_tenant()
    tenant.organization_id = organization.id

    _run(get_bundle(session, tenant))

    # Sabotage the cached bundle's Organization so attribute access
    # raises. We swap the cache entry's organization with a magic mock
    # whose `id` access raises — mirroring the real ORM-detach failure.
    cache_key = bundle_module._cache_key(tenant)
    cached = bundle_module._cache[cache_key][1]
    broken_org = MagicMock()
    type(broken_org).id = property(lambda self: (_ for _ in ()).throw(
        RuntimeError("greenlet_spawn has not been called; can't call await_only() here")
    ))
    # Frozen dataclass — replace the cache entry wholesale.
    new_bundle = RuntimeBundle(
        tenant_id=cached.tenant_id,
        organization=broken_org,
        organization_industry=cached.organization_industry,
        organization_name=cached.organization_name,
        overrides=cached.overrides,
        custom_tabs=cached.custom_tabs,
        policy_cards=cached.policy_cards,
        single_prompt_guidance=cached.single_prompt_guidance,
        single_prompt_enabled=cached.single_prompt_enabled,
        version_key=cached.version_key,
    )
    expires_at, _ = bundle_module._cache[cache_key]
    bundle_module._cache[cache_key] = (expires_at, new_bundle)
    assert _bundle_is_usable(new_bundle) is False

    # The next get_bundle call should self-heal and return a fresh
    # bundle whose organization is the original (good) instance.
    rebuilt = _run(get_bundle(session, tenant))
    assert rebuilt is not new_bundle
    assert rebuilt.organization is organization
    assert _bundle_is_usable(rebuilt) is True


def test_get_bundle_handles_missing_organization():
    """When the DB has no Organization row the bundle is still returned
    but with empty industry/name — callers fall through to the
    no-business-context branch."""
    session = _FakeSession(None)
    tenant = _fake_tenant()

    bundle = _run(get_bundle(session, tenant))
    assert bundle.organization is None
    assert bundle.organization_industry == ""
    assert bundle.organization_name == ""
    assert bundle.as_business_context_tuple() is None


def test_get_bundle_tolerates_db_failure():
    """If the DB query raises, get_bundle returns a bundle without
    organization details rather than crashing the turn."""

    class _BrokenSession:
        sync_session = SimpleNamespace(expunge=lambda x: None)

        async def execute(self, *args, **kwargs):
            raise RuntimeError("connection refused")

    tenant = _fake_tenant()
    bundle = _run(get_bundle(_BrokenSession(), tenant))
    assert bundle.organization is None
    assert bundle.organization_industry == ""
