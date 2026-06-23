"""Regression tests for the bulk-calling "it keeps calling me" re-dial loop.

Root cause was a lost-update race on the ``campaign.contacts`` JSON blob: the
launch dialer and the Plivo status webhooks each did a read-modify-write of the
whole blob with no row locking, so a webhook that loaded the campaign before a
placement commit landed would write its stale snapshot back — reverting
just-placed contacts to ``pending``. The throttled dialer then re-placed them,
which fired more webhooks, looping forever until the campaign was cancelled.

The fix:
  * every contacts read-modify-write goes through ``_lock_campaign`` (SELECT …
    FOR UPDATE + populate_existing) so handlers serialize and always start from
    the freshest committed snapshot;
  * ``handle_call_status`` persists the contacts mutation atomically under that
    lock BEFORE the best-effort side effects (which open their own transactions);
  * ``_dial_pending`` guards against re-placing a contact that already has a
    placement (call_id) or has ended.

These tests pin that behaviour without a live Postgres by stubbing the lock to
hand back a chosen "freshly-locked" campaign.
"""
from __future__ import annotations

import uuid

import pytest

from app.models.outbound_campaign import CampaignStatus, OutboundCampaign
from app.services.outbound_campaign_service import OutboundCampaignService as S
from app.services.plivo_service import PlivoService


class _FakeResult:
    def scalars(self):
        return self

    def first(self):
        return None

    def scalar_one_or_none(self):
        return None


class _FakeDB:
    """Minimal stand-in: records commits/adds; every query resolves empty."""

    def __init__(self):
        self.commits = 0
        self.added = []

    async def execute(self, _stmt):
        return _FakeResult()

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        pass

    async def get(self, _model, _pk):
        return None


def _bulk_caller(monkeypatch):
    monkeypatch.setattr(PlivoService, "bulk_calling_caller_id", staticmethod(lambda tr: "912264232977"))
    monkeypatch.setattr(PlivoService, "bulk_calling_auth", staticmethod(lambda tr: ("SAid", "tok")))


# ── _lock_campaign: requests FOR UPDATE + populate_existing ───────────────────

@pytest.mark.asyncio
async def test_lock_campaign_requests_for_update_and_populate_existing():
    captured = {}

    class _DB:
        async def execute(self, stmt):
            captured["stmt"] = stmt

            class _R:
                def scalar_one_or_none(self_inner):
                    return "CAMP"

            return _R()

    out = await S._lock_campaign(_DB(), uuid.uuid4())
    assert out == "CAMP"
    stmt = captured["stmt"]
    # FOR UPDATE locks the row so concurrent webhooks serialize; populate_existing
    # overwrites the caller's stale in-memory snapshot (expire_on_commit=False).
    assert stmt._for_update_arg is not None
    assert stmt.get_execution_options().get("populate_existing") is True


# ── _dial_pending: never re-places an already-placed / ended contact ──────────

@pytest.mark.asyncio
async def test_dial_pending_skips_already_placed_and_ended(monkeypatch):
    camp = OutboundCampaign(
        tenant_id="t1", name="c", status=CampaignStatus.running,
        agent_config={"bulk_csv": True},
        contacts=[
            # "pending" yet already has a placement — the exact stale shape that
            # used to get re-dialed. Must be skipped.
            {"phone": "911", "status": "pending", "call_id": "C1", "call_link_id": "L1"},
            # ended — slot already freed, must be skipped.
            {"phone": "922", "status": "pending", "ended": True, "call_link_id": "L2"},
            # genuinely undialed — the only one that should dial.
            {"phone": "933", "status": "pending", "call_id": None, "call_link_id": "L3"},
        ],
    )
    _bulk_caller(monkeypatch)
    monkeypatch.setattr(S, "_lock_campaign", staticmethod(lambda db, cid: _async(camp)))

    placed = []

    async def fake_place(contact, **_kw):
        contact["status"] = "calling"
        contact["call_id"] = "NEW"
        placed.append(contact["phone"])

    monkeypatch.setattr(S, "_place_call", staticmethod(fake_place))

    db = _FakeDB()
    await S._dial_pending(camp, db, tenant_res=object(), base="https://x", prefix="/p")

    assert placed == ["933"]
    assert db.commits >= 1  # committed to release the lock


