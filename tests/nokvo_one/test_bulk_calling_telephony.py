"""Regression tests for bulk-calling telephony correctness.

Root cause of the "bulk upload not calling" bug: the dedicated Plivo caller ID
was stored unusable (formatted with spaces, and not actually rented on the
dedicated account), so Plivo 400'd every /Call and contacts silently failed.

These lock in the fixes:
  * numbers are normalized to bare digits before they reach Plivo;
  * the SuperAdmin grant pre-flights creds + number ownership and refuses to
    enable a config that can't place calls.
"""
import pytest

from app.services.plivo_service import PlivoError, PlivoService


def _stub_single_campaign_guard(monkeypatch):
    """These are DB-less service units — skip the one-campaign-per-tenant guard
    (it needs a real session for its advisory lock + status query; it has its
    own tests in test_single_active_campaign.py)."""
    from app.services.outbound_campaign_service import OutboundCampaignService

    async def _no_guard(db, tenant_id, *, exclude_id=None):
        return None

    monkeypatch.setattr(
        OutboundCampaignService, "_assert_no_other_active_campaign", staticmethod(_no_guard)
    )


def test_normalize_number_strips_formatting():
    assert PlivoService.normalize_number("+91 22 6423 2977") == "912264232977"
    assert PlivoService.normalize_number("918031321315") == "918031321315"
    assert PlivoService.normalize_number("+1 (415) 555-0100") == "14155550100"
    assert PlivoService.normalize_number(None) == ""


@pytest.mark.asyncio
async def test_initiate_outbound_call_normalizes_from_and_to(monkeypatch):
    sent = {}

    async def fake_request(method, url, *, auth, json_body=None):
        sent["url"] = url
        sent["body"] = json_body
        return {"call": {"sid": "X"}}

    monkeypatch.setattr(PlivoService, "_request", staticmethod(fake_request))

    class _TR:  # minimal stand-in; auth_override means tenant cfg is unused
        provider_status = {}
        twilio_phone_number = None

    await PlivoService.initiate_outbound_call(
        _TR(),
        to_number="+91 75696 72503",
        answer_url="https://x/answer",
        from_number="+91 22 6423 2977",
        auth_override=("SAxxxxxxxxxxxxxxxxxx", "token"),
    )
    assert sent["body"]["from"] == "912264232977"  # no spaces reach Plivo
    assert sent["body"]["to"] == "917569672503"
    # Ring cap: an unanswered call is cancelled BEFORE carrier voicemail (~30-45s)
    # picks up — never Plivo's 120s default, always inside the accepted 5-600.
    assert 5 <= sent["body"]["ring_timeout"] <= 29


@pytest.mark.asyncio
async def test_ring_timeout_clamped_to_plivo_range(monkeypatch):
    from app.core.config import settings

    sent = {}

    async def fake_request(method, url, *, auth, json_body=None):
        sent["body"] = json_body
        return {"call": {"sid": "X"}}

    monkeypatch.setattr(PlivoService, "_request", staticmethod(fake_request))
    monkeypatch.setattr(settings, "OUTBOUND_RING_TIMEOUT_S", 2, raising=False)

    class _TR:
        provider_status = {}
        twilio_phone_number = None

    await PlivoService.initiate_outbound_call(
        _TR(), to_number="917569672503", answer_url="https://x/a",
        from_number="912264232977", auth_override=("SAid", "tok"),
    )
    assert sent["body"]["ring_timeout"] == 5  # floor of Plivo's accepted range


@pytest.mark.asyncio
async def test_validate_bulk_telephony_flags_unowned_number(monkeypatch):
    # Account GET ok, Number GET 404 → the number isn't rented on the account.
    async def fake_request(method, url, *, auth, json_body=None):
        if "/Number/" in url:
            raise PlivoError("Plivo GET Number failed (404): not found")
        return {"account": "ok"}

    monkeypatch.setattr(PlivoService, "_request", staticmethod(fake_request))
    err = await PlivoService.validate_bulk_telephony("SAid", "tok", "+91 22 6423 2977")
    assert err and "isn't rented" in err


