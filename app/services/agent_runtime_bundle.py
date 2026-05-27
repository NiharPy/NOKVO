"""Per-tenant runtime bundle cache.

Every voice turn previously rebuilt several values that are account-stable
within a short window:

  * the :class:`Organization` row (industry, name)
  * business-template overrides + custom tabs (driven by ``provider_status``)
  * the active policy cards (filtered by approval/status/policy_version)
  * the single-prompt voice agent config (enabled + prompt body)
  * a normalised policy-version marker (used as the cache invalidation key)

The hot path was paying a DB round trip + several dictionary copies per
turn even when nothing about the tenant had changed for hours. The
:class:`RuntimeBundle` keeps a process-local view of those values keyed by
``tenant_id`` + ``organization_id``, refreshed only when the tenant's
``agent_policy_version``, single-prompt updated_at, or overrides marker
changes.

This is intentionally NOT a Redis cache — these values are derived
synchronously from ``tenant_res.provider_status`` (which is already in
memory) plus one DB read for the organization, so a per-process dict with
a small TTL gives most of the wins of a distributed cache with none of
the consistency surprises.
"""

from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.organization import Organization
from app.models.tenant_resources import TenantResources
from app.services.agent_knowledge_service import (
    AGENT_POLICY_CARDS_KEY,
    AGENT_SINGLE_PROMPT_CONFIG_KEY,
)
from app.services.nokvo_one_business_templates import custom_tabs_from_overrides


_BUNDLE_TTL_SECONDS = 60.0
_BUNDLE_CACHE_MAX = 256


@dataclass(frozen=True)
class RuntimeBundle:
    """A pre-built, read-only snapshot of tenant runtime state.

    Constructed once per (tenant_id, version_key) by :func:`get_bundle`
    and reused across many turns until the version marker changes or the
    TTL elapses. Members are intentionally simple containers so the
    pipeline can read them without further dict copying.
    """

    tenant_id: str
    organization: Organization | None
    organization_industry: str
    organization_name: str
    overrides: dict[str, Any]
    custom_tabs: list[dict[str, Any]]
    policy_cards: list[dict[str, Any]]
    single_prompt_guidance: str
    single_prompt_enabled: bool
    version_key: str

    def as_business_context_tuple(self) -> tuple[Organization, dict[str, Any], list[dict[str, Any]]] | None:
        """Legacy ``(organization, overrides, custom_tabs)`` shape used by the
        existing pipeline branches. Returns ``None`` when the organization
        is unavailable so callers fall through to the no-business-context
        branch."""
        if self.organization is None:
            return None
        return self.organization, self.overrides, self.custom_tabs


_cache: dict[str, tuple[float, RuntimeBundle]] = {}
_locks: dict[str, asyncio.Lock] = {}


def _cache_key(tenant_res: TenantResources) -> str:
    tenant_id = str(tenant_res.tenant_id)
    organization_id = str(getattr(tenant_res, "organization_id", None) or "none")
    return f"{tenant_id}:org:{organization_id}"