@pytest.mark.asyncio
async def test_dial_pending_dials_off_the_locked_snapshot_not_the_stale_arg(monkeypatch):
    # The caller still holds a STALE campaign (both contacts pending). The
    # committed/locked state already has p1 placed. _dial_pending must dial off
    # the fresh locked snapshot, so p1 is NOT re-dialed.
    cid = uuid.uuid4()
    stale = OutboundCampaign(
        id=cid, tenant_id="t1", name="c", status=CampaignStatus.running,
        agent_config={"bulk_csv": True},
        contacts=[
            {"phone": "911", "status": "pending", "call_id": None, "call_link_id": "L1"},
            {"phone": "922", "status": "pending", "call_id": None, "call_link_id": "L2"},
        ],
    )
    fresh = OutboundCampaign(
        id=cid, tenant_id="t1", name="c", status=CampaignStatus.running,
        agent_config={"bulk_csv": True},
        contacts=[
            {"phone": "911", "status": "calling", "call_id": "C1", "call_link_id": "L1"},
            {"phone": "922", "status": "pending", "call_id": None, "call_link_id": "L2"},
        ],
    )
    _bulk_caller(monkeypatch)
    monkeypatch.setattr(S, "_lock_campaign", staticmethod(lambda db, c: _async(fresh)))

    placed = []

    async def fake_place(contact, **_kw):
        contact["status"] = "calling"
        contact["call_id"] = "NEW"
        placed.append(contact["phone"])

    monkeypatch.setattr(S, "_place_call", staticmethod(fake_place))

    await S._dial_pending(stale, _FakeDB(), tenant_res=object(), base="https://x", prefix="/p")

    assert placed == ["922"]  # p1 already placed in the locked snapshot → not re-dialed


# ── handle_call_status: hangup persists atomically and never reverts siblings ──

@pytest.mark.asyncio
async def test_handle_hangup_marks_ended_without_reverting_siblings(monkeypatch):
    cid = uuid.uuid4()
    # Freshly-locked state: both lines were placed (calling). This is what the
    # webhook must read — NOT a caller's stale "both pending" snapshot.
    fresh = OutboundCampaign(
        id=cid, tenant_id="t1", name="c", status=CampaignStatus.running,
        agent_config={"bulk_csv": True}, answered_count=0, failed_count=0,
        contacts=[
            {"phone": "911", "status": "calling", "call_id": "C1", "call_link_id": "L1"},
            {"phone": "922", "status": "calling", "call_id": "C2", "call_link_id": "L2"},
        ],
    )
    monkeypatch.setattr(S, "_lock_campaign", staticmethod(lambda db, c: _async(fresh)))
    # Side effects are best-effort and separately covered; stub them out.
    monkeypatch.setattr(S, "_close_call_outcomes", staticmethod(lambda *a, **k: _async(False)))
    monkeypatch.setattr(S, "_enqueue_post_call_followup", staticmethod(lambda *a, **k: _async(None)))

    # The caller hands in a STALE campaign (both pending) — the regression is that
    # this stale snapshot must not be the one written back.
    stale = OutboundCampaign(
        id=cid, tenant_id="t1", name="c", status=CampaignStatus.running,
        agent_config={"bulk_csv": True},
        contacts=[
            {"phone": "911", "status": "pending", "call_link_id": "L1"},
            {"phone": "922", "status": "pending", "call_link_id": "L2"},
        ],
    )

    db = _FakeDB()
    await S.handle_call_status(stale, "L1", "call.hangup", {"hangup_cause": "NORMAL_CLEARING"}, db)

    by = {c["phone"]: c for c in fresh.contacts}
    # L1 ended/failed …
    assert by["911"].get("ended") is True
    assert by["911"]["status"] == "failed"
    # … and crucially L2 is left exactly as it was — NOT reverted to "pending".
    assert by["922"]["status"] == "calling"
    assert not by["922"].get("ended")
    assert fresh.failed_count == 1
    assert db.commits >= 1  # contacts persisted atomically under the lock


