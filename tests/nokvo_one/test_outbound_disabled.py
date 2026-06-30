"""Nokvo One outbound kill switch (NOKVO_ONE_OUTBOUND_ENABLED).

Outbound calling has moved to the dedicated NOKVO APEX product, so Nokvo One is
inbound-only by default. These pin the two enforcement points:
  * the API gate (``_nokvo_one_outbound_blocked`` / ``_require_outbound_enabled``)
  * the dialer choke point (``_dial_pending`` returns before placing a call)
APEX orgs (product_tier="nokvo_apex") share the backend and must stay exempt.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

import app.api.nokvo_one_voice as voice_api
from app.core.config import settings
from app.services.outbound_campaign_service import OutboundCampaignService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Result:
    def __init__(self, val):
        self._val = val

    def scalar_one_or_none(self):
        return self._val


class _DB:
    """Minimal async DB stub: every execute() yields the configured tier."""

    def __init__(self, tier):
        self._tier = tier

    async def execute(self, *a, **k):
        return _Result(self._tier)


# ── API helper ────────────────────────────────────────────────────────────────


def test_blocked_for_nokvo_one_when_flag_off(monkeypatch):
    monkeypatch.setattr(settings, "NOKVO_ONE_OUTBOUND_ENABLED", False)
    assert voice_api._nokvo_one_outbound_blocked(SimpleNamespace(product_tier="nokvo_one")) is True
    # Legacy orgs with a NULL product_tier default to the Nokvo One product.
    assert voice_api._nokvo_one_outbound_blocked(SimpleNamespace(product_tier=None)) is True


def test_apex_is_never_blocked(monkeypatch):
    monkeypatch.setattr(settings, "NOKVO_ONE_OUTBOUND_ENABLED", False)
    assert voice_api._nokvo_one_outbound_blocked(SimpleNamespace(product_tier="nokvo_apex")) is False


def test_not_blocked_when_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "NOKVO_ONE_OUTBOUND_ENABLED", True)
    assert voice_api._nokvo_one_outbound_blocked(SimpleNamespace(product_tier="nokvo_one")) is False


def test_require_outbound_enabled_403s_for_nokvo_one(monkeypatch):
    monkeypatch.setattr(settings, "NOKVO_ONE_OUTBOUND_ENABLED", False)
    org = SimpleNamespace(product_tier="nokvo_one", calling_enabled=True)

    async def fake_org(db, user):
        return org

    monkeypatch.setattr(voice_api, "_org_for_user", fake_org)
    with pytest.raises(voice_api.HTTPException) as exc:
        _run(voice_api._require_outbound_enabled(object(), object()))
    assert exc.value.status_code == 403


# ── dialer choke point ────────────────────────────────────────────────────────


def test_dialer_skips_nokvo_one_when_disabled(monkeypatch):
    """A running Nokvo One campaign must never place a call — the dialer returns
    before acquiring the row lock (so well before ``_place_call``)."""
    monkeypatch.setattr(settings, "NOKVO_ONE_OUTBOUND_ENABLED", False)
    locked: list = []

    async def fake_lock(db, cid):
        locked.append(cid)
        return None

    monkeypatch.setattr(OutboundCampaignService, "_lock_campaign", staticmethod(fake_lock))
    campaign = SimpleNamespace(id=uuid.uuid4(), agent_config={})
    tenant_res = SimpleNamespace(organization_id=uuid.uuid4())
    _run(
        OutboundCampaignService._dial_pending(
            campaign, _DB("nokvo_one"), tenant_res=tenant_res, base="http://x", prefix="p"
        )
    )
    assert locked == []  # returned before the lock → never dialed


def test_dialer_still_runs_for_apex_when_nokvo_one_disabled(monkeypatch):
    """APEX is exempt: with Nokvo One outbound off, an APEX campaign gets past the
    kill switch (proven by reaching the row lock)."""
    monkeypatch.setattr(settings, "NOKVO_ONE_OUTBOUND_ENABLED", False)

    async def fake_has_balance(db, org_id):
        return True

    monkeypatch.setattr("app.services.minute_balance_service.has_balance", fake_has_balance)
    locked: list = []

    async def fake_lock(db, cid):
        locked.append(cid)
        return None  # stop right after the lock, before any placement

    monkeypatch.setattr(OutboundCampaignService, "_lock_campaign", staticmethod(fake_lock))
    campaign = SimpleNamespace(id=uuid.uuid4(), agent_config={"deterministic": True, "call_window": None})
    tenant_res = SimpleNamespace(organization_id=uuid.uuid4())
    _run(
        OutboundCampaignService._dial_pending(
            campaign, _DB("nokvo_apex"), tenant_res=tenant_res, base="http://x", prefix="p"
        )
    )
    assert locked  # got PAST the kill switch → APEX still dials
