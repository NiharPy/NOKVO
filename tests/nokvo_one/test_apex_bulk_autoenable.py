"""APEX orgs auto-enable bulk calling from their account-creation subaccount — the
`upload & start calling` gate no longer 403s waiting on a manual operator grant.
Pins the branching in `_ensure_apex_bulk_enabled` (APEX-only, only when disabled)."""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

import app.api.nokvo_one_voice as v
from app.services.plivo_service import PlivoService
from app.services.plivo_bulk_provisioning_service import PlivoBulkProvisioningService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _patch(monkeypatch, *, product_tier, enabled):
    calls = []

    async def fake_org(db, user):
        return SimpleNamespace(product_tier=product_tier)

    async def fake_grant(tr, db, *, numbers=None, superadmin_id=None):
        calls.append({"numbers": numbers, "superadmin_id": superadmin_id})
        return {"enabled": True}

    monkeypatch.setattr(v, "_org_for_user", fake_org)
    monkeypatch.setattr(PlivoService, "bulk_calling_enabled", staticmethod(lambda tr: enabled))
    monkeypatch.setattr(PlivoBulkProvisioningService, "grant_with_existing_subaccount", staticmethod(fake_grant))
    return calls


def test_apex_disabled_triggers_auto_enable(monkeypatch):
    calls = _patch(monkeypatch, product_tier="nokvo_apex", enabled=False)
    is_apex = _run(v._ensure_apex_bulk_enabled(object(), object(), object()))
    assert is_apex is True
    assert len(calls) == 1
    assert calls[0]["numbers"] is None and calls[0]["superadmin_id"] == "auto:apex"


def test_apex_already_enabled_is_noop(monkeypatch):
    calls = _patch(monkeypatch, product_tier="nokvo_apex", enabled=True)
    is_apex = _run(v._ensure_apex_bulk_enabled(object(), object(), object()))
    assert is_apex is True
    assert calls == []  # already enabled → no grant


def test_non_apex_never_auto_enables(monkeypatch):
    calls = _patch(monkeypatch, product_tier="nokvo_one", enabled=False)
    is_apex = _run(v._ensure_apex_bulk_enabled(object(), object(), object()))
    assert is_apex is False
    assert calls == []  # Nokvo One is never auto-enabled here


def test_auto_enable_swallows_grant_errors(monkeypatch):
    _patch(monkeypatch, product_tier="nokvo_apex", enabled=False)

    async def boom(tr, db, *, numbers=None, superadmin_id=None):
        raise RuntimeError("plivo down")

    monkeypatch.setattr(PlivoBulkProvisioningService, "grant_with_existing_subaccount", staticmethod(boom))
    # Best-effort: a grant failure must not raise out of the gate helper.
    assert _run(v._ensure_apex_bulk_enabled(object(), object(), object())) is True