@pytest.mark.asyncio
async def test_handle_answered_increments_without_reverting_siblings(monkeypatch):
    cid = uuid.uuid4()
    fresh = OutboundCampaign(
        id=cid, tenant_id="t1", name="c", status=CampaignStatus.running,
        agent_config={"bulk_csv": True}, answered_count=0, failed_count=0,
        contacts=[
            {"phone": "911", "status": "calling", "call_id": "C1", "call_link_id": "L1"},
            {"phone": "922", "status": "calling", "call_id": "C2", "call_link_id": "L2"},
        ],
    )
    monkeypatch.setattr(S, "_lock_campaign", staticmethod(lambda db, c: _async(fresh)))

    db = _FakeDB()
    await S.handle_call_status(fresh, "L1", "call.answered", {}, db)

    by = {c["phone"]: c for c in fresh.contacts}
    assert by["911"]["status"] == "answered" and by["911"].get("answered_at")
    assert by["922"]["status"] == "calling"  # sibling untouched
    assert fresh.answered_count == 1
    assert fresh.status == CampaignStatus.running  # not complete — calls still live


# ── canonicalize + dedupe: same person listed twice is dialed once ───────────

def test_canonical_phone_india_forms_collapse():
    from app.services.outbound_campaign_service import _canonical_phone

    # bare 10-digit, +country-code, spaced, and already-canonical all match
    assert _canonical_phone("7569672503") == "917569672503"
    assert _canonical_phone("+917569672503") == "917569672503"
    assert _canonical_phone("+91 75696 72503") == "917569672503"
    assert _canonical_phone("917569672503") == "917569672503"
    assert _canonical_phone("07569672503") == "917569672503"  # leading-0 trunk form
    assert _canonical_phone("") == ""
    assert _canonical_phone(None) == ""


def test_dedupe_contacts_collapses_same_person():
    from app.services.outbound_campaign_service import _dedupe_contacts

    out = _dedupe_contacts([
        {"phone": "7569672503", "name": "Nihar"},
        {"phone": "+917569672503", "name": "Nihar (dup)"},  # same person → dropped
        {"phone": "9177627064", "name": "B"},               # 10-digit → 91 prepended
        {"phone": "bad", "name": "C"},                       # uncanonicalizable → dropped
    ])
    phones = [c["phone"] for c in out]
    assert phones == ["917569672503", "919177627064"]  # one entry per person, canonical form
    assert out[0]["name"] == "Nihar"                   # first occurrence's name wins


# ── admin mutators also lock before the contacts read-modify-write ────────────
# The original fix routed only the launch dialer (_dial_pending) and the webhook
# (handle_call_status) through _lock_campaign. attach_leads / launch_campaign
# (relaunch) / rerun_bulk_campaign mutate campaign.contacts too and are reachable
# while a batch is still in flight — an unlocked read-modify-write there clobbers
# a webhook's just-committed contact state, re-opening the lost-update (stranded
# slots → fan-out stuck at one; reached leads reverted → re-dialed).


