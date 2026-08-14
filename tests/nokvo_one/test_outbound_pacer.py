"""Ring-ahead pacing + the graceful answer-side decline.

Two halves of the same guarantee: the dialer must not place more calls than it
can answer, and if it ever does anyway, the person who picked up must not be left
listening to silence. Before this, the dialer placed BULK_DIAL_CONCURRENCY (5)
calls regardless of plan and the media WebSocket closed the over-cap ones with
code 1013 — after the callee had already said hello.
"""
from __future__ import annotations

import uuid

import pytest

from app.core.config import settings
from app.services import outbound_pacer


class _StatsDB:
    """Fake session returning one pacer stats row."""

    def __init__(self, connected=0, attempted=0, abandoned=0):
        self._row = (connected, attempted, abandoned)

    async def execute(self, stmt, params=None):
        row = self._row

        class _R:
            def first(self):
                return row

        return _R()


@pytest.fixture(autouse=True)
def _no_redis(monkeypatch):
    """Read stats straight from the (fake) DB — the Redis cache in front of them
    is best-effort and not what these tests are about."""
    monkeypatch.setattr(outbound_pacer, "_cached_stats", outbound_pacer._stats)


@pytest.mark.asyncio
async def test_pacer_off_is_one_line_per_slot(monkeypatch):
    monkeypatch.setattr(settings, "APEX_PACER_ENABLED", False, raising=False)
    assert await outbound_pacer.ring_multiplier_for(_StatsDB(50, 200, 0), uuid.uuid4()) == (1.0, None)