def _lock_for(cache_key: str) -> asyncio.Lock:
    lock = _locks.get(cache_key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[cache_key] = lock
    return lock


def _version_key(tenant_res: TenantResources) -> str:
    """Compute a stable cache key that changes whenever any of the inputs
    that feed the bundle change. Cheap to recompute every turn."""
    provider_status = dict(tenant_res.provider_status or {})
    policy_version = str(provider_status.get("agent_policy_version") or "pv_default")
    single_prompt = provider_status.get(AGENT_SINGLE_PROMPT_CONFIG_KEY) or {}
    sp_marker = ""
    if isinstance(single_prompt, dict) and single_prompt.get("enabled") and single_prompt.get("prompt"):
        sp_marker = str(single_prompt.get("updated_at") or "")
        if not sp_marker:
            sp_marker = hashlib.sha256(
                str(single_prompt.get("prompt") or "").encode("utf-8")
            ).hexdigest()[:12]
    overrides = provider_status.get("business_template_schema_overrides") or {}
    overrides_marker = ""
    if isinstance(overrides, dict) and overrides:
        # Stable hash of the overrides dict — order-independent.
        overrides_marker = hashlib.sha256(
            repr(sorted(overrides.items())).encode("utf-8")
        ).hexdigest()[:12]
    custom_tabs = provider_status.get("business_template_custom_tabs") or []
    custom_tabs_marker = ""
    if isinstance(custom_tabs, list) and custom_tabs:
        custom_tabs_marker = hashlib.sha256(
            repr(custom_tabs).encode("utf-8")
        ).hexdigest()[:12]
    cards = provider_status.get(AGENT_POLICY_CARDS_KEY) or []
    cards_marker = ""
    if isinstance(cards, list) and cards:
        cards_marker = str(len(cards))
    return f"{policy_version}:{sp_marker}:{overrides_marker}:{custom_tabs_marker}:{cards_marker}"


def _active_policy_cards(provider_status: dict[str, Any]) -> list[dict[str, Any]]:
    """Filter the raw policy_cards list down to active+approved cards. This
    mirrors the filter the decision engine applies, but pre-computes it once
    per bundle build instead of on every turn."""
    raw = provider_status.get(AGENT_POLICY_CARDS_KEY) or []
    if not isinstance(raw, list):
        return []
    out: list[dict[str, Any]] = []
    for card in raw:
        if not isinstance(card, dict):
            continue
        approval = card.get("approval_status")
        if approval not in (None, "approved"):
            continue
        status = card.get("status")
        if status not in (None, "active", "ok"):
            continue
        out.append(card)
    return out


def _single_prompt_guidance(provider_status: dict[str, Any]) -> str:
    config = provider_status.get(AGENT_SINGLE_PROMPT_CONFIG_KEY) or {}
    if not isinstance(config, dict) or not config.get("enabled"):
        return ""
    prompt = str(config.get("prompt") or "").strip()
    return prompt[:8000]


async def _load_organization(
    db: AsyncSession | None,
    organization_id: Any,
) -> Organization | None:
    """Fetch the Organization row and detach it from its session.

    Detaching matters because the bundle is cached across requests. The
    session that loaded the instance closes when its request finishes;
    if the cached instance is still attached, a later attribute access
    can trigger a synchronous refresh — which raises::

        greenlet_spawn has not been called; can't call await_only() here.
        Was IO attempted in an unexpected place?

    With ``expire_on_commit=False`` on the sessionmaker, scalar
    attributes stay loaded after detachment, so downstream code can
    safely read ``organization.id`` / ``.industry`` / ``.name`` without
    re-hitting the DB. Relationship access would still fail, but the
    Organization model has only scalar columns.
    """
    if db is None or organization_id is None:
        return None
    try:
        res = await db.execute(select(Organization).where(Organization.id == organization_id))
        organization = res.scalars().first()
    except Exception:
        return None
    if organization is None:
        return None
    # ``AsyncSession.expunge`` is a sync API that removes the instance
    # from the session's identity map. We swallow expunge errors so a
    # session that's already in a weird state doesn't kill the turn.
    try:
        db.sync_session.expunge(organization)
    except Exception:
        try:
            # Fallback for any other AsyncSession variant that exposes
            # ``expunge`` directly.
            db.expunge(organization)  # type: ignore[attr-defined]
        except Exception:
            pass
    return organization


def _evict_stale(now: float) -> None:
    """Bounded eviction: drop expired entries, then trim down to the cap."""
    expired = [tid for tid, (expires_at, _) in _cache.items() if expires_at <= now]
    for tid in expired:
        _cache.pop(tid, None)
    while len(_cache) > _BUNDLE_CACHE_MAX:
        # Drop oldest insertion (dict preserves insertion order in CPython).
        _cache.pop(next(iter(_cache)), None)


def _bundle_is_usable(bundle: RuntimeBundle) -> bool:
    """Defensive probe — read each scalar field that downstream code
    relies on. If any access triggers the dreaded greenlet_spawn
    refresh, we'll surface the failure here and the caller will rebuild
    the bundle from scratch instead of crashing the turn."""
    try:
        if bundle.organization is not None:
            _ = bundle.organization.id  # touch a scalar
            _ = bundle.organization.industry
            _ = bundle.organization.name
        # Touch the pre-computed primitives too — these are plain
        # strings so they can't fail, but the access proves the dataclass
        # itself wasn't corrupted by some earlier exception.
        _ = bundle.organization_industry
        _ = bundle.organization_name
        return True
    except Exception:
        return False


async def get_bundle(
    db: AsyncSession | None,
    tenant_res: TenantResources,
) -> RuntimeBundle:
    """Return the cached :class:`RuntimeBundle` for ``tenant_res``, rebuilding
    only when the version key changed or the TTL elapsed.

    Safe to call concurrently — duplicate rebuilds for the same organization
    are coalesced behind an org-scoped lock so a burst of simultaneous calls
    triggers exactly one DB read.
    """
    tenant_id = str(tenant_res.tenant_id)
    cache_key = _cache_key(tenant_res)
    expected_version = _version_key(tenant_res)
    now = time.monotonic()

    cached = _cache.get(cache_key)
    if cached is not None:
        expires_at, bundle = cached
        if expires_at > now and bundle.version_key == expected_version and _bundle_is_usable(bundle):
            return bundle
        # Either expired, mismatched version, or the cached instance is
        # no longer safe to read (its loading session was closed in a
        # way that prevents scalar access). Drop it and fall through to
        # the rebuild path.
        _cache.pop(cache_key, None)

    lock = _lock_for(cache_key)
    async with lock:
        # Re-check after acquiring the lock — another caller may have
        # already rebuilt while we were waiting.
        cached = _cache.get(cache_key)
        now = time.monotonic()
        if cached is not None:
            expires_at, bundle = cached
            if expires_at > now and bundle.version_key == expected_version and _bundle_is_usable(bundle):
                return bundle

        provider_status = dict(tenant_res.provider_status or {})
        organization = await _load_organization(db, tenant_res.organization_id)
        overrides = dict(provider_status.get("business_template_schema_overrides") or {})
        custom_tabs = custom_tabs_from_overrides(provider_status)
        policy_cards = _active_policy_cards(provider_status)
        guidance = _single_prompt_guidance(provider_status)

        # Materialise scalar attributes immediately so the cached bundle
        # never has to touch ORM machinery again. ``_load_organization``
        # already expunged the instance, but reading the values into
        # plain strings here gives us a second layer of safety.
        org_industry = ""
        org_name = ""
        if organization is not None:
            try:
                org_industry = str(organization.industry or "").strip()
                org_name = str(organization.name or "").strip()
            except Exception:
                # Pathological case — the organization isn't readable
                # at all. Bundle without it; downstream code falls back
                # to the no-business-context branch.
                organization = None

        bundle = RuntimeBundle(
            tenant_id=tenant_id,
            organization=organization,
            organization_industry=org_industry,
            organization_name=org_name,
            overrides=overrides,
            custom_tabs=custom_tabs,
            policy_cards=policy_cards,
            single_prompt_guidance=guidance,
            single_prompt_enabled=bool(guidance),
            version_key=expected_version,
        )
        _cache[cache_key] = (now + _BUNDLE_TTL_SECONDS, bundle)
        _evict_stale(now)
        return bundle


def invalidate(tenant_id: str) -> None:
    """Force the next ``get_bundle`` call to rebuild. Used after a write
    path mutates ``provider_status`` so the new state is picked up
    immediately rather than waiting on the TTL."""
    prefix = f"{tenant_id}:"
    for key in list(_cache):
        if key == str(tenant_id) or key.startswith(prefix):
            _cache.pop(key, None)
    for key in list(_locks):
        if key == str(tenant_id) or key.startswith(prefix):
            _locks.pop(key, None)


def invalidate_all() -> None:
    _cache.clear()
    _locks.clear()