@pytest.mark.asyncio
async def test_rerun_reads_the_locked_snapshot_not_the_stale_arg(monkeypatch):
    """rerun_bulk_campaign must rebuild off the FRESH locked contacts. A contact
    whose answered webhook committed AFTER the caller's stale read is preserved as
    reached (kept, original link) instead of being re-armed and re-dialed — the
    "I cut the call and it rang me again" report on the bulk Re-run button."""
    import app.services.outbound_campaign_service as ocs

    cid = uuid.uuid4()
    # Caller's STALE snapshot: 911 still looks like a live "calling" (not reached).
    stale = OutboundCampaign(
        id=cid, tenant_id="t1", name="c", status=CampaignStatus.completed,
        agent_config={"bulk_csv": True},
        contacts=[
            {"phone": "911", "status": "calling", "call_id": "C1", "call_link_id": "OLD-911"},
            {"phone": "922", "status": "no_answer", "ended": True, "call_link_id": "OLD-922"},
        ],
    )
    # Freshly-locked committed truth: 911 ANSWERED (picked up) since the stale read.
    fresh = OutboundCampaign(
        id=cid, tenant_id="t1", name="c", status=CampaignStatus.completed,
        agent_config={"bulk_csv": True}, answered_count=1, failed_count=1,
        contacts=[
            {"phone": "911", "status": "answered", "answered_at": "2026-06-23T00:00:00+00:00",
             "call_id": "C1", "ended": True, "call_link_id": "OLD-911"},
            {"phone": "922", "status": "no_answer", "ended": True, "call_link_id": "OLD-922"},
        ],
    )
    _bulk_caller(monkeypatch)
    monkeypatch.setattr(PlivoService, "bulk_calling_enabled", staticmethod(lambda tr: True))
    monkeypatch.setattr(S, "_lock_campaign", staticmethod(lambda db, c: _async(fresh)))
    monkeypatch.setattr(ocs, "invalidate_outbound_context", lambda *_a, **_k: None)

    dialed_with = {}

    async def fake_dial(campaign, db, **_kw):
        dialed_with["campaign"] = campaign

    monkeypatch.setattr(S, "_dial_pending", staticmethod(fake_dial))

    out = await S.rerun_bulk_campaign(
        stale, _FakeDB(), tenant_res=object(),
        public_base_url="https://x", path_prefix="/p",
    )

    by = {c["phone"]: c for c in out.contacts}
    # 911 picked up (per the LOCKED truth) → preserved as reached, original link kept,
    # NOT re-armed. Reading the stale "calling" snapshot would have re-dialed someone
    # who already answered.
    assert by["911"]["status"] == "answered"
    assert by["911"]["call_link_id"] == "OLD-911"
    # 922 genuinely not reached → re-armed with a fresh link + pending.
    assert by["922"]["status"] == "pending"
    assert by["922"]["call_link_id"] != "OLD-922"
    assert out is fresh                      # operated on the locked object
    assert dialed_with["campaign"] is fresh  # dialer fed the locked snapshot


def test_all_contacts_mutators_go_through_lock_campaign():
    """Invariant guard: EVERY method that read-modify-writes campaign.contacts while
    a batch can be in flight must acquire _lock_campaign first. Cheap source check so
    a future writer can't silently reintroduce the lost-update ("keeps calling") race.
    (detach_lead is excluded — it is gated to draft campaigns, which have no in-flight
    calls and therefore no concurrent webhook to race.)"""
    import inspect

    for name in (
        "_dial_pending",
        "handle_call_status",
        "attach_leads",
        "launch_campaign",
        "rerun_bulk_campaign",
    ):
        src = inspect.getsource(getattr(S, name))
        assert "_lock_campaign" in src, (
            f"{name} read-modify-writes campaign.contacts but does not go through "
            f"_lock_campaign — this reopens the bulk-calling re-dial race."
        )


# ── caller-ID rotation: spread a bulk batch across the sub-account's DID pool ──


def test_pick_caller_prefers_free_number():
    # A number not already on a live call is preferred (spread).
    assert S._pick_caller(["A", "B", "C"], {"A", "B"}) == "C"
    # Everything busy → still returns a pool member (plain random pick).
    assert S._pick_caller(["A", "B"], {"A", "B"}) in {"A", "B"}
    # Single-number pool → that number, busy or not.
    assert S._pick_caller(["A"], set()) == "A"
    assert S._pick_caller(["A"], {"A"}) == "A"


@pytest.mark.asyncio
async def test_dial_pending_rotates_across_pool(monkeypatch):
    """A bulk batch of 3 fans out over 3 DIFFERENT numbers from the pool, and each
    placed contact records the from_number it dialed on."""
    camp = OutboundCampaign(
        tenant_id="t1", name="c", status=CampaignStatus.running,
        agent_config={"bulk_csv": True},
        contacts=[
            {"phone": "911", "status": "pending", "call_link_id": "L1"},
            {"phone": "922", "status": "pending", "call_link_id": "L2"},
            {"phone": "933", "status": "pending", "call_link_id": "L3"},
        ],
    )
    _bulk_caller(monkeypatch)
    monkeypatch.setattr(S, "_lock_campaign", staticmethod(lambda db, cid: _async(camp)))
    monkeypatch.setattr(S, "_resolve_caller_pool",
                        staticmethod(lambda *a, **k: _async(["A", "B", "C"])))

    used = []

    async def fake_place(contact, *, caller_id, **_kw):
        used.append(caller_id)
        contact["status"] = "calling"
        contact["call_id"] = "NEW"

    monkeypatch.setattr(S, "_place_call", staticmethod(fake_place))

    await S._dial_pending(camp, _FakeDB(), tenant_res=object(), base="https://x", prefix="/p")

    assert sorted(used) == ["A", "B", "C"]                       # 3 distinct numbers
    assert sorted(c["from_number"] for c in camp.contacts) == ["A", "B", "C"]