@pytest.mark.asyncio
async def test_pacer_waits_for_a_sample_before_pacing(monkeypatch):
    """A cold campaign must not guess: guessing is how the first hundred contacts
    get burned abandoning every connect."""
    monkeypatch.setattr(settings, "APEX_PACER_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "APEX_PACER_MIN_SAMPLE", 25, raising=False)
    assert await outbound_pacer.ring_multiplier_for(_StatsDB(2, 5, 0), uuid.uuid4()) == (1.0, None)


@pytest.mark.asyncio
async def test_pacer_multiplier_is_the_inverse_answer_rate(monkeypatch):
    monkeypatch.setattr(settings, "APEX_PACER_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "APEX_PACER_MIN_SAMPLE", 25, raising=False)
    monkeypatch.setattr(settings, "APEX_PACER_MIN_ANSWER_RATE", 0.10, raising=False)
    monkeypatch.setattr(settings, "APEX_PACER_MAX_RING_AHEAD", 10, raising=False)
    # 25% answered → ring 4 lines per free conversation slot.
    mult, ceiling = await outbound_pacer.ring_multiplier_for(_StatsDB(50, 200, 0), uuid.uuid4())
    assert mult == pytest.approx(4.0)
    assert ceiling == 10


@pytest.mark.asyncio
async def test_pacer_floors_the_answer_rate_not_the_multiplier(monkeypatch):
    """A near-dead list must not make the pacer ask for hundreds of lines."""
    monkeypatch.setattr(settings, "APEX_PACER_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "APEX_PACER_MIN_SAMPLE", 25, raising=False)
    monkeypatch.setattr(settings, "APEX_PACER_MIN_ANSWER_RATE", 0.10, raising=False)
    mult, _ = await outbound_pacer.ring_multiplier_for(_StatsDB(1, 1000, 0), uuid.uuid4())
    assert mult == pytest.approx(10.0)  # 1/0.10, not 1/0.001


@pytest.mark.asyncio
async def test_pacer_collapses_to_safe_when_abandoning(monkeypatch):
    """Abandons are people hung up on. Breaching the ceiling stops ring-ahead
    immediately rather than trimming it."""
    monkeypatch.setattr(settings, "APEX_PACER_ENABLED", True, raising=False)
    monkeypatch.setattr(settings, "APEX_PACER_MIN_SAMPLE", 25, raising=False)
    monkeypatch.setattr(settings, "APEX_PACER_MAX_ABANDON_PCT", 3.0, raising=False)
    # 100 connects, 5 abandoned = 5% > 3% ceiling.
    assert await outbound_pacer.ring_multiplier_for(
        _StatsDB(100, 400, 5), uuid.uuid4()
    ) == (1.0, None)
    # 2 abandoned = 2%, under the ceiling → pacing resumes.
    mult, _ = await outbound_pacer.ring_multiplier_for(_StatsDB(100, 400, 2), uuid.uuid4())
    assert mult > 1.0


@pytest.mark.asyncio
async def test_pacer_fails_safe_on_error(monkeypatch):
    monkeypatch.setattr(settings, "APEX_PACER_ENABLED", True, raising=False)

    class _Broken:
        async def execute(self, *a, **kw):
            raise RuntimeError("db down")

    assert await outbound_pacer.ring_multiplier_for(_Broken(), uuid.uuid4()) == (1.0, None)


# ── the graceful answer-side decline ─────────────────────────────────────────


class _AnswerDB:
    """Fake session for _outbound_capacity_available: an org row + a requeue UPDATE."""

    def __init__(self, tier="nokvo_apex", concurrency=1):
        self.org = type("Org", (), {
            "product_tier": tier, "apex_concurrency": concurrency, "apex_plan_code": "core",
        })()
        self.requeued = False
        self.committed = False

    async def get(self, model, pk):
        return self.org

    async def execute(self, stmt, params=None):
        outer = self
        s = str(stmt)

        class _R:
            def first(self):
                if "abandoned_at" in s:
                    outer.requeued = True
                    return (uuid.uuid4(),)
                return None

            def scalars(self):
                return self

        return _R()

    async def commit(self):
        self.committed = True

    async def rollback(self):
        pass


@pytest.mark.asyncio
async def test_answer_gate_declines_and_requeues_when_at_capacity(monkeypatch):
    """At capacity the ring is cut BEFORE any audio and the contact goes back in
    the queue — never dead air followed by a silent hangup, and never filed as a
    missed call the customer thinks nobody answered."""
    from app.api import nokvo_one_voice as v
    from types import SimpleNamespace

    camp = SimpleNamespace(id=uuid.uuid4(), tenant_id="t1")

    async def fake_lookup(link, db):
        return camp, {"phone": "919000000001"}

    async def fake_tenant(db, tenant_id):
        return SimpleNamespace(tenant_id="t1", organization_id=uuid.uuid4())

    async def at_cap(*a, **kw):
        return 1  # one conversation already live, plan concurrency is 1

    monkeypatch.setattr(v.OutboundCampaignService, "get_by_call_link_id", fake_lookup)
    monkeypatch.setattr(v, "_tenant_by_tenant_id", fake_tenant)
    monkeypatch.setattr("app.services.call_concurrency.active_count", at_cap)

    db = _AnswerDB()
    result = await v._outbound_capacity_available("LINK1", db)
    assert result is v._SLOT_DENIED
    assert db.requeued and db.committed


@pytest.mark.asyncio
async def test_answer_gate_proceeds_when_a_slot_is_free(monkeypatch):
    from app.api import nokvo_one_voice as v
    from types import SimpleNamespace

    camp = SimpleNamespace(id=uuid.uuid4(), tenant_id="t1")

    async def fake_lookup(link, db):
        return camp, {"phone": "919000000001"}

    async def fake_tenant(db, tenant_id):
        return SimpleNamespace(tenant_id="t1", organization_id=uuid.uuid4())

    async def idle(*a, **kw):
        return 0

    monkeypatch.setattr(v.OutboundCampaignService, "get_by_call_link_id", fake_lookup)
    monkeypatch.setattr(v, "_tenant_by_tenant_id", fake_tenant)
    monkeypatch.setattr("app.services.call_concurrency.active_count", idle)

    db = _AnswerDB()
    # Proceeds without reserving — the media WS acquires, and releases in its
    # finally. A split acquire/release would leak the slot when Plivo never
    # opens the stream, wedging a one-slot plan for the stale-token window.
    assert await v._outbound_capacity_available("LINK1", db) is None
    assert not db.requeued


@pytest.mark.asyncio
async def test_answer_gate_fails_open(monkeypatch):
    """A lookup blip must never drop a live call — proceed without a token and
    let the media WS acquire as it did before."""
    from app.api import nokvo_one_voice as v

    async def boom(link, db):
        raise RuntimeError("db down")

    monkeypatch.setattr(v.OutboundCampaignService, "get_by_call_link_id", boom)
    assert await v._outbound_capacity_available("LINK1", _AnswerDB()) is None


@pytest.mark.asyncio
async def test_answer_gate_skips_followups(monkeypatch):
    """Follow-ups draw from the inbound pool on the media WS — unchanged."""
    from app.api import nokvo_one_voice as v
    from types import SimpleNamespace

    async def fake_lookup(link, db):
        return SimpleNamespace(id=uuid.uuid4(), tenant_id="t1"), {"is_followup": True}

    monkeypatch.setattr(v.OutboundCampaignService, "get_by_call_link_id", fake_lookup)
    assert await v._outbound_capacity_available("LINK1", _AnswerDB()) is None
