"""APEX onboarding auto-seeds a BulkCallingRequest so the SuperAdmin console can
surface the one-click DID auto-provision. APEX never hits the nokvo_one-only
/bulk-calling/request endpoint, so without this seed the button never appears.
Idempotent + APEX-only.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

from app.api.nokvo_one_onboarding import _ensure_apex_bulk_request


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _Result:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _DB:
    """Async DB stub: execute() yields a configurable 'existing request' row."""

    def __init__(self, existing_row=None):
        self.existing = existing_row
        self.added: list = []

    async def execute(self, *a, **k):
        return _Result(self.existing)

    def add(self, obj):
        self.added.append(obj)


def _apex_org():
    return SimpleNamespace(id=uuid.uuid4(), product_tier="nokvo_apex")


def test_apex_seeds_pending_request_when_none_exists():
    db = _DB(existing_row=None)
    org = _apex_org()
    user = SimpleNamespace(id=uuid.uuid4(), email="admin@acme.com")
    _run(_ensure_apex_bulk_request(db, org, SimpleNamespace(tenant_id="t-123"), user))
    assert len(db.added) == 1
    req = db.added[0]
    assert req.organization_id == org.id
    assert req.tenant_id == "t-123"
    assert req.requested_by_user_id == user.id
    assert req.contact_number == "admin@acme.com"  # admin email = ops contact
    assert req.status == "pending"


def test_idempotent_when_a_request_already_exists():
    db = _DB(existing_row=(uuid.uuid4(),))  # a non-None row → already requested
    user = SimpleNamespace(id=uuid.uuid4(), email="a@b.com")
    _run(_ensure_apex_bulk_request(db, _apex_org(), SimpleNamespace(tenant_id="t"), user))
    assert db.added == []


def test_noop_for_non_apex_org():
    db = _DB(existing_row=None)
    org = SimpleNamespace(id=uuid.uuid4(), product_tier="nokvo_one")
    user = SimpleNamespace(id=uuid.uuid4(), email="a@b.com")
    _run(_ensure_apex_bulk_request(db, org, SimpleNamespace(tenant_id="t"), user))
    assert db.added == []


def test_falls_back_when_admin_has_no_email():
    db = _DB(existing_row=None)
    user = SimpleNamespace(id=uuid.uuid4(), email=None)
    _run(_ensure_apex_bulk_request(db, _apex_org(), SimpleNamespace(tenant_id="t"), user))
    assert db.added[0].contact_number == "APEX auto-provision"