@pytest.mark.asyncio
async def test_validate_bulk_telephony_flags_bad_creds(monkeypatch):
    async def fake_request(method, url, *, auth, json_body=None):
        raise PlivoError("Plivo GET  failed (401): unauthorized")

    monkeypatch.setattr(PlivoService, "_request", staticmethod(fake_request))
    err = await PlivoService.validate_bulk_telephony("SAid", "tok", "912264232977")
    assert err and "credentials" in err


@pytest.mark.asyncio
async def test_validate_bulk_telephony_passes_when_owned(monkeypatch):
    async def fake_request(method, url, *, auth, json_body=None):
        return {"ok": True}  # account + number both resolve

    monkeypatch.setattr(PlivoService, "_request", staticmethod(fake_request))
    assert await PlivoService.validate_bulk_telephony("SAid", "tok", "912264232977") is None


@pytest.mark.asyncio
async def test_rerun_redials_unreached_but_not_answered_leads(monkeypatch):
    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
    from app.services.outbound_campaign_service import OutboundCampaignService

    _stub_single_campaign_guard(monkeypatch)
    monkeypatch.setattr(PlivoService, "bulk_calling_enabled", staticmethod(lambda tr: True))
    monkeypatch.setattr(PlivoService, "bulk_calling_caller_id", staticmethod(lambda tr: "912264232977"))
    monkeypatch.setattr(PlivoService, "bulk_calling_auth", staticmethod(lambda tr: ("SAid", "tok")))

    dialed = {}
    async def fake_dial(campaign, db, *, tenant_res, base, prefix):
        dialed["called"] = True
    monkeypatch.setattr(OutboundCampaignService, "_dial_pending", staticmethod(fake_dial))

    class _FakeDB:
        def add(self, *_a, **_k): pass
        async def commit(self): pass
        async def refresh(self, *_a, **_k): pass

    camp = OutboundCampaign(
        tenant_id="t1", name="rerun", status=CampaignStatus.completed,
        agent_config={"bulk_csv": True}, from_number="old",
        answered_count=1, failed_count=1, total_count=3,
        contacts=[
            {"phone": "911", "name": "A", "status": "failed", "call_id": "C1", "ended": True, "error": "x", "call_link_id": "L1"},
            # answered = reached/cut → must NOT be called back
            {"phone": "922", "name": "B", "status": "answered", "answered_at": "2026-06-22T00:00:00", "call_id": "C2", "ended": True, "call_link_id": "L2"},
            {"phone": "933", "name": "C", "status": "no_answer", "call_id": "C3", "ended": True, "call_link_id": "L3"},
        ],
    )
    # rerun now row-locks + refreshes contacts before rebuilding; hand the lock back
    # the same campaign (the rebuild logic under test is unchanged).
    async def fake_lock(db, cid):
        return camp
    monkeypatch.setattr(OutboundCampaignService, "_lock_campaign", staticmethod(fake_lock))

    out = await OutboundCampaignService.rerun_bulk_campaign(
        camp, _FakeDB(), tenant_res=object(), public_base_url="https://x", path_prefix="/p")

    assert dialed.get("called") is True
    assert out.status == CampaignStatus.running
    assert out.from_number == "912264232977"
    by_phone = {c["phone"]: c for c in out.contacts}
    # the answered lead is left exactly as-is (not re-dialed)
    assert by_phone["922"]["status"] == "answered" and by_phone["922"]["call_link_id"] == "L2"
    # the unreached leads are re-armed to pending with fresh link ids
    assert by_phone["911"]["status"] == "pending" and by_phone["911"]["call_link_id"] != "L1"
    assert by_phone["933"]["status"] == "pending" and by_phone["933"]["call_link_id"] != "L3"
    assert "error" not in by_phone["911"]
    assert out.answered_count == 1 and out.failed_count == 0


