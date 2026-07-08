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
    """Fake session: the V2 pre-checks (`exists` probe / v2.set_lead_status owner
    read) resolve to "no V2 row" so the endpoints exercise the legacy blob path
    these tests pin."""

    async def commit(self):
        pass

    async def execute(self, stmt, params=None):
        class _R:
            def scalar_one_or_none(self):
                return None

        return _R()


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
    camp = _campaign([{"call_link_id": "a", "phone": "+919", "name": "N", "lead_score": 1}])
    _lock_returns(patched, camp)
    user = _user()
    row = _run(m.apex_claim_lead(camp.id, "a", user=user, db=_DB()))
    assert row["claim_status"] == "claimed"
    assert camp.contacts[0]["claimed_by"] == str(user.id)
    # A different member claiming the same contact loses → 409.
    with pytest.raises(m.HTTPException) as exc:
        _run(m.apex_claim_lead(camp.id, "a", user=_user(), db=_DB()))
    assert exc.value.status_code == 409


def test_claim_rejects_unqualified_contact(patched):
    """Only QUALIFIED contacts are claimable — a guessed call_link_id must not
    let a member claim an unscored/failed contact (V2 enforces this in SQL; the
    blob path mirrors it)."""
    camp = _campaign([{"call_link_id": "a", "phone": "+919", "name": "N"}])  # unscored
    _lock_returns(patched, camp)
    with pytest.raises(m.HTTPException) as exc:
        _run(m.apex_claim_lead(camp.id, "a", user=_user(), db=_DB()))
    assert exc.value.status_code == 404


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


# ── max_score uses the weighted helper (never len(questions)) ────────────────────


def test_lead_row_max_score_weighted():
    """A weighted questionnaire (points/tiers) must show the weighted maximum —
    len(questions) rendered impossible '7/4' member-pool scores."""
    camp = _campaign(
        [],
        cfg={"questionnaire": {"questions": [
            {"points": 5}, {"tiers": [{"points": 1}, {"points": 3}]}, {},
        ], "threshold": 4}},
    )
    row = m._lead_row({"call_link_id": "a", "phone": "+919", "lead_score": 7}, camp)
    assert row["max_score"] == 9  # 5 + 3 + 1, not 3


# ── invite guard: one APEX account per email, product-wide ────────────────────────


def test_invite_rejects_email_owned_by_another_apex_org(patched):
    """Login resolves by email across ALL APEX orgs, so a second org's seat makes
    sign-in ambiguous (and invite-accept would overwrite the other org's row)."""
    org = SimpleNamespace(id=uuid.uuid4(), name="Org A", product_tier="nokvo_apex")
    other_org = SimpleNamespace(id=uuid.uuid4(), name="Org B", product_tier="nokvo_apex")
    other_user = SimpleNamespace(id=uuid.uuid4(), organization_id=other_org.id)

    class _InviteDB:
        def __init__(self):
            # In call order: _org (scalars().first() → inviter org), then
            # _resolve_apex_user (.first() → (user, OTHER org)) → 409.
            self._queue = [("scalars", org), ("first", (other_user, other_org))]

        async def execute(self, stmt, params=None):
            kind, value = self._queue.pop(0)

            class _R:
                def scalars(self):
                    return self

                def first(self):
                    return value

            return _R()

    inviter = SimpleNamespace(id=uuid.uuid4(), organization_id=org.id, full_name="Admin")
    with pytest.raises(m.HTTPException) as exc:
        _run(m.apex_invite_member(
            m.ApexMemberInviteRequest(email="dup@example.com"),
            background=SimpleNamespace(add_task=lambda *a, **k: None),
            inviter=inviter,
            db=_InviteDB(),
        ))
    assert exc.value.status_code == 409
    assert "another organization" in str(exc.value.detail)
