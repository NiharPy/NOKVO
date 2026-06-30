"""APEX qualified-lead claim pool: server-side qualification (mirrors the frontend
``leadCategory``), the contact lookup, and the claim/status endpoints' exclusivity
(first-come 409, claimer-only status, value validation). The campaign row-lock +
``flag_modified`` are mocked so this stays a fast unit test of the logic.
"""
from __future__ import annotations

import asyncio
import uuid
from types import SimpleNamespace

import pytest

import app.api.nokvo_one_apex_members as m
from app.services.outbound_campaign_service import OutboundCampaignService


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class _DB:
    async def commit(self):
        pass


def _campaign(contacts, cfg=None):
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id="tnt-1",
        name="C1",
        agent_config=cfg or {"deterministic": True, "questionnaire": {"questions": [{}], "threshold": 1}},
        contacts=contacts,
    )


def _user(uid=None):
    return SimpleNamespace(id=uid or uuid.uuid4(), organization_id=uuid.uuid4(), full_name="Member")


# ── _is_qualified mirrors the frontend leadCategory "successful" branch ──────────


def test_is_qualified_threshold_and_flag():
    cfg = {"questionnaire": {"questions": [{}, {}], "threshold": 2}}
    assert m._is_qualified({"lead_score": 2}, cfg) is True       # at threshold
    assert m._is_qualified({"lead_score": 3}, cfg) is True       # above
    assert m._is_qualified({"lead_score": 1}, cfg) is False      # below
    assert m._is_qualified({"qualified": True, "lead_score": 0}, cfg) is True  # flag wins
    assert m._is_qualified({}, cfg) is False                     # unscored → not yet qualified


def test_is_qualified_interest_when_no_questionnaire():
    assert m._is_qualified({"interest_outcome": "interested"}, {}) is True
    assert m._is_qualified({"interest_outcome": "not_interested"}, {}) is False
    assert m._is_qualified({}, {}) is False


def test_find_contact_by_call_link_id():
    camp = _campaign([{"call_link_id": "a"}, {"call_link_id": "b"}])
    idx, c = m._find_contact(camp, "b")
    assert idx == 1 and c["call_link_id"] == "b"
    assert m._find_contact(camp, "zzz") == (None, None)


# ── claim + status endpoints (row-lock + flag_modified mocked) ───────────────────


@pytest.fixture
def patched(monkeypatch):
    monkeypatch.setattr(m, "flag_modified", lambda *a, **k: None)

    async def fake_tenant(db, org_id):
        return SimpleNamespace(tenant_id="tnt-1")

    monkeypatch.setattr(m, "_tenant", fake_tenant)
    return monkeypatch


def _lock_returns(monkeypatch, camp):
    async def fake_lock(db, cid):
        return camp

    monkeypatch.setattr(OutboundCampaignService, "_lock_campaign", staticmethod(fake_lock))


def test_claim_sets_owner_then_second_claim_409(patched):
    camp = _campaign([{"call_link_id": "a", "phone": "+919", "name": "N"}])
    _lock_returns(patched, camp)
    user = _user()
    row = _run(m.apex_claim_lead(camp.id, "a", user=user, db=_DB()))
    assert row["claim_status"] == "claimed"
    assert camp.contacts[0]["claimed_by"] == str(user.id)
    # A different member claiming the same contact loses → 409.
    with pytest.raises(m.HTTPException) as exc:
        _run(m.apex_claim_lead(camp.id, "a", user=_user(), db=_DB()))
    assert exc.value.status_code == 409


def test_claim_unknown_contact_404(patched):
    camp = _campaign([{"call_link_id": "a"}])
    _lock_returns(patched, camp)
    with pytest.raises(m.HTTPException) as exc:
        _run(m.apex_claim_lead(camp.id, "missing", user=_user(), db=_DB()))
    assert exc.value.status_code == 404


def test_status_only_claimer_may_update(patched):
    owner = _user()
    camp = _campaign([{"call_link_id": "a", "claimed_by": str(owner.id), "claim_status": "claimed"}])
    _lock_returns(patched, camp)
    _run(m.apex_set_lead_status(camp.id, "a", m.ApexLeadStatusRequest(status="won"), user=owner, db=_DB()))
    assert camp.contacts[0]["claim_status"] == "won"
    with pytest.raises(m.HTTPException) as exc:
        _run(m.apex_set_lead_status(camp.id, "a", m.ApexLeadStatusRequest(status="lost"), user=_user(), db=_DB()))
    assert exc.value.status_code == 403


def test_status_rejects_bad_value(patched):
    owner = _user()
    camp = _campaign([{"call_link_id": "a", "claimed_by": str(owner.id)}])
    _lock_returns(patched, camp)
    with pytest.raises(m.HTTPException) as exc:
        _run(m.apex_set_lead_status(camp.id, "a", m.ApexLeadStatusRequest(status="bogus"), user=owner, db=_DB()))
    assert exc.value.status_code == 400