@pytest.mark.asyncio
async def test_rerun_noop_when_everyone_reached(monkeypatch):
    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
    from app.services.outbound_campaign_service import OutboundCampaignService

    _stub_single_campaign_guard(monkeypatch)
    monkeypatch.setattr(PlivoService, "bulk_calling_enabled", staticmethod(lambda tr: True))
    monkeypatch.setattr(PlivoService, "bulk_calling_caller_id", staticmethod(lambda tr: "912264232977"))
    monkeypatch.setattr(PlivoService, "bulk_calling_auth", staticmethod(lambda tr: ("SAid", "tok")))

    camp = OutboundCampaign(
        tenant_id="t1", name="rerun", status=CampaignStatus.completed,
        agent_config={"bulk_csv": True},
        contacts=[{"phone": "911", "status": "answered", "answered_at": "x", "call_link_id": "L1"}],
    )

    async def fake_lock(db, cid):
        return camp
    monkeypatch.setattr(OutboundCampaignService, "_lock_campaign", staticmethod(fake_lock))

    with pytest.raises(ValueError, match="already reached"):
        await OutboundCampaignService.rerun_bulk_campaign(
            camp, None, tenant_res=object(), public_base_url="https://x")


def _v2_rerun_setup(monkeypatch):
    """Shared scaffolding for the V2 (per-row) re-run branch tests."""
    from app.services.outbound_campaign_service import OutboundCampaignService

    _stub_single_campaign_guard(monkeypatch)
    monkeypatch.setattr(PlivoService, "bulk_calling_enabled", staticmethod(lambda tr: True))
    monkeypatch.setattr(PlivoService, "bulk_calling_caller_id", staticmethod(lambda tr: "912264232977"))
    monkeypatch.setattr(PlivoService, "bulk_calling_auth", staticmethod(lambda tr: ("SAid", "tok")))

    dialed = {}

    async def fake_dial(campaign, db, *, tenant_res, base, prefix):
        dialed["called"] = True

    monkeypatch.setattr(OutboundCampaignService, "_dial_pending", staticmethod(fake_dial))

    class _FakeDB:
        def __init__(self):
            self.sql = []

        async def execute(self, stmt, params=None):
            self.sql.append(str(stmt))

        async def commit(self):
            pass

        async def refresh(self, *_a, **_k):
            pass

    return dialed, _FakeDB()


@pytest.mark.asyncio
async def test_rerun_v2_rearms_and_resumes(monkeypatch):
    """A V2 campaign (contacts=None — every APEX campaign) re-runs via the per-row
    path: unreached rows re-armed, campaign set running, dialer kicked. This is
    what the ↻ Re-run button does after an Add CSV."""
    import uuid as _uuid

    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
    from app.services import campaign_contacts_v2 as v2
    from app.services.outbound_campaign_service import OutboundCampaignService

    dialed, db = _v2_rerun_setup(monkeypatch)
    calls = {}

    async def fake_rearm(_db, cid):
        calls["rearmed"] = cid
        return 3

    async def fake_pending(_db, cid):
        return 7  # re-armed misses + a just-appended CSV's pending rows

    monkeypatch.setattr(v2, "rearm_unreached", fake_rearm)
    monkeypatch.setattr(v2, "pending_count", fake_pending)

    camp = OutboundCampaign(
        id=_uuid.uuid4(), tenant_id="t1", name="v2 rerun",
        status=CampaignStatus.completed, agent_config={"bulk_csv": True}, contacts=None,
    )
    out = await OutboundCampaignService.rerun_bulk_campaign(
        camp, db, tenant_res=object(), public_base_url="https://x")

    assert calls["rearmed"] == camp.id
    assert dialed.get("called") is True
    assert any("status = 'running'" in s for s in db.sql)  # campaign resumed
    assert out is camp