@pytest.mark.asyncio
async def test_dial_pending_uses_single_caller_when_pool_has_one(monkeypatch):
    """A one-DID pool (non-bulk, or a bulk account with one number / empty listing)
    dials every call from that number — exactly today's behaviour."""
    camp = OutboundCampaign(
        tenant_id="t1", name="c", status=CampaignStatus.running,
        agent_config={"bulk_csv": True},
        contacts=[
            {"phone": "911", "status": "pending", "call_link_id": "L1"},
            {"phone": "922", "status": "pending", "call_link_id": "L2"},
        ],
    )
    _bulk_caller(monkeypatch)
    monkeypatch.setattr(S, "_lock_campaign", staticmethod(lambda db, cid: _async(camp)))
    monkeypatch.setattr(S, "_resolve_caller_pool",
                        staticmethod(lambda *a, **k: _async(["912264232977"])))

    used = []

    async def fake_place(contact, *, caller_id, **_kw):
        used.append(caller_id)
        contact["status"] = "calling"
        contact["call_id"] = "NEW"

    monkeypatch.setattr(S, "_place_call", staticmethod(fake_place))

    await S._dial_pending(camp, _FakeDB(), tenant_res=object(), base="https://x", prefix="/p")

    assert used == ["912264232977", "912264232977"]


@pytest.mark.asyncio
async def test_resolve_caller_pool_bulk_lists_and_caches(monkeypatch):
    """Bulk → live DID list (granted fallback appended if missing), cached by
    auth_id; non-bulk → single fallback, never lists."""
    import app.services.outbound_campaign_service as ocs
    ocs._CALLER_POOL_CACHE.clear()

    calls = {"n": 0}

    async def fake_list(auth):
        calls["n"] += 1
        return ["N1", "N2"]

    monkeypatch.setattr(PlivoService, "list_account_numbers", staticmethod(fake_list))

    bulk = OutboundCampaign(tenant_id="t1", name="c", status=CampaignStatus.running,
                            agent_config={"bulk_csv": True}, contacts=[])
    lead = OutboundCampaign(tenant_id="t1", name="c", status=CampaignStatus.running,
                            agent_config={}, contacts=[])

    # non-bulk never lists — keeps its single fallback
    out = await S._resolve_caller_pool(lead, object(), auth_override=None, fallback="F")
    assert out == ["F"] and calls["n"] == 0

    # bulk lists live; fallback appended because it's not in the listing
    out1 = await S._resolve_caller_pool(bulk, object(), auth_override=("AID", "tok"), fallback="F")
    assert out1 == ["N1", "N2", "F"] and calls["n"] == 1
    # within TTL → served from cache, no second Plivo hit
    out2 = await S._resolve_caller_pool(bulk, object(), auth_override=("AID", "tok"), fallback="F")
    assert out2 == ["N1", "N2", "F"] and calls["n"] == 1


@pytest.mark.asyncio
async def test_resolve_caller_pool_empty_list_falls_back(monkeypatch):
    """An empty/failed listing degrades to the single granted caller ID."""
    import app.services.outbound_campaign_service as ocs
    ocs._CALLER_POOL_CACHE.clear()
    monkeypatch.setattr(PlivoService, "list_account_numbers",
                        staticmethod(lambda auth: _async([])))
    bulk = OutboundCampaign(tenant_id="t1", name="c", status=CampaignStatus.running,
                            agent_config={"bulk_csv": True}, contacts=[])
    out = await S._resolve_caller_pool(bulk, object(), auth_override=("AID", "tok"), fallback="F")
    assert out == ["F"]


def _async(value):
    """Wrap a plain value in an awaitable for monkeypatched async staticmethods."""
    async def _coro(*_a, **_k):
        return value
    return _coro()