@pytest.mark.asyncio
async def test_rerun_v2_noop_when_nothing_pending(monkeypatch):
    import uuid as _uuid

    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
    from app.services import campaign_contacts_v2 as v2
    from app.services.outbound_campaign_service import OutboundCampaignService

    _dialed, db = _v2_rerun_setup(monkeypatch)

    async def fake_rearm(_db, cid):
        return 0

    async def fake_pending(_db, cid):
        return 0

    monkeypatch.setattr(v2, "rearm_unreached", fake_rearm)
    monkeypatch.setattr(v2, "pending_count", fake_pending)

    camp = OutboundCampaign(
        id=_uuid.uuid4(), tenant_id="t1", name="drained",
        status=CampaignStatus.completed, agent_config={"bulk_csv": True}, contacts=None,
    )
    with pytest.raises(ValueError, match="already reached"):
        await OutboundCampaignService.rerun_bulk_campaign(
            camp, db, tenant_res=object(), public_base_url="https://x")


@pytest.mark.asyncio
async def test_rerun_canonicalizes_and_dedupes_old_raw_contacts(monkeypatch):
    _stub_single_campaign_guard(monkeypatch)
    """Re-running a campaign created before canonicalization must fix the stored raw
    numbers: a bare 10-digit mobile (Plivo 403s these as a foreign region) gets 91
    prepended, and the same person listed bare AND as +91 collapses to one dial —
    the exact "barred 403 / it keeps calling me" reproduction from the field."""
    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
    from app.services.outbound_campaign_service import OutboundCampaignService

    monkeypatch.setattr(PlivoService, "bulk_calling_enabled", staticmethod(lambda tr: True))
    monkeypatch.setattr(PlivoService, "bulk_calling_caller_id", staticmethod(lambda tr: "912264232977"))
    monkeypatch.setattr(PlivoService, "bulk_calling_auth", staticmethod(lambda tr: ("SAid", "tok")))

    async def fake_dial(campaign, db, *, tenant_res, base, prefix):
        pass
    monkeypatch.setattr(OutboundCampaignService, "_dial_pending", staticmethod(fake_dial))

    class _FakeDB:
        def add(self, *_a, **_k): pass
        async def commit(self): pass
        async def refresh(self, *_a, **_k): pass

    camp = OutboundCampaign(
        tenant_id="t1", name="old-raw", status=CampaignStatus.cancelled,
        agent_config={"bulk_csv": True},
        contacts=[
            {"phone": "7569672503", "name": "Nihar", "status": "failed", "ended": True, "call_link_id": "L1"},
            {"phone": "+917569672503", "name": "Nihar dup", "status": "pending", "call_link_id": "L2"},
            {"phone": "9705636850", "name": "B", "status": "no_answer", "ended": True, "call_link_id": "L3"},
        ],
    )

    async def fake_lock(db, cid):
        return camp
    monkeypatch.setattr(OutboundCampaignService, "_lock_campaign", staticmethod(fake_lock))

    out = await OutboundCampaignService.rerun_bulk_campaign(
        camp, _FakeDB(), tenant_res=object(), public_base_url="https://x", path_prefix="/p")

    phones = sorted(c["phone"] for c in out.contacts)
    # bare 7569672503 + +917569672503 collapse to ONE India number; 9705636850 → 91…
    assert phones == ["917569672503", "919705636850"]
    assert out.total_count == 2  # the duplicate is gone
    # all re-armed to a clean pending state with fresh links + no stale error/call_id
    for c in out.contacts:
        assert c["status"] == "pending" and c["call_id"] is None and "error" not in c
        assert c["call_link_id"] not in ("L1", "L2", "L3")


def test_bulk_dial_concurrency_is_five():
    from app.services.outbound_campaign_service import OutboundCampaignService
    assert OutboundCampaignService.BULK_DIAL_CONCURRENCY == 5


@pytest.mark.asyncio
async def test_rerun_rejects_non_bulk_campaign(monkeypatch):
    from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
    from app.services.outbound_campaign_service import OutboundCampaignService

    camp = OutboundCampaign(tenant_id="t1", name="lead", status=CampaignStatus.completed,
                            agent_config={}, contacts=[{"phone": "911", "call_link_id": "L"}])
    with pytest.raises(ValueError, match="bulk"):
        await OutboundCampaignService.rerun_bulk_campaign(
            camp, None, tenant_res=object(), public_base_url="https://x")
